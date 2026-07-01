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
                'strategy_type': None,  # Kullanılan strateji türü
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
            'strategy_type': None,
        }

    def set_tp_taken(self, symbol: str):
        """TP sinyali işlendiğinde bayrağı True yapar."""
        if symbol in self._state:
            self._state[symbol]['tp_taken'] = True


class AtlantisStrategyRunner:
    """Tam otonom Atlantis tarayıcısı: Çift işlem korumalı ve Çıkış kontrollü."""

    def __init__(self, symbols: List[str], execution_engine, risk_manager, client, timeframe: str = '5m'):
        # Orijinal sembol formatını (örn. "SOL/USDT") koru - CCXT bu formatta bekler
        self.symbols = symbols  # Orijinal haliyle sakla
        # Lock/state anahtarı için normalize edilmiş versiyon (örn. "SOLUSDT")
        self.symbol_keys = [s.replace("/", "").upper() for s in symbols]
        self.execution_engine = execution_engine
        self.risk_manager = risk_manager
        self.client = client
        self.timeframe = timeframe
        self._running = False
        self.indicator = AtlantisIndicator()
        self.trade_state = TradeState()  # Durum hafızası
        logger.info("🛠️ Atlantis İndikatör Matematiği ve Durum Hafızası belleğe yüklendi.")

    async def _scan_market_for_symbol(self, symbol: str):
        # CCXT için orijinal format (örn. "SOL/USDT"), lock/state için normalize (örn. "SOLUSDT")
        sym_ccxt = symbol  # CCXT "SOL/USDT" formatını bekler
        sym_key = symbol.replace("/", "").upper()  # Lock/state anahtarı
        lock = state.get_symbol_lock(sym_key) # Sembole özel kilit alınıyor
        logger.info(f"[{sym_key}] 🔍 {self.timeframe} periyotlu piyasa taraması başlatıldı.")

        while self._running:
            try:
                # 1. Veri Çekme Aşaması (Lock Dışında - API istekleri kilidi bloklamasın)
                ohlcv = await self.client.exchange.fetch_ohlcv(sym_ccxt, timeframe=self.timeframe, limit=200)

                if not ohlcv or len(ohlcv) < 150:
                    await asyncio.sleep(5)
                    continue
                               # 2. DataFrame Çevirimi ve Hesaplama (Lock Dışında)
                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                df_signals = self.indicator.get_signals(df)

                last_closed_candle = df_signals.iloc[-2]
                last_candle = df_signals.iloc[-1]
                current_price = last_candle['close']

                # Yeni rejim değişimli sinyaller
                regime = str(last_closed_candle.get('regime', 'UNKNOWN'))
                strategy_type = str(last_closed_candle.get('strategy_type', 'NONE'))
                long_signal = bool(last_closed_candle['long_signal'])
                short_signal = bool(last_closed_candle['short_signal'])
                long_exit = bool(last_closed_candle['long_exit'])
                short_exit = bool(last_closed_candle['short_exit'])
                long_tp_signal = bool(last_closed_candle['long_tp_signal'])
                short_tp_signal = bool(last_closed_candle['short_tp_signal'])
                atr_stop_dist = float(last_closed_candle['atr_stop_dist'])

                # --- 📊 METRİK HESAPLAMALARI (en güncel mumdan) ---
                adx_val = last_candle.get('adx', 0)
                rsi_val = last_candle.get('rsi', 0)
                vol_sma = last_candle.get('vol_sma', 0)
                vol_ratio = (last_candle['volume'] / vol_sma) * 100 if vol_sma > 0 else 0

                # 1. Rejim ve Strateji Bilgisini Belirle
                if adx_val < 20:
                    regime_icon = "↔️ YATAY (RANGE)"
                    strat_info = f"Mean Reversion (Bollinger) bekleniyor... [RSI: {rsi_val:.1f}]"
                elif 20 <= adx_val <= 25:
                    regime_icon = "🎯 SIKIŞMA (TRANSITION)"
                    strat_info = "TTM Squeeze kırılımı bekleniyor..."
                else:
                    regime_icon = "🚀 TREND"
                    strat_info = "Donchian Breakout / Pullback aranıyor..."

                # 2. Hacim Onayını Görselleştir
                vol_status = "✅" if vol_ratio >= 120 else "❌"

                # 3. Kritik Karar ve İşlem Bölgesi (LOCK İÇİNDE - Çift İşlem / Yarış Durumu Koruması)
                async with lock:
                    open_trade = await db.get_open_trade(sym_key)
                    current_state = self.trade_state.get_state(sym_key)

                    # DB'deki açık işlem ile state senkronizasyonu
                    if open_trade and not current_state['in_position']:
                        current_state['in_position'] = True
                        current_state['side'] = open_trade['side']
                        current_state['entry_price'] = open_trade['entry_price']
                        current_state['size'] = open_trade['size']
                    elif not open_trade and current_state['in_position']:
                        self.trade_state.reset_for_new_trade(sym_key)

                    # Pozisyon durumu
                    pozisyon_durumu = f"İÇERİDEYİZ: {current_state['side']}" if current_state['in_position'] else "NAKİTTE"
                    tp_durumu = " (TP ALINDI ✅)" if current_state['tp_taken'] else ""
                    
                    # Yeni Profesyonel Log Çıktısı (Quant tarzı)
                    logger.info(
                        f"[{sym_key}] {regime_icon} | 💵 Fiyat: {current_price:.4f} | {pozisyon_durumu}{tp_durumu}\n"
                        f"          └─ 📊 Metrikler: ADX: {adx_val:.1f} | Hacim: %{vol_ratio:.0f} ({vol_status}) | RSI: {rsi_val:.1f}\n"
                        f"          └─ 🎯 Strateji : {strat_info}"
                    )
  
                    # --- 1. HAYATTA KAL (ÇIKIŞ SİNYALİ KONTROLÜ) ---
                    if current_state['in_position']:
                        # Stratejiye özel çıkış sinyali
                        exit_reason = None
                        if long_exit and current_state['side'] == 'LONG':
                            if current_state['strategy_type'] == 'MEAN_REVERSION':
                                exit_reason = "LONG EXIT (Bollinger Orta Bandı)"
                            elif current_state['strategy_type'] == 'SQUEEZE':
                                exit_reason = "LONG EXIT (Squeeze Bitti)"
                            elif current_state['strategy_type'] == 'TREND':
                                exit_reason = "LONG EXIT (DI Kesişimi)"
                            else:
                                exit_reason = "LONG EXIT SİNYALİ"
                                
                        elif short_exit and current_state['side'] == 'SHORT':
                            if current_state['strategy_type'] == 'MEAN_REVERSION':
                                exit_reason = "SHORT EXIT (Bollinger Orta Bandı)"
                            elif current_state['strategy_type'] == 'SQUEEZE':
                                exit_reason = "SHORT EXIT (Squeeze Bitti)"
                            elif current_state['strategy_type'] == 'TREND':
                                exit_reason = "SHORT EXIT (DI Kesişimi)"
                            else:
                                exit_reason = "SHORT EXIT SİNYALİ"
                        
                        if exit_reason:
                            logger.warning(f"[{sym_key}] ⚠️ {exit_reason}! Kalan pozisyon kapatılıyor...")
                            await self.execution_engine.close_position(
                                symbol=sym_key, 
                                side=current_state['side'], 
                                size=current_state['size'], 
                                reason=exit_reason
                            )
                            self.trade_state.reset_for_new_trade(sym_key)
                            await asyncio.sleep(10)
                            continue

                        # --- 2. KÂRI KORU (TP SİNYALİ KONTROLÜ) ---
                        # Sadece daha önce TP alınmamışsa ve hala pozisyondaysak
                        if not current_state['tp_taken']:
                            if long_tp_signal and current_state['side'] == 'LONG':
                                logger.warning(f"[{sym_key}] 💰 LONG TP SİNYALİ! (Trend Yoruldu). %50 kâr alınıyor...")
                                # Pozisyonun yarısını sat
                                half_size = current_state['size'] / 2
                                await self.execution_engine.close_position(
                                    symbol=sym_key, side='LONG', size=half_size, reason="LONG TP (Trend Yoruldu)"
                                )
                                # Stop loss'u break-even'a çek
                                await self.execution_engine.move_stop_to_breakeven(
                                    symbol=sym_key, side='LONG', entry_price=current_state['entry_price']
                                )
                                # TP bayrağını işaretle
                                self.trade_state.set_tp_taken(sym_key)
                                # State'deki boyutu güncelle (kalan %50)
                                current_state['size'] = half_size
                                await asyncio.sleep(10)
                                continue
                                
                            elif short_tp_signal and current_state['side'] == 'SHORT':
                                logger.warning(f"[{sym_key}] 💰 SHORT TP SİNYALİ! (Trend Yoruldu). %50 kâr alınıyor...")
                                # Pozisyonun yarısını sat
                                half_size = current_state['size'] / 2
                                await self.execution_engine.close_position(
                                    symbol=sym_key, side='SHORT', size=half_size, reason="SHORT TP (Trend Yoruldu)"
                                )
                                # Stop loss'u break-even'a çek
                                await self.execution_engine.move_stop_to_breakeven(
                                    symbol=sym_key, side='SHORT', entry_price=current_state['entry_price']
                                )
                                # TP bayrağını işaretle
                                self.trade_state.set_tp_taken(sym_key)
                                # State'deki boyutu güncelle (kalan %50)
                                current_state['size'] = half_size
                                await asyncio.sleep(10)
                                continue

                        # Zaten bir işlemdeysek ve çıkış/TP sinyali de yoksa, yeni girişleri yok say ve bekle
                        await asyncio.sleep(3)
                        continue

                    # --- 3. FIRSAT ARA (GİRİŞ SİNYALİ KONTROLÜ) ---
                    if long_signal:
                        strateji_adi = strategy_type if strategy_type != 'NONE' else 'UNKNOWN'
                        logger.info(f"[{sym_key}] 🚀 GÜÇLÜ LONG SİNYALİ! ({regime} | {strategy_type}) İşleme giriliyor...")
                        margin_usdt = await self.risk_manager.calculate_margin(sym_key)
                        
                        if margin_usdt > 0:
                            stop_price = current_price - atr_stop_dist
                            success = await self.execution_engine.execute_trade(
                                symbol=sym_key, side="LONG", margin_usdt=margin_usdt, 
                                leverage=config.LEVERAGE, entry_price=current_price, stop_price=stop_price
                            )
                            if success:
                                # State'i güncelle
                                current_state['in_position'] = True
                                current_state['side'] = 'LONG'
                                current_state['entry_price'] = current_price
                                current_state['strategy_type'] = strategy_type
                                # Gerçek boyutu DB'den çek (senkronizasyon)
                                await asyncio.sleep(0.5)  # DB kaydının tamamlanması için kısa bekle
                                updated_trade = await db.get_open_trade(sym_key)
                                if updated_trade:
                                    current_state['size'] = updated_trade['size']
                                    logger.info(f"[{sym_key}] State boyut güncellendi: {current_state['size']} | Strateji: {strategy_type}")
                            await asyncio.sleep(10)
                            
                    elif short_signal:
                        strateji_adi = strategy_type if strategy_type != 'NONE' else 'UNKNOWN'
                        logger.info(f"[{sym_key}] 🩸 GÜÇLÜ SHORT SİNYALİ! ({regime} | {strategy_type}) İşleme giriliyor...")
                        margin_usdt = await self.risk_manager.calculate_margin(sym_key)
                        
                        if margin_usdt > 0:
                            stop_price = current_price + atr_stop_dist
                            success = await self.execution_engine.execute_trade(
                                symbol=sym_key, side="SHORT", margin_usdt=margin_usdt, 
                                leverage=config.LEVERAGE, entry_price=current_price, stop_price=stop_price
                            )
                            if success:
                                # State'i güncelle
                                current_state['in_position'] = True
                                current_state['side'] = 'SHORT'
                                current_state['entry_price'] = current_price
                                current_state['strategy_type'] = strategy_type
                                # Gerçek boyutu DB'den çek (senkronizasyon)
                                await asyncio.sleep(0.5)  # DB kaydının tamamlanması için kısa bekle
                                updated_trade = await db.get_open_trade(sym_key)
                                if updated_trade:
                                    current_state['size'] = updated_trade['size']
                                    logger.info(f"[{sym_key}] State boyut güncellendi: {current_state['size']} | Strateji: {strategy_type}")
                            await asyncio.sleep(10)

            except Exception as e:
                logger.error(f"[{sym_key}] ❌ Tarama döngüsünde kritik hata: {e}")
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