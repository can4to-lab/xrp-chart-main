import logging
import asyncio
import pandas as pd
from exchange.binance_async import BinanceFuturesClient
from core.database import db
from core.notifier import notifier

logger = logging.getLogger(__name__)

class ExecutionEngine:
    """Stratejiden gelen sinyalleri borsada çalıştıran ve DB/Telegram'a bildiren katman."""

    def __init__(self, client: BinanceFuturesClient):
        self.client = client
        self.exchange = client.exchange

    def _to_ccxt_symbol(self, symbol: str) -> str:
        """'SOLUSDT' -> 'SOL/USDT' dönüşümü. CCXT bu formatta bekler."""
        if '/' not in symbol:
            # USDT, BUSD, USDC, BTC, ETH ile biten sembollerde / işareti ekle
            for quote in ['USDT', 'BUSD', 'USDC', 'BTC', 'ETH', 'BNB']:
                if symbol.endswith(quote) and len(symbol) > len(quote):
                    base = symbol[:-len(quote)]
                    return f"{base}/{quote}"
        return symbol

    async def execute_trade(self, symbol: str, side: str, margin_usdt: float, leverage: int, entry_price: float, stop_price: float) -> bool:
        try:
            sym_ccxt = self._to_ccxt_symbol(symbol)
            is_setup = await self.client.setup_margin_and_leverage(sym_ccxt, leverage)
            if not is_setup: return False

            await self.exchange.load_markets()
            
            raw_size = (margin_usdt * leverage) / entry_price
            formatted_size = float(self.exchange.amount_to_precision(sym_ccxt, raw_size))
            
            if (formatted_size * entry_price) < 5.0:
                logger.warning(f"[{symbol}] İşlem hacmi < 5 USDT! İptal.")
                return False

            order_side = 'buy' if side == 'LONG' else 'sell'
            stop_side = 'sell' if side == 'LONG' else 'buy'

            logger.info(f"[{symbol}] {side} Emir Gönderiliyor... Büyüklük: {formatted_size}")

            # 1. BORSAYA GİRİŞ EMRİNİ GÖNDER
            entry_order = await self.exchange.create_order(
                symbol=sym_ccxt, type='market', side=order_side, amount=formatted_size
            )
            
            actual_entry_price = float(entry_order.get('average') or entry_order.get('price') or entry_price)
            actual_filled_size = float(entry_order.get('filled') or formatted_size)
            formatted_stop_price = float(self.exchange.price_to_precision(sym_ccxt, stop_price))
            
            # 2. BORSAYA STOP LOSS EMRİNİ GÖNDER (VE GÜVENLİK KONTROLÜ)
            try:
                await self.exchange.create_order(
                    symbol=sym_ccxt, type='stop_market', side=stop_side, amount=actual_filled_size,
                    params={'stopPrice': formatted_stop_price, 'reduceOnly': True}
                )
                logger.info(f"[{symbol}] ✅ İşleme Girildi ve Stop Loss Yerleştirildi.")
            except Exception as sl_err:
                logger.critical(f"[{symbol}] ⚠️ Stop Loss emri başarısız! Pozisyon ACİL kapatılıyor (Rollback). Hata: {sl_err}")
                # Rollback: Açılan pozisyonu kapatmak için acil ters market emri gönderiyoruz
                await self.exchange.create_order(
                    symbol=sym_ccxt, type='market', side=stop_side, amount=actual_filled_size,
                    params={'reduceOnly': True}
                )
                return False
            
            # 3. VERİTABANINA KAYDET
            await db.insert_trade(
                symbol=symbol, side=side, leverage=leverage, 
                size=actual_filled_size, entry_price=actual_entry_price, stop_price=formatted_stop_price
            )

            # 4. TELEGRAM'A BİLDİR
            alert_msg = (
                f"🟢 <b>YENİ İŞLEM AÇILDI</b>\n\n"
                f"📌 <b>Parite:</b> {symbol}\n"
                f"🎯 <b>Yön:</b> {side}\n"
                f"💰 <b>Giriş Fiyatı:</b> {actual_entry_price}\n"
                f"🛡️ <b>Stop Loss:</b> {formatted_stop_price}\n"
                f"⚖️ <b>Büyüklük:</b> {actual_filled_size} ({leverage}x)"
            )
            await notifier.send_message(alert_msg)

            return True

        except Exception as e:
            logger.error(f"[{symbol}] İşlem hatası: {str(e)}")
            return False

    async def close_position(self, symbol: str, side: str, size: float, reason: str):
        try:
            sym_ccxt = self._to_ccxt_symbol(symbol)
            # Önce DB'den açık pozisyon bilgilerini alalım (PnL hesaplayabilmek için)
            open_trade = await db.get_open_trade(symbol)
            
            close_side = 'sell' if side == 'LONG' else 'buy'
            formatted_size = float(self.exchange.amount_to_precision(sym_ccxt, size))

            # 1. Borsadan Pozisyonu Kapat
            close_order = await self.exchange.create_order(
                symbol=sym_ccxt, type='market', side=close_side, amount=formatted_size,
                params={'reduceOnly': True}
            )
            
            # 2. Borsadaki o sembole ait diğer tüm emirleri (Örn: Yetim Stop Loss) iptal et
            try:
                await self.exchange.cancel_all_orders(sym_ccxt)
                logger.info(f"[{symbol}] Borsadaki tüm aktif emirler ve stop loss'lar temizlendi.")
            except Exception as cancel_err:
                logger.warning(f"[{symbol}] Bekleyen emirler iptal edilirken uyarı: {cancel_err}")
            
            # 3. Kapanış fiyatını ve PnL değerini hesapla
            actual_close_price = float(close_order.get('average') or close_order.get('price') or 0.0)
            if actual_close_price == 0.0:
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

            # 4. Veritabanında Kapat
            await db.close_trade(symbol, close_price=actual_close_price, pnl=pnl)
            
            # 5. Telegram'a Bildir
            pnl_icon = "🟢" if pnl >= 0 else "🔴"
            alert_msg = (
                f"⚠️ <b>İŞLEM KAPATILDI</b>\n\n"
                f"📌 <b>Parite:</b> {symbol}\n"
                f"ℹ️ <b>Sebep:</b> {reason}\n"
                f"💰 <b>Kapanış Fiyatı:</b> {actual_close_price:.4f}\n"
                f"{pnl_icon} <b>Net PnL:</b> {pnl:+.2f} USDT"
            )
            await notifier.send_message(alert_msg)
            logger.info(f"[{symbol}] Kapatıldı. Sebep: {reason}. Fiyat: {actual_close_price:.4f}, PnL: {pnl:.2f} USDT")
            
        except Exception as e:
            logger.error(f"[{symbol}] Kapatma hatası: {str(e)}")

    async def move_stop_to_breakeven(self, symbol: str, side: str, entry_price: float):
        """
        Stop loss'u giriş fiyatına (break-even) taşır.
        Order ID tabanlı güvenli yönetim ve rollback mekanizması ile.
        """
        try:
            sym_ccxt = self._to_ccxt_symbol(symbol)
            # 1. Açık pozisyonu kontrol et
            open_trade = await db.get_open_trade(symbol)
            if not open_trade:
                logger.warning(f"[{symbol}] Break-even stop için açık pozisyon bulunamadı.")
                return False

            position_size = open_trade['size']
            old_stop_price = open_trade.get('stop_price')
            old_stop_order_id = open_trade.get('stop_order_id')
            
            # 2. Binance'daki aktif stop emirlerini bul
            open_orders = await self.exchange.fetch_open_orders(sym_ccxt)
            stop_orders = [order for order in open_orders if order['type'] == 'stop_market']
            
            # 3. Eki stop emirlerini teker teker iptal et
            cancelled_order_ids = []
            for order in stop_orders:
                try:
                    await self.exchange.cancel_order(order['id'], sym_ccxt)
                    cancelled_order_ids.append(order['id'])
                    logger.info(f"[{symbol}] Eski stop emri iptal edildi: {order['id']}")
                except Exception as cancel_err:
                    logger.warning(f"[{symbol}] Stop emri {order['id']} iptal edilirken hata: {cancel_err}")
                    # İptal edilemeyen emirleri logla ama devam et

            # 4. Yeni break-even stop fiyatını hesapla (ATR bazlı)
            atr = await self._get_atr(sym_ccxt, period=14)
            if atr <= 0:
                logger.error(f"[{symbol}] ATR hesaplanamadı, break-even stop kurulamıyor.")
                return False
            
            # ATR'ye göre break-even stop (entry'ye yakın ama volatiliteye uygun)
            atr_multiplier = 0.5  # Break-even için orta seviye stop
            ticker = await self.exchange.fetch_ticker(sym_ccxt)
            current_price = float(ticker.get('last', entry_price))
            
            if side == 'LONG':
                breakeven_stop = entry_price - (atr * atr_multiplier)
                # Minimum fiyat kontrolü (entry'den %3 altına düşme)
                breakeven_stop = max(breakeven_stop, entry_price * 0.97)
            else:
                breakeven_stop = entry_price + (atr * atr_multiplier)
                # Maksimum fiyat kontrolü (entry'den %3 üstüne çıkma)
                breakeven_stop = min(breakeven_stop, entry_price * 1.03)
            
            formatted_breakeven_stop = float(self.exchange.price_to_precision(sym_ccxt, breakeven_stop))
            stop_side = 'sell' if side == 'LONG' else 'buy'

            # 5. Yeni break-even stop emrini gönder
            new_stop_order_id = None
            try:
                new_stop_order = await self.exchange.create_order(
                    symbol=sym_ccxt, 
                    type='stop_market', 
                    side=stop_side, 
                    amount=position_size,
                    params={
                        'stopPrice': formatted_breakeven_stop, 
                        'reduceOnly': True,
                        'timeInForce': 'GTC'  # Good Till Cancel
                    }
                )
                new_stop_order_id = new_stop_order.get('id')
                logger.info(f"[{symbol}] ✅ Yeni break-even stop emri gönderildi: {formatted_breakeven_stop} (Order ID: {new_stop_order_id})")
                
            except Exception as new_stop_err:
                logger.critical(f"[{symbol}] ⚠️ Yeni stop emri başarısız! Rollback gerekli. Hata: {new_stop_err}")
                
                # ROLLBACK: Eski stop'u geri yüklemeye çalış
                if old_stop_price and old_stop_order_id:
                    try:
                        # Eski stop emri zaten iptal edildi, yenisini oluştur
                        await self.exchange.create_order(
                            symbol=sym_ccxt,
                            type='stop_market',
                            side=stop_side,
                            amount=position_size,
                            params={
                                'stopPrice': old_stop_price,
                                'reduceOnly': True
                            }
                        )
                        logger.warning(f"[{symbol}] 🔄 Rollback: Eski stop geri yüklendi: {old_stop_price}")
                    except Exception as rollback_err:
                        logger.critical(f"[{symbol}] 🚨 Rollback başarısız! Manuel müdahale gerekli: {rollback_err}")
                        await notifier.send_message(
                            f"🚨 <b>ACİL DURUM:</b> {symbol} için stop loss yok!\n"
                            f"Yön: {side}\n"
                            f"Boyut: {position_size}\n"
                            f"MANUEL MÜDAHALE GEREKLİ!"
                        )
                
                return False

            # 6. Veritabanını güncelle (yeni stop fiyatı ve order ID)
            await db.update_trade_stop(symbol, formatted_breakeven_stop, new_stop_order_id)
            
            # 7. Telegram'a bildir
            alert_msg = (
                f"🛡️ <b>STOP LOSS BREAK-EVEN'A ÇEKİLDİ</b>\n\n"
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
        """ATR değerini hesaplar (yardımcı fonksiyon)."""
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
            logger.error(f"[{symbol}] ATR hesaplama hatası: {e}")
            return 0.0
