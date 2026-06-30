import asyncio
import logging
import pandas as pd
from typing import List, Dict, Optional

from strategy.atlantis_math import AtlantisIndicator
from core.config import config
from core.database import db  # Veritabanını beynimize bağladık
from core.state import state  # Kilitler için ekledik

logger = logging.getLogger(__name__)


class TradeState:
    """Her sembol için işlem durumunu hafızada tutan sınıf."""

    def __init__(self):
        self._state: Dict[str, Dict] = {}

    def get_state(self, symbol: str) -> Dict:
        """Sembol için işlem durumunu döndürür, yoksa oluşturur."""
        if symbol not in self._state:
            self._state[symbol] = {
                'in_position': False,
                'side': None,
                'entry_price': 0.0,
                'size': 0.0,
                'tp_taken': False,  # %50 kâr alındı bayrağı
            }
        return self._state[symbol]

    def reset_for_new_trade(self, symbol: str):
        """Yeni işlem için durumu sıfırlar."""
        self._state[symbol] = {
            'in_position': False,
            'side': None,
            'entry_price': 0.0,
            'size': 0.0,
            'tp_taken': False,
        }

    def set_tp_taken(self, symbol: str):
        """TP sinyali işlendiğinde bayrağı True yapar."""
        if symbol in self._state:
            self._state[symbol]['tp_taken'] = True


