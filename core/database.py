import asyncpg
import asyncio
import logging
import os
from core.config import config

logger = logging.getLogger(__name__)

class Database:
    """PostgreSQL bağlantı havuzunu ve işlem kayıtlarını yöneten SSOT sınıfı."""
    
    def __init__(self):
        self.pool = None

    async def connect(self):
        """Veritabanına bağlanır ve tabloları otomatik oluşturur."""
        db_url = os.getenv("DATABASE_URL")
        
        if not db_url:
            logger.warning("⚠️ DATABASE_URL bulunamadı! Veritabanı işlemleri pas geçilecek (Sadece test modu).")
            return

        delay = 1.0
        for attempt in range(3):
            try:
                self.pool = await asyncpg.create_pool(
                    dsn=db_url,
                    min_size=config.DB_POOL_MIN_SIZE,
                    max_size=config.DB_POOL_MAX_SIZE,
                    max_inactive_connection_lifetime=300,
                    command_timeout=60,
                    statement_cache_size=100
                )
                logger.info("🗄️ PostgreSQL Veritabanı bağlantısı başarıyla kuruldu.")
                await self._create_tables()
                return
            except Exception as e:
                logger.error(f"❌ Veritabanı bağlantı hatası (deneme {attempt + 1}/3): {e}")
                if attempt < 2:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 10)
                else:
                    logger.critical("❌ Veritabanına bağlanılamıyor. Bot sınırlı modda çalışacak.")
                    self.pool = None
                    return

    async def _create_tables(self):
        """İşlemlerin tutulacağı 'trades' ve 'trade_diagnostics' tablolarını oluşturur (Yoksa yaratır)."""
        query_table = """
        CREATE TABLE IF NOT EXISTS trades (
            id SERIAL PRIMARY KEY,
            symbol VARCHAR(20) NOT NULL,
            side VARCHAR(10) NOT NULL,
            leverage INT NOT NULL,
            size FLOAT NOT NULL,
            entry_price FLOAT NOT NULL,
            stop_price FLOAT,
            close_price FLOAT,
            stop_order_id VARCHAR(100),
            tp_taken BOOLEAN NOT NULL DEFAULT FALSE,
            strategy_type VARCHAR(50) NOT NULL DEFAULT 'UNKNOWN',
            status VARCHAR(20) NOT NULL DEFAULT 'OPEN',
            pnl FLOAT DEFAULT 0.0,
            reason VARCHAR(255),
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
        
        if self.pool:
            async with self.pool.acquire() as conn:
                await conn.execute(query_table)
                await conn.execute("""
                CREATE TABLE IF NOT EXISTS trade_diagnostics (
                    id SERIAL PRIMARY KEY,
                    symbol VARCHAR(20) NOT NULL,
                    side VARCHAR(10) NOT NULL,
                    pnl FLOAT NOT NULL DEFAULT 0.0,
                    reason VARCHAR(50) NOT NULL DEFAULT 'UNKNOWN',
                    adx FLOAT DEFAULT 0.0,
                    regime VARCHAR(30) DEFAULT 'UNKNOWN',
                    volume FLOAT DEFAULT 0.0,
                    vol_sma FLOAT DEFAULT 0.0,
                    atr FLOAT DEFAULT 0.0,
                    btc_trend VARCHAR(30) DEFAULT 'UNKNOWN',
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """)
                await conn.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS tp_taken BOOLEAN NOT NULL DEFAULT FALSE;")
                await conn.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS strategy_type VARCHAR(50) NOT NULL DEFAULT 'UNKNOWN';")
                try:
                    await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_symbol_open ON trades (symbol) WHERE status = 'OPEN';")
                    logger.debug("Veritabanı tabloları ve şeması kontrol edildi/güncellendi.")
                except Exception as e:
                    logger.error(f"❌ İndeks oluşturulurken hata: {e}")
                    # Eğer açık (OPEN) statüsünde aynı symbol için birden fazla kayıt varsa, indeks oluşturulamaz.
                    # Bu durumda eski/duplicate kayıtları kapatıp indeks oluşturmayı tekrar deniyoruz.
                    try:
                        duplicates_query = """
                        SELECT symbol, array_agg(id ORDER BY created_at DESC) AS ids, COUNT(*) as cnt
                        FROM trades
                        WHERE status = 'OPEN'
                        GROUP BY symbol
                        HAVING COUNT(*) > 1
                        """
                        duplicates = await conn.fetch(duplicates_query)
                        if duplicates:
                            for rec in duplicates:
                                symbol = rec['symbol']
                                ids = rec['ids']
                                # Keep the most recent (first in ordered array), close the rest
                                keep_id = ids[0]
                                remove_ids = ids[1:]
                                if remove_ids:
                                    await conn.execute(
                                        """
                                        UPDATE trades
                                        SET status = 'CLOSED', reason = 'Startup dedupe: closed duplicate open records', updated_at = CURRENT_TIMESTAMP
                                        WHERE id = ANY($1::int[])
                                        """,
                                        remove_ids,
                                    )
                                    logger.warning(f"[{symbol}] {len(remove_ids)} duplicate OPEN kayıt kapatıldı (IDs: {remove_ids})")
                        # indeks tekrar deneniyor
                        await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_symbol_open ON trades (symbol) WHERE status = 'OPEN';")
                        logger.info("✅ İndeks başarıyla oluşturuldu (duplikatlar temizlendi).")
                    except Exception as e2:
                        logger.critical(f"❌ İndeks oluşturma/duplikat temizleme başarısız: {e2}")

    async def insert_pending_trade(self, symbol: str, side: str, leverage: int, size: float, entry_price: float, stop_price: float) -> int:
        """Yeni açılan bir işlemi 'PENDING' statüsüyle veritabanına kaydeder."""
        if not self.pool:
            return None

        query = """
            INSERT INTO trades (symbol, side, leverage, size, entry_price, stop_price, status)
            VALUES ($1, $2, $3, $4, $5, $6, 'PENDING') RETURNING id
        """
        async with self.pool.acquire() as conn:
            trade_id = await conn.fetchval(query, symbol, side, leverage, size, entry_price, stop_price)
            logger.debug(f"[{symbol}] 💾 Yeni PENDING işlem kaydı oluşturuldu. (ID: {trade_id})")
            return trade_id

    async def confirm_trade(self, trade_id: int, stop_order_id: str):
        """PENDING kaydı OPEN olarak onaylar ve stop order kimliğini ekler."""
        if not self.pool:
            return

        query = """
            UPDATE trades
            SET status = 'OPEN', stop_order_id = $1, updated_at = CURRENT_TIMESTAMP
            WHERE id = $2
        """
        async with self.pool.acquire() as conn:
            await conn.execute(query, stop_order_id, trade_id)
            logger.debug(f"[trade_id={trade_id}] 💾 PENDING işlem OPEN olarak onaylandı.")

    async def mark_trade_failed(self, symbol: str, close_price: float = None, pnl: float = 0.0, reason: str = None):
        """PENDING veya başarısız işlemi FAILED statüsüyle kaydeder."""
        if not self.pool:
            return

        query = """
            UPDATE trades
            SET status = 'FAILED', close_price = $1, pnl = $2, reason = $3, updated_at = CURRENT_TIMESTAMP
            WHERE symbol = $4 AND status IN ('PENDING', 'OPEN')
        """
        async with self.pool.acquire() as conn:
            await conn.execute(query, close_price, pnl, reason, symbol)
            logger.debug(f"[{symbol}] 💾 İşlem FAILED olarak kaydedildi. Reason: {reason}")

    async def insert_trade(self, symbol: str, side: str, leverage: int, size: float, entry_price: float, stop_price: float, strategy_type: str = 'UNKNOWN') -> int:
        """Yeni açılan bir işlemi 'OPEN' (Açık) statüsüyle veritabanına kaydeder."""
        if not self.pool:
            return None
        
        query = """
            INSERT INTO trades (symbol, side, leverage, size, entry_price, stop_price, strategy_type)
            VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id
        """
        async with self.pool.acquire() as conn:
            trade_id = await conn.fetchval(query, symbol, side, leverage, size, entry_price, stop_price, strategy_type)
            logger.debug(f"[{symbol}] 💾 Yeni işlem veritabanına kaydedildi. (ID: {trade_id}, strategy_type={strategy_type})")
            return trade_id

    async def insert_trade_diagnostic(self, symbol: str, side: str, pnl: float, reason: str, adx: float, regime: str, volume: float, vol_sma: float, atr: float, btc_trend: str):
        """Zarar/stop analitiği kayıtlarını trade_diagnostics tablosuna yazar."""
        if not self.pool:
            return None

        query = """
            INSERT INTO trade_diagnostics (symbol, side, pnl, reason, adx, regime, volume, vol_sma, atr, btc_trend)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) RETURNING id
        """
        async with self.pool.acquire() as conn:
            diagnostic_id = await conn.fetchval(query, symbol, side, pnl, reason, adx, regime, volume, vol_sma, atr, btc_trend)
            logger.debug(f"[{symbol}] 💾 Trade diagnostic kaydı veritabanına yazıldı. (ID: {diagnostic_id})")
            return diagnostic_id

    async def close_trade(self, symbol: str, close_price: float, pnl: float):
        """Açık olan işlemi bulur, 'CLOSED' olarak işaretler ve Kâr/Zarar (PnL) ile kapanış fiyatını yazar."""
        if not self.pool: return
        
        query = """
            UPDATE trades 
            SET status = 'CLOSED', pnl = COALESCE(pnl, 0.0) + $1, close_price = $2, updated_at = CURRENT_TIMESTAMP
            WHERE symbol = $3 AND status = 'OPEN'
        """
        async with self.pool.acquire() as conn:
            await conn.execute(query, pnl, close_price, symbol)
            logger.debug(f"[{symbol}] 💾 İşlem veritabanında kapatıldı. (Fiyat: {close_price:.4f}, PnL: {pnl:.2f} USDT)")

    async def update_trade_after_partial_close(self, symbol: str, pnl_delta: float, remaining_size: float):
        """Kısmi kapatma sonrası kalan boyutu ve kümülatif PnL'yi günceller."""
        if not self.pool: return

        query = """
            UPDATE trades
            SET size = $1, pnl = COALESCE(pnl, 0.0) + $2, updated_at = CURRENT_TIMESTAMP
            WHERE symbol = $3 AND status = 'OPEN'
        """
        async with self.pool.acquire() as conn:
            await conn.execute(query, remaining_size, pnl_delta, symbol)
            logger.debug(f"[{symbol}] 💾 Kısmi kapatma sonrası kalan boyut ve PnL güncellendi. (Kalan: {remaining_size:.4f}, PnL delta: {pnl_delta:.2f})")

    async def get_open_trade(self, symbol: str) -> dict:
        """Belirli bir sembol için açık işlem olup olmadığını kontrol eder."""
        if not self.pool: return None
        
        query = "SELECT id, side, size, entry_price, stop_price, stop_order_id, tp_taken, strategy_type FROM trades WHERE symbol = $1 AND status = 'OPEN' LIMIT 1"
        async with self.pool.acquire() as conn:
            record = await conn.fetchrow(query, symbol)
            return dict(record) if record else None

    async def get_all_open_trades(self) -> list:
        """Veritabanındaki tüm açık işlemleri getirir (Self-Healing için)."""
        if not self.pool: return []
        
        query = "SELECT id, symbol, side, leverage, size, entry_price, stop_price, stop_order_id, tp_taken, strategy_type FROM trades WHERE status = 'OPEN'"
        async with self.pool.acquire() as conn:
            records = await conn.fetch(query)
            return [dict(record) for record in records]

    async def update_trade_stop(self, symbol: str, new_stop_price: float, stop_order_id: str = None, tp_taken: bool = None):
        """Açık işlemin stop loss fiyatını, order ID'sini ve TP durumunu günceller."""
        if not self.pool: return
        
        if stop_order_id is not None and tp_taken is not None:
            query = """
                UPDATE trades 
                SET stop_price = $1, stop_order_id = $2, tp_taken = $3, updated_at = CURRENT_TIMESTAMP
                WHERE symbol = $4 AND status = 'OPEN'
            """
            async with self.pool.acquire() as conn:
                await conn.execute(query, new_stop_price, stop_order_id, tp_taken, symbol)
                logger.debug(f"[{symbol}] 💾 Stop loss ve TP durumu güncellendi: {new_stop_price:.4f}, tp_taken={tp_taken}")
        elif stop_order_id is not None:
            query = """
                UPDATE trades 
                SET stop_price = $1, stop_order_id = $2, updated_at = CURRENT_TIMESTAMP
                WHERE symbol = $3 AND status = 'OPEN'
            """
            async with self.pool.acquire() as conn:
                await conn.execute(query, new_stop_price, stop_order_id, symbol)
                logger.debug(f"[{symbol}] 💾 Stop loss güncellendi: {new_stop_price:.4f} (Order ID: {stop_order_id})")
        elif tp_taken is not None:
            query = """
                UPDATE trades 
                SET tp_taken = $1, updated_at = CURRENT_TIMESTAMP
                WHERE symbol = $2 AND status = 'OPEN'
            """
            async with self.pool.acquire() as conn:
                await conn.execute(query, tp_taken, symbol)
                logger.debug(f"[{symbol}] 💾 TP durumu güncellendi: tp_taken={tp_taken}")
        else:
            query = """
                UPDATE trades 
                SET stop_price = $1, updated_at = CURRENT_TIMESTAMP
                WHERE symbol = $2 AND status = 'OPEN'
            """
            async with self.pool.acquire() as conn:
                await conn.execute(query, new_stop_price, symbol)
                logger.debug(f"[{symbol}] 💾 Stop loss fiyatı güncellendi: {new_stop_price:.4f}")
    
    async def close(self):
        """Bağlantı havuzunu güvenle kapatır."""
        if self.pool:
            await self.pool.close()
            logger.info("🗄️ Veritabanı bağlantısı güvenle kapatıldı.")

db = Database()
