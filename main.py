import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
import uvicorn
import os

# Yazdığımız kendi modüllerimizi (Sistem Organlarını) İçe Aktarıyoruz
from core.config import config
from exchange.binance_async import BinanceFuturesClient
from execution.engine import ExecutionEngine
from strategy.atlantis_runner import AtlantisStrategyRunner
from execution.risk_manager import RiskManager
from core.database import db
from core.notifier import notifier
from core.diagnostics import diagnostics_store

# Loglama Konfigürasyonu
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("Atlantis_Node")

# Takip Edilecek Semboller
TARGET_SYMBOLS = ["SOL/USDT", "BNB/USDT", "ETH/USDT"]

# Global bileşenleri tanımla
binance_client = None
execution_engine = None
strategy_runner = None
background_tasks = set()


async def reconcile_system_state(client: BinanceFuturesClient, symbols: list):
    """
    Sistem Başlangıç Mutabakatı (Self-Healing / Reconciliation Loop)
    Veritabanı (SSOT) ile Binance Futures üzerindeki gerçek durumu eşleştirir ve onarır.
    API down ise graceful degradation - reconciliation skip edilir.
    """
    logger.info("🔄 Sistem Mutabakatı (Reconciliation) başlatılıyor...")
    try:
        # 1. Borsadaki aktif pozisyonları çek (contracts > 0 olanlar)
        try:
            await client.exchange.load_markets()
            raw_positions = await client.exchange.fetch_positions(symbols)
        except Exception as api_err:
            # API down ise (502, timeout vb), reconciliation'ı skip et
            logger.warning(f"⚠️ Reconciliation: Binance API erişilemedi ({type(api_err).__name__}). Başlangıç mutabakatı skip edildi.")
            logger.info("✅ Bot yine de başlatılıyor - API online olduktan sonra sonraki scan'de senkronize olacak.")
            return
        binance_positions = {}

        for pos in raw_positions:
            contracts = float(pos.get('contracts', 0.0)
                              or pos.get('positionAmt', 0.0) or 0.0)
            if abs(contracts) > 0.0:
                # "SOL/USDT:USDT" -> "SOLUSDT" dönüşümü (DB formatı)
                sym = pos['symbol'].split(':')[0].replace('/', '')

                pos_side = 'LONG' if contracts > 0 else 'SHORT'
                if 'side' in pos and pos['side']:
                    pos_side = pos['side'].upper()  # 'LONG' veya 'SHORT'

                binance_positions[sym] = {
                    'side': pos_side,
                    'size': abs(contracts),
                    'entry_price': float(pos.get('entryPrice', 0.0))
                }

        # 2. Veritabanındaki açık işlemleri çek
        db_open_trades = await db.get_all_open_trades()
        db_open_dict = {t['symbol']: t for t in db_open_trades}

        logger.info(
            f"📊 Mevcut Durum: Borsada {len(binance_positions)} açık pozisyon, DB'de {len(db_open_trades)} açık işlem var.")

        # 3. Durum A & C Kontrolü: DB'de açık olanların borsadaki karşılıklarını denetle
        for db_trade in db_open_trades:
            symbol = db_trade['symbol']  # DB formatı: "SOLUSDT"
            # CCXT formatı: "SOL/USDT"
            sym_ccxt = symbol.replace('USDT', '/USDT', 1) if 'USDT' in symbol else symbol

            if symbol not in binance_positions:
                # Durum A: DB'de açık görünüyor ama Binance'te kapanmış (Örn: Stop Loss tetiklenmiş)
                logger.warning(
                    f"[{symbol}] ⚠️ Tespit: İşlem DB'de AÇIK ama borsada pozisyon YOK! Kapatılıyor...")

                # Kapanış fiyatı olarak son fiyatı çekelim (CCXT formatında)
                ticker = await client.exchange.fetch_ticker(sym_ccxt)
                last_price = float(ticker.get('last', 0.0))

                # PnL hesaplama
                pnl = 0.0
                if db_trade['side'] == 'LONG':
                    pnl = (last_price -
                           db_trade['entry_price']) * db_trade['size']
                else:
                    pnl = (db_trade['entry_price'] -
                           last_price) * db_trade['size']

                await db.close_trade(symbol, close_price=last_price, pnl=pnl)

                await notifier.send_message(
                    f"🔄 <b>SİSTEM MUTABAKATI (OTOMATİK ONARIM)</b>\n\n"
                    f"📌 <b>Parite:</b> {symbol}\n"
                    f"⚠️ <b>Durum:</b> Bot kapalıyken borsada pozisyon kapanmış (Stop Loss / Manuel).\n"
                    f"💾 <b>Eylem:</b> Veritabanı kaydı senkronize edilerek 'CLOSED' yapıldı.\n"
                    f"💰 <b>Kapanış Fiyatı:</b> {last_price:.4f}\n"
                    f"💵 <b>Tahmini PnL:</b> {pnl:+.2f} USDT"
                )
            else:
                # Durum C: İkisinde de var ama boyut (size) farklılığı kontrolü
                bin_pos = binance_positions[symbol]
                if abs(db_trade['size'] - bin_pos['size']) > 1e-5:
                    logger.warning(
                        f"[{symbol}] ⚠️ Tespit: Boyut uyuşmazlığı! DB: {db_trade['size']}, Binance: {bin_pos['size']}. DB güncelleniyor...")
                    async with db.pool.acquire() as conn:
                        await conn.execute("UPDATE trades SET size = $1 WHERE id = $2", bin_pos['size'], db_trade['id'])

                    await notifier.send_message(
                        f"🔄 <b>SİSTEM MUTABAKATI (OTOMATİK ONARIM)</b>\n\n"
                        f"📌 <b>Parite:</b> {symbol}\n"
                        f"⚠️ <b>Durum:</b> İşlem boyutu uyumsuzluğu tespit edildi.\n"
                        f"💾 <b>Eylem:</b> Veritabanı boyutu borsa ile eşitlendi.\n"
                        f"📐 <b>Yeni Boyut:</b> {bin_pos['size']}"
                    )

        # 4. Durum B Kontrolü: Borsada açık ama DB'de kaydı olmayan kaçak pozisyonları kapat (Emergency Close)
        for symbol, bin_pos in binance_positions.items():
            if symbol not in db_open_dict:
                # CCXT formatına çevir
                sym_ccxt = symbol.replace('USDT', '/USDT', 1) if 'USDT' in symbol else symbol

                logger.critical(
                    f"[{symbol}] 🔥 ACİL DURUM: Borsada açık pozisyon var ama veritabanında KAYDI YOK! Güvenlik için pozisyon acilen kapatılıyor...")

                close_side = 'sell' if bin_pos['side'] == 'LONG' else 'buy'
                formatted_size = float(
                    client.exchange.amount_to_precision(sym_ccxt, bin_pos['size']))

                # Pozisyonu borsada acil kapat (CCXT formatında)
                await client.exchange.create_order(
                    symbol=sym_ccxt, type='market', side=close_side, amount=formatted_size,
                    params={'reduceOnly': True}
                )

                # Bekleyen tüm emirleri iptal et (CCXT formatında)
                await client.exchange.cancel_all_orders(sym_ccxt)

                await notifier.send_message(
                    f"🚨 <b>ACİL DURUM MUTABAKATI (EMERGENCY CLOSE)</b>\n\n"
                    f"📌 <b>Parite:</b> {symbol}\n"
                    f"🔥 <b>Hata:</b> Borsada yetim/kaçak açık pozisyon tespit edildi (DB kaydı yok!).\n"
                    f"🛡️ <b>Eylem:</b> Pozisyon borsa üzerinde piyasa fiyatından acilen kapatıldı ve bekleyen tüm emirler iptal edildi!"
                )

        logger.info(
            "✅ Sistem Mutabakatı başarıyla tamamlandı. Sistem senkronize durumda.")
    except Exception as e:
        logger.error(
            f"❌ Sistem Mutabakatı sırasında kritik hata: {e}", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global binance_client, execution_engine, strategy_runner

    logger.info("Atlantis Trading Bot Başlatılıyor...")
    await db.connect()

    binance_client = BinanceFuturesClient(
        api_key=config.BINANCE_API_KEY,
        secret_key=config.BINANCE_SECRET_KEY,
        testnet=config.TESTNET
    )

    # Risk ve İşlem Motorlarını Başlat
    execution_engine = ExecutionEngine(client=binance_client)
    risk_manager = RiskManager(client=binance_client)

    # --- SİSTEM BAŞLANGIÇ MUTABAKATI (SELF-HEALING) ---
    if db.pool:
        await reconcile_system_state(binance_client, TARGET_SYMBOLS)

    # Strateji Motoruna risk_manager'ı da dahil ederek başlat
    strategy_runner = AtlantisStrategyRunner(
        symbols=TARGET_SYMBOLS,
        execution_engine=execution_engine,
        risk_manager=risk_manager,
        client=binance_client,
        timeframe=config.TIMEFRAME
    )

    # Açık pozisyonlar varsa RAM state'e geri yükle
    await strategy_runner.restore_state_from_db()

    # Strateji döngüsünü arka planda asenkron olarak çalıştır
    task_strategy = asyncio.create_task(strategy_runner.start())
    background_tasks.add(task_strategy)

    logger.info("Tüm sistemler aktif. Piyasalar taranıyor...")

    yield  # --- SUNUCU BURADA ÇALIŞIR ---

    # --- KAPANIŞ SÜRECİ (Graceful Shutdown) ---
    logger.info("Kapanış sinyali alındı. Motorlar güvenle durduruluyor...")

    if strategy_runner:
        strategy_runner.stop()

    # Asenkron görevlerin bitmesini bekle
    for task in background_tasks:
        task.cancel()

    if binance_client:
        await binance_client.close()

    await notifier.close()
    logger.info("Sistem başarıyla ve güvenle kapatıldı.")
    await db.close()

# FastAPI Uygulaması
app = FastAPI(title="Atlantis Trading Node", version="1.0", lifespan=lifespan)


@app.get("/")
async def root_status():
    """Sistemin genel sağlık durumunu döndürür."""
    return {
        "status": "ONLINE",
        "system": "Atlantis Bot V1.0",
        "testnet": config.TESTNET,
        "target_symbols": TARGET_SYMBOLS
    }


@app.get("/open-trades")
async def get_open_trades():
    """Veritabanındaki aktif işlemleri tarayıcıda JSON formatında gösterir."""
    if not db.pool:
        return {"status": "error", "message": "Veritabanı bağlı değil."}

    query = "SELECT id, symbol, side, leverage, entry_price, size, stop_price FROM trades WHERE status = 'OPEN'"
    async with db.pool.acquire() as conn:
        records = await conn.fetch(query)
        trades = [dict(record) for record in records]

    return {"open_trades_count": len(trades), "trades": trades}


@app.get("/trade-diagnostics")
async def get_trade_diagnostics():
    """Son işlemlerden elde edilen zarar analitiğini döndürür."""
    return diagnostics_store.summarize_losses()


@app.get("/status")
async def get_status():
    """Botun canlı sağlık durumunu gösterir."""
    return {"status": "ONLINE", "strategy": "Atlantis W/ EMA", "active_symbols": TARGET_SYMBOLS}

if __name__ == "__main__":
    # Uygulamayı başlat
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
