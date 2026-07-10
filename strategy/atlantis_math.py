import pandas as pd
import numpy as np


class AtlantisIndicator:
    """
    Rejim Değişimli (Regime-Switching) Quant Mimarisi.
    
    Rejim 1 - Yatay Piyasa (ADX < 20): Bollinger Bantları + RSI (Mean Reversion)
    Rejim 2 - Sıkışma (Bollinger Keltner içinde): TTM Squeeze
    Rejim 3 - Trend (ADX > 25): Donchian Breakout veya EMA20 Pullback
    Bonus: Likidite Avı (MSB + Liquidity Sweep)
    
    TradingView (Pine Script) matematiğine sadık kalınmıştır.
    """

    def __init__(self,
                 # TBO parametreleri (geriye dönük uyumluluk için korundu)
                 fast_len: int = 20,
                 medium_len: int = 40,
                 medfast_len: int = 50,
                 slow_len: int = 150,
                 # Risk yönetimi
                 atr_period: int = 14,
                 atr_multiplier: float = 6.0,
                 # Piyasa rejimi filtresi
                 adx_period: int = 14,
                 adx_threshold_low: int = 20,
                 adx_threshold_high: int = 25,
                 # Hacim filtresi
                 volume_ma_period: int = 20,
                 volume_multiplier: float = 1.2,
                 # Bollinger Bantları
                 bb_period: int = 20,
                 bb_std: float = 2.0,
                 # RSI
                 rsi_period: int = 14,
                 rsi_oversold: int = 30,
                 rsi_overbought: int = 70,
                 # Keltner Kanalları (TTM Squeeze için)
                 kc_period: int = 20,
                 kc_atr_mult: float = 1.5,
                 # Donchian Kanalları
                 donchian_period: int = 20,
                 # EMA Pullback
                 ema_pullback_period: int = 20
                 ):

        # TBO parametreleri
        self.fast_len = fast_len
        self.medium_len = medium_len
        self.medfast_len = medfast_len
        self.slow_len = slow_len
        # Risk yönetimi
        self.atr_period = atr_period
        self.atr_multiplier = atr_multiplier
        # Piyasa rejimi
        self.adx_period = adx_period
        self.adx_threshold_low = adx_threshold_low
        self.adx_threshold_high = adx_threshold_high
        # Hacim
        self.volume_ma_period = volume_ma_period
        self.volume_multiplier = volume_multiplier
        # Bollinger
        self.bb_period = bb_period
        self.bb_std = bb_std
        # RSI
        self.rsi_period = rsi_period
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        # Keltner
        self.kc_period = kc_period
        self.kc_atr_mult = kc_atr_mult
        # Donchian
        self.donchian_period = donchian_period
        # EMA Pullback
        self.ema_pullback_period = ema_pullback_period

    def calculate_ema(self, series: pd.Series, length: int) -> pd.Series:
        return series.ewm(span=length, adjust=False).mean()

    def calculate_sma(self, series: pd.Series, length: int) -> pd.Series:
        return series.rolling(window=length).mean()

    def calculate_atr(self, df: pd.DataFrame, length: int) -> pd.Series:
        """Pine Script'teki ta.atr() fonksiyonunun birebir Python karşılığıdır (RMA kullanır)."""
        high = df['high']
        low = df['low']
        prev_close = df['close'].shift(1)

        tr1 = high - low
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()

        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1/length, adjust=False).mean()
        return atr

    def calculate_rma(self, series: pd.Series, length: int) -> pd.Series:
        """Wilder's Smoothing (RMA) - Pine Script'teki ta.rma() fonksiyonunun birebir karşılığı."""
        return series.ewm(alpha=1/length, adjust=False).mean()

    def calculate_adx(self, df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
        """
        ADX (Average Directional Index) hesaplama.
        TradingView Pine Script ta.adx() matematiğine sadık kalınmıştır.
        """
        high = df['high']
        low = df['low']
        close = df['close']

        up_move = high - high.shift(1)
        down_move = low.shift(1) - low

        plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0), index=df.index)
        minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0), index=df.index)

        tr1 = high - low
        tr2 = (high - close.shift(1)).abs()
        tr3 = (low - close.shift(1)).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        atr = self.calculate_rma(tr, period)
        plus_di = 100 * self.calculate_rma(plus_dm, period) / atr
        minus_di = 100 * self.calculate_rma(minus_dm, period) / atr

        plus_di_plus_minus_di = plus_di + minus_di
        dx = pd.Series(
            np.where(plus_di_plus_minus_di == 0, 0,
                     100 * (plus_di - minus_di).abs() / plus_di_plus_minus_di),
            index=df.index
        )
        adx = self.calculate_rma(dx, period)

        return pd.DataFrame({
            'plus_di': plus_di,
            'minus_di': minus_di,
            'adx': adx
        }, index=df.index)

    def calculate_bollinger_bands(self, df: pd.DataFrame) -> pd.DataFrame:
        """Bollinger Bantları hesaplama."""
        bb_middle = self.calculate_sma(df['close'], self.bb_period)
        bb_std = df['close'].rolling(window=self.bb_period).std()
        bb_upper = bb_middle + (bb_std * self.bb_std)
        bb_lower = bb_middle - (bb_std * self.bb_std)
        
        return pd.DataFrame({
            'bb_middle': bb_middle,
            'bb_upper': bb_upper,
            'bb_lower': bb_lower,
            'bb_width': (bb_upper - bb_lower) / bb_middle  # Bant genişliği (normalize)
        }, index=df.index)

    def calculate_rsi(self, df: pd.DataFrame) -> pd.Series:
        """RSI (Relative Strength Index) hesaplama."""
        delta = df['close'].diff()
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)
        
        avg_gain = gain.ewm(alpha=1/self.rsi_period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/self.rsi_period, adjust=False).mean()
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return rsi

    def calculate_keltner_channels(self, df: pd.DataFrame) -> pd.DataFrame:
        """Keltner Kanalları hesaplama (TTM Squeeze için)."""
        kc_middle = self.calculate_ema(df['close'], self.kc_period)
        atr = self.calculate_atr(df, self.atr_period)
        kc_upper = kc_middle + (atr * self.kc_atr_mult)
        kc_lower = kc_middle - (atr * self.kc_atr_mult)
        
        return pd.DataFrame({
            'kc_middle': kc_middle,
            'kc_upper': kc_upper,
            'kc_lower': kc_lower
        }, index=df.index)

    def calculate_ttm_squeeze(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        TTM Squeeze: Bollinger Bantları Keltner Kanalları'nın içindeyken sıkışma.
        Bantlar daraldıktan sonra dışarıya kırılım = Sinyal.
        """
        bb = self.calculate_bollinger_bands(df)
        kc = self.calculate_keltner_channels(df)
        
        # Sıkışma: Bollinger üst/alt Keltner sınırları içinde mi?
        squeeze_on = (bb['bb_lower'] > kc['kc_lower']) & (bb['bb_upper'] < kc['kc_upper'])
        
        # Sıkışma çözülüyor mu? (Önceki mumda sıkışma vardı, şimdi yok)
        squeeze_firing = squeeze_on & (~squeeze_on.shift(1).fillna(False))
        
        # Kırılım yönü
        bb_momentum = df['close'] - bb['bb_middle']
        
        return pd.DataFrame({
            'squeeze_on': squeeze_on,
            'squeeze_firing': squeeze_firing,
            'squeeze_momentum': bb_momentum,
            'in_squeeze': squeeze_on
        }, index=df.index)

    def calculate_donchian_channels(self, df: pd.DataFrame) -> pd.DataFrame:
        """Donchian Kanalları (N periyotlı en yüksek/en düşük)."""
        donchian_high = df['high'].rolling(window=self.donchian_period).max()
        donchian_low = df['low'].rolling(window=self.donchian_period).min()
        donchian_middle = (donchian_high + donchian_low) / 2
        
        return pd.DataFrame({
            'donchian_high': donchian_high,
            'donchian_low': donchian_low,
            'donchian_middle': donchian_middle
        }, index=df.index)

    def calculate_market_structure(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Market Structure Break (MSB) ve Liquidity Sweep tespiti.
        
        MSB: Yüksek/düşük kırılımı
        Liquidity Sweep: Stop avı (üst/düşük sınırı test edip geri dönüş)
        """
        # Son 3 mum için yapısal seviyeler
        recent_high = df['high'].rolling(window=3).max()
        recent_low = df['low'].rolling(window=3).min()
        
        # Market Structure Break (Yüksek kırılım)
        msb_bullish = (df['close'] > recent_high.shift(1)) & (df['close'] > df['open'])
        msb_bearish = (df['close'] < recent_low.shift(1)) & (df['close'] < df['open'])
        
        # Liquidity Sweep (Üst sınırı test et, kapanışı içeride bırak)
        upper_wick_sweep = (df['high'] > recent_high.shift(1)) & (df['close'] < df['high']) & (df['close'] < recent_high.shift(1))
        lower_wick_sweep = (df['low'] < recent_low.shift(1)) & (df['close'] > df['low']) & (df['close'] > recent_low.shift(1))
        
        return pd.DataFrame({
            'msb_bullish': msb_bullish,
            'msb_bearish': msb_bearish,
            'liquidity_sweep_upper': upper_wick_sweep,
            'liquidity_sweep_lower': lower_wick_sweep
        }, index=df.index)

    def detect_regime(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Piyasa rejimini tespit et:
        - REJIM 1: Yatay Piyasa (ADX < 20)
        - REJIM 2: Sıkışma (Bollinger Keltner içinde)
        - REJIM 3: Trend (ADX > 25)
        """
        adx_df = self.calculate_adx(df, self.adx_period)
        squeeze_df = self.calculate_ttm_squeeze(df)
        
        regime = pd.Series('UNKNOWN', index=df.index)
        
        # Rejim 1: Yatay Piyasa
        regime[adx_df['adx'] < self.adx_threshold_low] = 'RANGE'
        
        # Rejim 2: Sıkışma
        regime[squeeze_df['in_squeeze']] = 'SQUEEZE'
        
        # Rejim 3: Trend
        regime[adx_df['adx'] > self.adx_threshold_high] = 'TREND'
        
        # Çakışma durumunda öncelik: Trend > Sıkışma > Yatay
        regime = regime.replace('UNKNOWN', 'RANGE')  # Varsayılan
        
        return pd.DataFrame({
            'regime': regime,
            'adx': adx_df['adx'],
            'plus_di': adx_df['plus_di'],
            'minus_di': adx_df['minus_di']
        }, index=df.index)

    def get_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Rejim değişimli sinyal üretici.
        
        Rejim 1 (RANGE): Bollinger + RSI Mean Reversion
        Rejim 2 (SQUEEZE): TTM Squeeze Breakout
        Rejim 3 (TREND): Donchian Breakout veya EMA20 Pullback
        Bonus: Likidite Avı filtresi
        """
        for col in ['open', 'high', 'low', 'close', 'volume']:
            if col not in df.columns:
                raise ValueError(f"DataFrame içinde '{col}' sütunu bulunmalı.")

        df = df.copy()

        # ========== TEMEL HESAPLAMALAR ==========
        # 1. ATR ve Stop Mesafesi
        df['atr'] = self.calculate_atr(df, self.atr_period)
        df['atr_stop_dist'] = df['atr'] * self.atr_multiplier

        # 2. Hacim Ortalaması ve Filtresi
        df['volume_ma'] = self.calculate_sma(df['volume'], self.volume_ma_period)
        df['vol_sma'] = df['volume_ma']  # Runner'da kullanılan alias
        df['volume_ok'] = df['volume'] > (df['volume_ma'] * self.volume_multiplier)

        # 3. TBO (Geriye dönük uyumluluk - trend yönü için)
        df['fastTBO'] = self.calculate_ema(df['close'], self.fast_len)
        df['mediumTBO'] = self.calculate_ema(df['close'], self.medium_len)
        df['medfastTBO'] = self.calculate_sma(df['close'], self.medfast_len)
        df['slowTBO'] = self.calculate_sma(df['close'], self.slow_len)

        # 4. Rejim Tespiti
        regime_df = self.detect_regime(df)
        df['regime'] = regime_df['regime']
        df['adx'] = regime_df['adx']
        df['plus_di'] = regime_df['plus_di']
        df['minus_di'] = regime_df['minus_di']

        # 5. ADX yönelim sinyalleri
        df['plus_di_cross'] = (df['plus_di'] > df['minus_di']) & (df['plus_di'].shift(1) <= df['minus_di'].shift(1))
        df['minus_di_cross'] = (df['minus_di'] > df['plus_di']) & (df['minus_di'].shift(1) <= df['plus_di'].shift(1))

        # ========== REJİM 1: YATAY PİYASA (Bollinger + RSI) ==========
        bb_df = self.calculate_bollinger_bands(df)
        df['bb_upper'] = bb_df['bb_upper']
        df['bb_middle'] = bb_df['bb_middle']
        df['bb_lower'] = bb_df['bb_lower']
        df['bb_width'] = bb_df['bb_width']
        
        df['rsi'] = self.calculate_rsi(df)
        
        # LONG: Alt bandı del + RSI < 30 + Hacim onayı
        df['range_long_entry'] = (
            (df['close'] < df['bb_lower']) &
            (df['rsi'] < self.rsi_oversold) &
            df['volume_ok'] &
            (df['regime'] == 'RANGE')
        )
        
        # SHORT: Üst bandı del + RSI > 70 + Hacim onayı
        df['range_short_entry'] = (
            (df['close'] > df['bb_upper']) &
            (df['rsi'] > self.rsi_overbought) &
            df['volume_ok'] &
            (df['regime'] == 'RANGE')
        )

        # ========== REJİM 2: SIKIŞMA (TTM Squeeze) ==========
        squeeze_df = self.calculate_ttm_squeeze(df)
        df['squeeze_on'] = squeeze_df['squeeze_on']
        df['squeeze_firing'] = squeeze_df['squeeze_firing']
        df['squeeze_momentum'] = squeeze_df['squeeze_momentum']
        
        # Sıkışma çözülünce yöne göre işlem
        df['squeeze_long_entry'] = (
            df['squeeze_firing'] &
            (df['squeeze_momentum'] > 0) &
            df['volume_ok']
        )
        
        df['squeeze_short_entry'] = (
            df['squeeze_firing'] &
            (df['squeeze_momentum'] < 0) &
            df['volume_ok']
        )

        # ========== REJİM 3: TREND (Donchian Breakout + EMA Pullback) ==========
        donchian_df = self.calculate_donchian_channels(df)
        df['donchian_high'] = donchian_df['donchian_high']
        df['donchian_low'] = donchian_df['donchian_low']
        df['donchian_middle'] = donchian_df['donchian_middle']
        
        df['ema20'] = self.calculate_ema(df['close'], self.ema_pullback_period)
        
        # Trend yönü (DI kesişimi)
        df['trend_bullish'] = df['plus_di'] > df['minus_di']
        df['trend_bearish'] = df['minus_di'] > df['plus_di']
        
        # Donchian Breakout
        df['donchian_long_entry'] = (
            (df['close'] > df['donchian_high'].shift(1)) &
            df['trend_bullish'] &
            df['volume_ok'] &
            (df['regime'] == 'TREND')
        )
        
        df['donchian_short_entry'] = (
            (df['close'] < df['donchian_low'].shift(1)) &
            df['trend_bearish'] &
            df['volume_ok'] &
            (df['regime'] == 'TREND')
        )
        
        # EMA20 Pullback (Geri çekilme)
        df['pullback_long_entry'] = (
            (df['close'] > df['ema20']) &
            (df['low'] <= df['ema20'] * 1.001) &  # EMA'ya temas
            (df['close'] > df['open']) &  # Mum pozitif
            df['trend_bullish'] &
            df['volume_ok'] &
            (df['regime'] == 'TREND')
        )
        
        df['pullback_short_entry'] = (
            (df['close'] < df['ema20']) &
            (df['high'] >= df['ema20'] * 0.999) &  # EMA'ya temas
            (df['close'] < df['open']) &  # Mum negatif
            df['trend_bearish'] &
            df['volume_ok'] &
            (df['regime'] == 'TREND')
        )

        # ========== BONUS: LİKİDİTE AVI (MSB + Liquidity Sweep) ==========
        msb_df = self.calculate_market_structure(df)
        df['msb_bullish'] = msb_df['msb_bullish']
        df['msb_bearish'] = msb_df['msb_bearish']
        df['liquidity_sweep_upper'] = msb_df['liquidity_sweep_upper']
        df['liquidity_sweep_lower'] = msb_df['liquidity_sweep_lower']
        
        # Likidite avı: Sweep + MSB + hacim
        df['liquidity_long_entry'] = (
            df['liquidity_sweep_lower'] &
            df['msb_bullish'] &
            df['volume_ok']
        )
        
        df['liquidity_short_entry'] = (
            df['liquidity_sweep_upper'] &
            df['msb_bearish'] &
            df['volume_ok']
        )

        # ========== BİRLEŞİK GİRİŞ SİNYALLERİ ==========
        # Her rejim için en güçlü sinyali birleştir
        df['long_entry_ready'] = (
            df['range_long_entry'] |
            df['squeeze_long_entry'] |
            df['donchian_long_entry'] |
            df['pullback_long_entry'] |
            df['liquidity_long_entry']
        )
        
        df['short_entry_ready'] = (
            df['range_short_entry'] |
            df['squeeze_short_entry'] |
            df['donchian_short_entry'] |
            df['pullback_short_entry'] |
            df['liquidity_short_entry']
        )
        
        # İlk kez tüm şartlar aynı anda gerçekleşti mi?
        df['long_signal'] = df['long_entry_ready'] & (~df['long_entry_ready'].shift(1).fillna(False))
        df['short_signal'] = df['short_entry_ready'] & (~df['short_entry_ready'].shift(1).fillna(False))

        # ========== STRATEJİ TİPİ BELİRLEME (Her mumda) ==========
        # Öncelik sırası: TREND > SQUEEZE > RANGE > LIQUIDITY_SWEEP
        df['strategy_type'] = 'NONE'
        
        # Varsayılan olarak rejime göre strateji ata
        df.loc[df['regime'] == 'RANGE', 'strategy_type'] = 'MEAN_REVERSION'
        df.loc[df['regime'] == 'SQUEEZE', 'strategy_type'] = 'SQUEEZE'
        df.loc[df['regime'] == 'TREND', 'strategy_type'] = 'TREND'
        
        # Likidite avı sinyali varsa öncelik ver
        df.loc[df['liquidity_long_entry'] | df['liquidity_short_entry'], 'strategy_type'] = 'LIQUIDITY_SWEEP'

        # ========== ÇIKIŞ SİNYALLERİ (Rejimden Bağımsız) ==========
        
        df['long_exit_bb'] = df['close'] > df['bb_middle']
        df['long_exit_squeeze'] = (~df['squeeze_on']) & (df['squeeze_on'].shift(1).fillna(False)) & (df['squeeze_momentum'] < 0)
        # YENİ: Ayılar daha güçlü (di_minus > di_plus) VE fiyat EMA20'nin altında kapatmışsa çık!
        df['long_exit_trend'] = (df['di_minus'] > df['di_plus']) & (df['close'] < df['ema_20'])
        
        # SHORT çıkışları
        df['short_exit_bb'] = df['close'] < df['bb_middle']
        df['short_exit_squeeze'] = (~df['squeeze_on']) & (df['squeeze_on'].shift(1).fillna(False)) & (df['squeeze_momentum'] > 0)
        # YENİ: Boğalar daha güçlü (di_plus > di_minus) VE fiyat EMA20'nin üstünde kapatmışsa çık!
        df['short_exit_trend'] = (df['di_plus'] > df['di_minus']) & (df['close'] > df['ema_20'])

        # Birleşik çıkış bayrakları: herhangi bir koşul tetiklenirse işlem kapatılır.
        df['long_exit'] = df['long_exit_bb'] | df['long_exit_squeeze'] | df['long_exit_trend']
        df['short_exit'] = df['short_exit_bb'] | df['short_exit_squeeze'] | df['short_exit_trend']

        # ========== YENİ NESİL TP SİNYALLERİ ==========
        # 1. ADX yorulma eşiği 40'tan 30'a çekildi (Gerçekçi piyasa normu)
        df['adx_fatigue'] = df['adx'] > 30
        
        # 2. RSI Momentum Yorulması (Aşırı Alım/Satım tepeleri)
        df['long_momentum_fatigue'] = df['rsi'] >= 75
        df['short_momentum_fatigue'] = df['rsi'] <= 25
        
        has_3_green = 'is_3_green' in df.columns
        has_3_red = 'is_3_red' in df.columns
        
        # LONG TP: Trend yorulduysa VEYA fiyat çok hızlı şişip RSI'ı patlattıysa kâr al!
        df['long_tp_signal'] = (
            (df['adx_fatigue'] & df['minus_di_cross']) | 
            df['long_momentum_fatigue']
        )
        
        # SHORT TP: Trend yorulduysa VEYA fiyat çok sert çöküp RSI'ı dip yaptırdıysa kâr al!
        df['short_tp_signal'] = (
            (df['adx_fatigue'] & df['plus_di_cross']) | 
            df['short_momentum_fatigue']
        )

        return df