class AtlantisStrategyRunner:
    """Tam otonom Atlantis tarayıcısı: Çift işlem korumalı ve Çıkış kontrollü."""

    def __init__(self, symbols: List[str], execution_engine, risk_manager, client, timeframe: str = '5m'):
        self.symbols = [s.replace("/", "").lower() for s in symbols]
        self.execution_engine = execution_engine
        self.risk_manager = risk_manager
        self.client = client
        self.timeframe = timeframe
        self._running = False
        self.indicator = AtlantisIndicator()
        self.trade_state = TradeState()  # Durum hafızası
        logger.info("🛠️ Atlantis İndikatör Matematiği ve Durum Hafızası belleğe yüklendi.")

    async def _scan_market_for_symbol(self, symbol: str):
        sym_upper = symbol.upper()
        lock = state.get_symbol_lock(sym_upper) # Sembole özel kilit alınıyor
        logger.info(f"[{sym_upper}] 🔍 {self.timeframe} periyotlu piyasa taraması başlatıldı.")

        while self._running:
            try:
                # 1. Veri Çekme Aşaması (Lock Dışında - API istekleri kilidi bloklamasın)
                ohlcv = await self.client.exchange.fetch_ohlcv(sym_upper, timeframe=self.timeframe, limit=200)

                if not ohlcv or len(ohlcv) < 150:
                    await asyncio.sleep(5)
                    continue
                               # 2. DataFrame Çevirimi ve Hesaplama (Lock Dışında)
                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                df_signals = self.indicator.get_signals(df)

                last_closed_candle = df_signals.iloc[-2]
                current_price = df_signals.iloc[-1]['close']

                is_3_green = bool(last_closed_candle['is_3_green'])
                is_3_red = bool(last_closed_candle['is_3_red'])
                long_signal = bool(last_closed_candle['long_signal'])
                short_signal = bool(last_closed_candle['short_signal'])
                long_exit = bool(last_closed_candle['long_exit'])
                short_exit = bool(last_closed_candle['short_exit'])
                long_tp_signal = bool(last_closed_candle['long_tp_signal'])
                short_tp_signal = bool(last_closed_candle['short_tp_signal'])
                atr_stop_dist = float(last_closed_candle['atr_stop_dist'])

                # --- 🔍 DETAYLI TREND ANALİZİ ---
                # Hareketli ortalama değerlerini alıyoruz
                fast = last_closed_candle['fastTBO']
                medium = last_closed_candle['mediumTBO']
                medfast = last_closed_candle['medfastTBO']
                slow = last_closed_candle['slowTBO']

                # LONG (3 Yeşil) Uyum Şartları: Fast > Medium > MedFast > Slow
                l1 = "✅" if fast > medium else "❌"
                l2 = "✅" if medium > medfast else "❌"
                l3 = "✅" if medfast > slow else "❌"

                # SHORT (3 Kırmızı) Uyum Şartları: Fast < Medium < MedFast < Slow
                s1 = "✅" if fast < medium else "❌"
                s2 = "✅" if medium < medfast else "❌"
                s3 = "✅" if medfast < slow else "❌"

                # 3. Kritik Karar ve İşlem Bölgesi (LOCK İÇİNDE - Çift İşlem / Yarış Durumu Koruması)
                async with lock:
                    open_trade = await db.get_open_trade(sym_upper)
                    current_state = self.trade_state.get_state(sym_upper)

                    # DB'deki açık işlem ile state senkronizasyonu
                    if open_trade and not current_state['in_position']:
                        current_state['in_position'] = True
                        current_state['side'] = open_trade['side']
                        current_state['entry_price'] = open_trade['entry_price']
                        current_state['size'] = open_trade['size']
                    elif not open_trade and current_state['in_position']:
                        self.trade_state.reset_for_new_trade(sym_upper)

                    durum_ikonu = "🟢" if is_3_green else ("🔴" if is_3_red else "⚪")
                    pozisyon_durumu = f"İÇERİDEYİZ: {current_state['side']}" if current_state['in_position'] else "NAKİTTE BEKLENİYOR"
                    tp_durumu = " (TP ALINDI ✅)" if current_state['tp_taken'] else ""
                    
                    # Detaylandırılmış Log Çıktısı
                    logger.info(
                        f"[{sym_upper}] {durum_ikonu} Fiyat: {current_price:.4f} | {pozisyon_durumu}{tp_durumu}\n"
                        f"          └─ 📈 LONG Uyum : [F>M:{l1}] [M>MF:{l2}] [MF>S:{l3}]\n"
                        f"          └─ 📉 SHORT Uyum: [F<M:{s1}] [M<MF:{s2}] [MF<S:{s3}]"
                    )
  
                    # --- 1. HAYATTA KAL (ÇIKIŞ SİNYALİ KONTROLÜ) ---
                    if current_state['in_position']:
                        # Ana çıkış: TBO 3'lü uyum bozulduğunda kalan pozisyonu kapat
                        if long_exit and current_state['side'] == 'LONG':
                            logger.warning(f"[{sym_upper}] ⚠️ LONG ÇIKIŞ SİNYALİ! (3 Yeşil Bozuldu). Kalan pozisyon kapatılıyor...")
                            await self.execution_engine.close_position(
                                symbol=sym_upper, side='LONG', size=current_state['size'], reason="LONG EXIT SİNYALİ (TBO Bozuldu)"
                            )
                            self.trade_state.reset_for_new_trade(sym_upper)
                            await asyncio.sleep(10)
                            continue
                            
                        elif short_exit and current_state['side'] == 'SHORT':
                            logger.warning(f"[{sym_upper}] ⚠️ SHORT ÇIKIŞ SİNYALİ! (3 Kırmızı Bozuldu). Kalan pozisyon kapatılıyor...")
                            await self.execution_engine.close_position(
                                symbol=sym_upper, side='SHORT', size=current_state['size'], reason="SHORT EXIT SİNYALİ (TBO Bozuldu)"
                            )
                            self.trade_state.reset_for_new_trade(sym_upper)
                            await asyncio.sleep(10)
                            continue

                        # --- 2. KÂRI KORU (TP SİNYALİ KONTROLÜ) ---
                        # Sadece daha önce TP alınmamışsa ve hala pozisyondaysak
                        if not current_state['tp_taken']:
                            if long_tp_signal and current_state['side'] == 'LONG':
                                logger.warning(f"[{sym_upper}] 💰 LONG TP SİNYALİ! (Trend Yoruldu). %50 kâr alınıyor...")
                                # Pozisyonun yarısını sat
                                half_size = current_state['size'] / 2
                                await self.execution_engine.close_position(
                                    symbol=sym_upper, side='LONG', size=half_size, reason="LONG TP (Trend Yoruldu)"
                                )
                                # Stop loss'u break-even'a çek
                                await self.execution_engine.move_stop_to_breakeven(
                                    symbol=sym_upper, side='LONG', entry_price=current_state['entry_price']
                                )
                                # TP bayrağını işaretle
                                self.trade_state.set_tp_taken(sym_upper)
                                # State'deki boyutu güncelle (kalan %50)
                                current_state['size'] = half_size
                                await asyncio.sleep(10)
                                continue
                                
                            elif short_tp_signal and current_state['side'] == 'SHORT':
                                logger.warning(f"[{sym_upper}] 💰 SHORT TP SİNYALİ! (Trend Yoruldu). %50 kâr alınıyor...")
                                # Pozisyonun yarısını sat
                                half_size = current_state['size'] / 2
                                await self.execution_engine.close_position(
                                    symbol=sym_upper, side='SHORT', size=half_size, reason="SHORT TP (Trend Yoruldu)"
                                )
                                # Stop loss'u break-even'a çek
                                await self.execution_engine.move_stop_to_breakeven(
                                    symbol=sym_upper, side='SHORT', entry_price=current_state['entry_price']
                                )
                                # TP bayrağını işaretle
                                self.trade_state.set_tp_taken(sym_upper)
                                # State'deki boyutu güncelle (kalan %50)
                                current_state['size'] = half_size
                                await asyncio.sleep(10)
                                continue

                        # Zaten bir işlemdeysek ve çıkış/TP sinyali de yoksa, yeni girişleri yok say ve bekle
                        await asyncio.sleep(3)
                        continue

                    # --- 3. FIRSAT ARA (GİRİŞ SİNYALİ KONTROLÜ) ---
                    if long_signal:
                        logger.info(f"[{sym_upper}] 🚀 GÜÇLÜ LONG SİNYALİ! İşleme giriliyor...")
                        margin_usdt = await self.risk_manager.calculate_margin(sym_upper)
                        
                        if margin_usdt > 0:
                            stop_price = current_price - atr_stop_dist
                            success = await self.execution_engine.execute_trade(
                                symbol=sym_upper, side="LONG", margin_usdt=margin_usdt, 
                                leverage=config.LEVERAGE, entry_price=current_price, stop_price=stop_price
                            )
                            if success:
                                # State'i güncelle
                                current_state['in_position'] = True
                                current_state['side'] = 'LONG'
                                current_state['entry_price'] = current_price
                                # Gerçek boyutu DB'den çek (senkronizasyon)
                                await asyncio.sleep(0.5)  # DB kaydının tamamlanması için kısa bekle
                                updated_trade = await db.get_open_trade(sym_upper)
                                if updated_trade:
                                    current_state['size'] = updated_trade['size']
                                    logger.info(f"[{sym_upper}] State boyut güncellendi: {current_state['size']}")
                            await asyncio.sleep(10)
                            
                    elif short_signal:
                        logger.info(f"[{sym_upper}] 🩸 GÜÇLÜ SHORT SİNYALİ! İşleme giriliyor...")
                        margin_usdt = await self.risk_manager.calculate_margin(sym_upper)
                        
                        if margin_usdt > 0:
                            stop_price = current_price + atr_stop_dist
                            success = await self.execution_engine.execute_trade(
                                symbol=sym_upper, side="SHORT", margin_usdt=margin_usdt, 
                                leverage=config.LEVERAGE, entry_price=current_price, stop_price=stop_price
                            )
                            if success:
                                # State'i güncelle
                                current_state['in_position'] = True
                                current_state['side'] = 'SHORT'
                                current_state['entry_price'] = current_price
                                # Gerçek boyutu DB'den çek (senkronizasyon)
                                await asyncio.sleep(0.5)  # DB kaydının tamamlanması için kısa bekle
                                updated_trade = await db.get_open_trade(sym_upper)
                                if updated_trade:
                                    current_state['size'] = updated_trade['size']
                                    logger.info(f"[{sym_upper}] State boyut güncellendi: {current_state['size']}")
                            await asyncio.sleep(10)

            except Exception as e:
                logger.error(f"[{sym_upper}] ❌ Tarama döngüsünde kritik hata: {e}")
                await asyncio.sleep(5)

            await asyncio.sleep(3)

    async def start(self):
        self._running = True
        logger.info(f"🏁 Atlantis Strateji Motoru {len(self.symbols)} sembol için başlatılıyor...")
        tasks = [asyncio.create_task(self._scan_market_for_symbol(symbol)) for symbol in self.symbols]
        await asyncio.gather(*tasks)

    def stop(self):
        self._running = False
        logger.info("🛑 Atlantis Strateji Motoru durduruluyor...")