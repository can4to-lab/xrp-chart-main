import logging
import asyncio
import pandas as pd
from exchange.binance_async import BinanceFuturesClient
from core.database import db
from core.notifier import notifier

logger = logging.getLogger(__name__)

class ExecutionEngine:
    """Stratejiden gelen sinyalleri borsada calistiran ve DB/Telegram'a bildiren katman."""

    def __init__(self, client: BinanceFuturesClient):
        self.client = client
        self.exchange = client.exchange

    def _to_ccxt_symbol(self, symbol: str) -> str:
        """'SOLUSDT' -> 'SOL/USDT' donusumu. CCXT bu formatta bekler."""
        if '/' not in symbol:
            # USDT, BUSD, USDC, BTC, ETH ile biten sembollerde / isareti ekle
            for quote in ['USDT', 'BUSD', 'USDC', 'BTC', 'ETH', 'BNB']:
                if symbol.endswith(quote) and len(symbol) > len(quote):
                    base = symbol[:-len(quote)]
                    return f"{base}/{quote}"
        return symbol

    async def execute_trade(self, symbol: str, side: str, margin_usdt: float, leverage: int, entry_price: float, stop_price: float, strategy_type: str = 'UNKNOWN') -> bool:
        try:
            sym_ccxt = self._to_ccxt_symbol(symbol)
            is_setup = await self.client.setup_margin_and_leverage(sym_ccxt, leverage)
            if not is_setup: return False

            await self.exchange.load_markets()
            
            raw_size = (margin_usdt * leverage) / entry_price
            formatted_size = float(self.exchange.amount_to_precision(sym_ccxt, raw_size))
            
            if (formatted_size * entry_price) < 5.0:
                logger.warning(f"[{symbol}] Islem hacmi < 5 USDT! Iptal.")
                return False

            order_side = 'buy' if side == 'LONG' else 'sell'
            stop_side = 'sell' if side == 'LONG' else 'buy'

            logger.info(f"[{symbol}] {side} Emir Gonderiliyor... Buyukluk: {formatted_size}")

            # 1. BORSAYA GIRIS EMRINI GONDER
            entry_order = await self.exchange.create_order(
                symbol=sym_ccxt, type='market', side=order_side, amount=formatted_size
            )
            
            actual_entry_price = float(entry_order.get('average') or entry_order.get('price') or entry_price)
            actual_filled_size = float(entry_order.get('filled') or formatted_size)
            formatted_stop_price = float(self.exchange.price_to_precision(sym_ccxt, stop_price))
            
            # 2. BORSAYA STOP LOSS EMRINI GONDER (VE GUVENLIK KONTROLU)
            # Once mevcut acik stop emirlerini iptal et (limit hatasini onlemek icin)
            try:
                await self.exchange.cancel_all_orders(sym_ccxt)
                logger.info(f"[{symbol}] Gecmis emirler iptal edildi, yeni islem basliyor.")
            except Exception as cancel_err:
                logger.warning(f"[{symbol}] Emirler iptal edilirken uyari: {cancel_err}")

            # 3. VERITABANINA KAYDET (Stop emrinden ONCE kaydet - islem havada kalmasin)
            trade_id = await db.insert_trade(
                symbol=symbol, side=side, leverage=leverage, 
                size=actual_filled_size, entry_price=actual_entry_price, stop_price=formatted_stop_price,
                strategy_type=strategy_type
            )
            if trade_id:
                logger.info(f"[{symbol}] ✅ Islem DB'ye kaydedildi. (ID: {trade_id})")
            else:
                logger.warning(f"[{symbol}] ⚠️ Islem DB'ye kaydedilemedi - DATABASE_URL veya bağlantı yok.")

            try:
                stop_order = await self.exchange.create_order(
                    symbol=sym_ccxt, type='stop_market', side=stop_side, amount=actual_filled_size,
                    params={'stopPrice': formatted_stop_price, 'triggerType': '4', 'reduceOnly': True}
                )
                stop_order_id = stop_order.get('id')
                logger.info(f"[{symbol}] ✅ Isleme Girildi ve Stop Loss Yerelendi. (Order ID: {stop_order_id})")
                
                # Stop emri basarili olunca order ID'yi DB'ye kaydet
                if stop_order_id:
                    if getattr(db, 'pool', None):
                        await db.update_trade_stop(symbol, formatted_stop_price, stop_order_id)
                        logger.info(f"[{symbol}] ✅ Stop emri DB'ye kaydedildi.")
                    else:
                        logger.warning(f"[{symbol}] ⚠️ Stop emri DB'ye kaydedilemedi - DATABASE_URL veya bağlantı yok.")
                
            except Exception as sl_err:
                logger.critical(f"[{symbol}] ⚠️ Stop Loss emri basarisiz! Pozisyon ACIL kapatiliyor (Rollback). Hata: {sl_err}")
                # Rollback: Açilan pozisyonu kapatmak için acil ters market emri gonderiyoruz
                # reduceOnly olmadan dene, cünkü pozisyon kontrolü gerekir
                try:
                    # Once pozisyonu kontrol et
                    positions = await self.exchange.fetch_positions([sym_ccxt])
                    current_position = None
                    for pos in positions:
                        if pos['symbol'] == sym_ccxt:
                            current_position = pos
                            break
                    
                    pos_contracts = 0.0
                    if current_position:
                        pos_contracts = float(current_position.get('contracts', 0.0) or current_position.get('positionAmt', 0.0) or 0.0)
                    
                    if abs(pos_contracts) > 0:
                        # Pozisyon var, kapat
                        await self.exchange.create_order(
                            symbol=sym_ccxt, type='market', side=stop_side, amount=actual_filled_size
                        )
                        logger.info(f"[{symbol}] Rollback: Pozisyon kapatildi.")
                    else:
                        logger.warning(f"[{symbol}] Rollback: Pozisyon zaten kapali.")
                
                except Exception as rollback_err:
                    logger.error(f"[{symbol}] Rollback hatasi: {rollback_err}")
                # DB'de islemi kapatildi olarak isaretle
                try:
                    ticker = await self.exchange.fetch_ticker(sym_ccxt)
                    close_price = float(ticker.get('last', 0.0))
                    await db.close_trade(symbol, close_price=close_price, pnl=0.0)
                    logger.info(f"[{symbol}] Rollback sonrasi DB kaydi guncellendi.")
                except Exception as db_err:
                    logger.error(f"[{symbol}] DB guncelleme hatasi: {db_err}")
                
                return False

            # 4. TELEGRAM'A BILDIR
            alert_msg = (
                f"🟢 <b>YENI ISLEM ACILDI</b>\n\n"
                f"📌 <b>Parite:</b> {symbol}\n"
                f"🎯 <b>Yon:</b> {side}\n"
                f"💰 <b>Giris Fiyati:</b> {actual_entry_price}\n"
                f"🛡️ <b>Stop Loss:</b> {formatted_stop_price}\n"
                f"⚖️ <b>Buyukluk:</b> {actual_filled_size} ({leverage}x)"
            )
            await notifier.send_message(alert_msg)

            return True

        except Exception as e:
            logger.error(f"[{symbol}] Islem hatasi: {str(e)}")
            return False

    async def close_position(self, symbol: str, side: str, size: float, reason: str):
        try:
            sym_ccxt = self._to_ccxt_symbol(symbol)
            # Once DB'den acik pozisyon bilgilerini alalim (PnL hesaplayabilmek icin)
            open_trade = await db.get_open_trade(symbol)
            
            close_side = 'sell' if side == 'LONG' else 'buy'
            formatted_size = float(self.exchange.amount_to_precision(sym_ccxt, size))

            # 1. Borsadan Pozisyonu Kapat
            # Once pozisyonu kontrol et - reduceOnly kullanirken pozisyonun varligi kontrolu gerekir
            try:
                positions = await self.exchange.fetch_positions([sym_ccxt])
                current_position = None
                for pos in positions:
                    if pos['symbol'] == sym_ccxt:
                        current_position = pos
                        break
                
                pos_contracts = 0.0
                if current_position:
                    pos_contracts = float(current_position.get('contracts', 0.0) or current_position.get('positionAmt', 0.0) or 0.0)
                
                if abs(pos_contracts) > 0:
                    # Pozisyon var, kapat
                    close_order = await self.exchange.create_order(
                        symbol=sym_ccxt, type='market', side=close_side, amount=formatted_size,
                        params={'reduceOnly': True}
                    )
                else:
                    # Pozisyon yok, sadece emirleri iptal et ve DB'yi senkronize etmeye devam et
                    logger.warning(f"[{symbol}] close_position: Pozisyon zaten kapali, sadece emirler iptal ediliyor ve DB senkronize ediliyor.")
                    await self.exchange.cancel_all_orders(sym_ccxt)
                    close_order = {}  # RETURN YOK! Aşağıdaki DB close_trade satırlarına akmaya devam etmeli.
            except Exception as pos_err:
                logger.error(f"[{symbol}] Pozisyon kontrol hatasi: {pos_err}")
                return
            
            # 2. Borsadaki o sembole ait diğer tüm emirleri (Örn: Yetim Stop Loss) iptal et
            try:
                await self.exchange.cancel_all_orders(sym_ccxt)
                logger.info(f"[{symbol}] Borsadaki tum aktif emirler ve stop loss'lar temizlendi.")
            except Exception as cancel_err:
                logger.warning(f"[{symbol}] Bekleyen emirler iptal edilirken uyari: {cancel_err}")
            
            # 3. Kapanis fiyatini ve PnL degerini hesapla
            actual_close_price = float(close_order.get('average') or close_order.get('price') or None)
            if not actual_close_price or actual_close_price == 0.0:
                ticker = await self.exchange.fetch_ticker(sym_ccxt)
                actual_close_price = float(ticker.get('last', 0.0))

            pnl = 0.0
            if open_trade:
                entry_price = open_trade['entry_price']
                trade_size = open_trade['size']
                if side == 'LONG':
                    pnl = (actual_close_price - entry_price) * trade_size
                else:
                    pnl = (entry_price - actual_close_price) * trade_size

            # 4. Kısmi kapatma kontrolü - Eğer kısmi kapatma ise DB'deki boyutu güncelle
            if open_trade and abs(size) < abs(open_trade['size']):
                # Kısmi kapatma - kalan boyutu DB'ye yaz
                remaining_size = abs(open_trade['size']) - abs(size)
                logger.info(f"[{symbol}] Kısmi kapatma: {abs(size)} satıldı, kalan: {remaining_size}")
                # DB'deki boyutu güncelle (update_trade_stop kullanarak)
                await db.update_trade_stop(symbol, open_trade['stop_price'], open_trade.get('stop_order_id'))
                # Boyutu güncellemek için özel sorgu
                if open_trade.get('stop_order_id') and getattr(db, 'pool', None):
                    query = "UPDATE trades SET size = $1 WHERE symbol = $2 AND status = 'OPEN'"
                    async with db.pool.acquire() as conn:
                        await conn.execute(query, remaining_size, symbol)
                    logger.info(f"[{symbol}] ✅ Kalan pozisyon boyutu DB'ye kaydedildi: {remaining_size}")
                else:
                    logger.warning(f"[{symbol}] ⚠️ Kalan pozisyon boyutu DB'ye kaydedilemedi - DATABASE_URL veya bağlantı yok.")
                
                # Telegram'a kısmi kapatma bildirimi
                partial_msg = (
                    f"💰 <b>KISMI KAPATMA</b>\n\n"
                    f"📌 <b>Parite:</b> {symbol}\n"
                    f"🎯 <b>Yon:</b> {side}\n"
                    f"💵 <b>Kapanis Fiyati:</b> {actual_close_price:.4f}\n"
                    f"📊 <b>Kapatilan:</b> {abs(size):.4f} / Kalan: {remaining_size:.4f}\n"
                    f"💚 <b>PnL:</b> {pnl:+.2f} USDT"
                )
                await notifier.send_message(partial_msg)
                return
            
            # 5. Tam kapatma - DB'de kapat
            await db.close_trade(symbol, close_price=actual_close_price, pnl=pnl)
            
            # 6. Telegram'a Bildir
            pnl_icon = "🟢" if pnl >= 0 else "🔴"
            alert_msg = (
                f"⚠️ <b>ISLEM KAPATILDI</b>\n\n"
                f"📌 <b>Parite:</b> {symbol}\n"
                f"ℹ️ <b>Sebep:</b> {reason}\n"
                f"💰 <b>Kapanis Fiyati:</b> {actual_close_price:.4f}\n"
                f"{pnl_icon} <b>Net PnL:</b> {pnl:+.2f} USDT"
            )
            await notifier.send_message(alert_msg)
            logger.info(f"[{symbol}] Kapatildi. Sebep: {reason}. Fiyat: {actual_close_price:.4f}, PnL: {pnl:.2f} USDT")
            
        except Exception as e:
            logger.error(f"[{symbol}] Kapatma hatasi: {str(e)}")

    async def move_stop_to_breakeven(self, symbol: str, side: str, entry_price: float):
        """
        Stop loss'u giriş fiyatına (break-even) taşır.
        ATOMİK (Cancel & Replace) metodu ile milisaniyelik riskleri (naked window) sıfırlar.
        """
        try:
            sym_ccxt = self._to_ccxt_symbol(symbol)
            
            # 1. Açık pozisyonu kontrol et ve ESKİ STOP ORDER ID'sini DB'den çek
            open_trade = await db.get_open_trade(symbol)
            if not open_trade:
                logger.warning(f"[{symbol}] Break-even stop için açık pozisyon bulunamadı.")
                return False

            position_size = open_trade['size']
            old_stop_order_id = open_trade.get('stop_order_id')
            
            if not old_stop_order_id:
                logger.error(f"[{symbol}] DB'de stop_order_id bulunamadı! Atomik güncelleme yapılamaz.")
                return False

            # 2. Yeni break-even stop fiyatını hesapla (ATR bazlı)
            atr = await self._get_atr(sym_ccxt, period=14)
            if atr <= 0:
                logger.error(f"[{symbol}] ATR hesaplanamadı, break-even stop kurulamıyor.")
                return False
            
            # ATR'ye gore break-even stop (entry'ye yakin ama volatiliteye uygun)
            atr_multiplier = 0.5  # Break-even icin orta seviye stop
            ticker = await self.exchange.fetch_ticker(sym_ccxt)
            current_price = float(ticker.get('last', entry_price))
            
            if side == 'LONG':
                breakeven_stop = entry_price - (atr * atr_multiplier)
                # Minimum fiyat kontrolü (entry'den %3 altina düsme)
                breakeven_stop = max(breakeven_stop, entry_price * 0.97)
            else:
                breakeven_stop = entry_price + (atr * atr_multiplier)
                # Maksimum fiyat kontrolü (entry'den %3 ustune cikma)
                breakeven_stop = min(breakeven_stop, entry_price * 1.03)
            
            formatted_breakeven_stop = float(self.exchange.price_to_precision(sym_ccxt, breakeven_stop))
            stop_side = 'sell' if side == 'LONG' else 'buy'

            # 3. ATOMİK GÜNCELLEME (Cancel & Replace) - Eski 3. ve 5. adımların birleşimi
            new_stop_order_id = None
            try:
                # CCXT edit_order Binance'te PUT /fapi/v1/order endpoint'ini tetikler
                updated_order = await self.exchange.edit_order(
                    id=old_stop_order_id,
                    symbol=sym_ccxt,
                    type='stop_market',
                    side=stop_side,
                    amount=position_size,
                    params={
                        'stopPrice': formatted_breakeven_stop,
                        'triggerType': '4',
                        'reduceOnly': True
                    }
                )
                
                # Binance düzenlenmiş emre yeni bir ID atar, onu almalıyız
                new_stop_order_id = updated_order.get('id')
                logger.info(f"[{symbol}] ✅ Atomik Güncelleme Başarılı! Eski Stop: {old_stop_order_id} -> Yeni Stop: {formatted_breakeven_stop} (Yeni ID: {new_stop_order_id})")
                
            except Exception as edit_err:
                # Atomik işlem başarısız olduysa ESKİ EMİR HALA GEÇERLİDİR. 
                # Rollback (Geri sarma) kodlarına artık gerek yok! Çıplak (naked) kalmadık.
                logger.error(f"[{symbol}] ⚠️ Atomik Cancel&Replace başarısız oldu! Eski stop emri ({old_stop_order_id}) hala devrede koruma sağlıyor. Hata: {edit_err}")
                return False

            # 4. Veritabanını güncelle (yeni stop fiyatı ve YENİ order ID ile)
            await db.update_trade_stop(symbol, formatted_breakeven_stop, new_stop_order_id)
            
            # 5. Telegram'a bildir
            alert_msg = (
                f"🛡️ <b>STOP LOSS BREAK-EVEN'A ÇEKİLDİ (ATOMİK)</b>\n\n"
                f"📌 <b>Parite:</b> {symbol}\n"
                f"🎯 <b>Yön:</b> {side}\n"
                f"💰 <b>Giriş Fiyatı:</b> {entry_price:.4f}\n"
                f"🛡️ <b>Yeni Stop:</b> {formatted_breakeven_stop:.4f}\n"
                f"✅ <b>Durum:</b> Risksiz sürüş (Free Ride) aktif!"
            )
            await notifier.send_message(alert_msg)
            
            return True

        except Exception as e:
            logger.error(f"[{symbol}] Break-even stop ayarlama hatası: {str(e)}")
            return False

    async def _get_atr(self, symbol: str, period: int = 14) -> float:
        """ATR degerini hesaplar (yardimci fonksiyon)."""
        try:
            ohlcv = await self.exchange.fetch_ohlcv(symbol, timeframe='1h', limit=period + 1)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            
            high = df['high']
            low = df['low']
            prev_close = df['close'].shift(1)
            
            tr1 = high - low
            tr2 = (high - prev_close).abs()
            tr3 = (low - prev_close).abs()
            tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
            
            atr = tr.ewm(alpha=1/period, adjust=False).mean().iloc[-1]
            return float(atr)
        except Exception as e:
            logger.error(f"[{symbol}] ATR hesaplama hatasi: {e}")
            return 0.0