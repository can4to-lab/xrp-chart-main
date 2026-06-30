import pandas as pd
import numpy as np


class AtlantisIndicator:
    """
    Atlantis EMA indikatörü, ATR tabanlı risk yönetimi, ADX filtresi ve akıllı çıkış sinyalleri.
    TradingView (Pine Script) matematiğine %100 sadık kalınmıştır.
    """

    def __init__(self,
                 fast_len: int = 20,
                 medium_len: int = 40,
                 medfast_len: int = 50,
                 slow_len: int = 150,
                 atr_period: int = 50,
                 atr_multiplier: float = 6.0,
                 adx_period: int = 14,
                 adx_threshold: int = 25,
                 volume_ma_period: int = 20,
                 volume_multiplier: float = 1.2
                 ):

        self.fast_len = fast_len
        self.medium_len = medium_len
        self.medfast_len = medfast_len
        self.slow_len = slow_len
        self.atr_period = atr_period
        self.atr_multiplier = atr_multiplier
        self.adx_period = adx_period
        self.adx_threshold = adx_threshold
        self.volume_ma_period = volume_ma_period
        self.volume_multiplier = volume_multiplier

    def calculate_ema(self, series: pd.Series, length: int) -> pd.Series:
        return series.ewm(span=length, adjust=False).mean()

    def calculate_sma(self, series: pd.Series, length: int) -> pd.Series:
        return series.rolling(window=length).mean()

    def calculate_atr(self, df: pd.DataFrame, length: int) -> pd.Series:
        """Pine Script'teki ta.atr() fonksiyonunun birebir Python karşılığıdır (RMA kullanır)."""
        high = df['high']
        low = df['low']
        prev_close = df['close'].shift(1)

        # True Range (TR) Hesaplaması
        tr1 = high - low
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()

        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        # Pine Script ATR'si RMA (Wilder's Smoothing) kullanır. Alpha = 1 / length
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

        # +DM ve -DM hesaplama
        up_move = high - high.shift(1)
        down_move = low.shift(1) - low

        plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0), index=df.index)
        minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0), index=df.index)

        # True Range
        tr1 = high - low
        tr2 = (high - close.shift(1)).abs()
        tr3 = (low - close.shift(1)).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        # Wilder's Smoothing (RMA) kullanarak smoothing
        atr = self.calculate_rma(tr, period)
        plus_di = 100 * self.calculate_rma(plus_dm, period) / atr
        minus_di = 100 * self.calculate_rma(minus_dm, period) / atr

        # DX ve ADX (Zero division koruması)
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

    def get_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """Mum verilerinden (OHLCV) giriş, çıkış ve stop loss seviyelerini hesaplar."""
        # Gerekli sütunların kontrolü
        for col in ['open', 'high', 'low', 'close', 'volume']:
            if col not in df.columns:
                raise ValueError(f"DataFrame içinde '{col}' sütunu bulunmalı.")

        df = df.copy()

        # 1. TBO (Hareketli Ortalamalar) Hesaplaması
        df['fastTBO'] = self.calculate_ema(df['close'], self.fast_len)
        df['mediumTBO'] = self.calculate_ema(df['close'], self.medium_len)
        df['medfastTBO'] = self.calculate_sma(df['close'], self.medfast_len)
        df['slowTBO'] = self.calculate_sma(df['close'], self.slow_len)

        # 2. ATR ve Stop Mesafesi Hesaplaması
        df['atr'] = self.calculate_atr(df, self.atr_period)
        df['atr_stop_dist'] = df['atr'] * self.atr_multiplier

        # 3. Hacim Ortalaması ve Hacim Filtresi
        df['volume_ma'] = self.calculate_sma(df['volume'], self.volume_ma_period)
        df['volume_ok'] = df['volume'] > (df['volume_ma'] * self.volume_multiplier)

        # 4. ADX Hesaplama
        adx_df = self.calculate_adx(df, self.adx_period)
        df['adx'] = adx_df['adx']
        df['plus_di'] = adx_df['plus_di']
        df['minus_di'] = adx_df['minus_di']

        # ADX trend gücü filtresi (ADX > threshold)
        df['adx_strong'] = df['adx'] > self.adx_threshold

        # ADX yönelim sinyalleri (DI kesişimleri)
        df['plus_di_cross'] = (df['plus_di'] > df['minus_di']) & (df['plus_di'].shift(1) <= df['minus_di'].shift(1))
        df['minus_di_cross'] = (df['minus_di'] > df['plus_di']) & (df['minus_di'].shift(1) <= df['plus_di'].shift(1))

        # 5. Üçlü Trend Kontrolü (Renkler)
        df['is_3_green'] = (df['fastTBO'] > df['mediumTBO']) & (
            df['mediumTBO'] > df['medfastTBO']) & (df['medfastTBO'] > df['slowTBO'])
        df['is_3_red'] = (df['fastTBO'] < df['mediumTBO']) & (
            df['mediumTBO'] < df['medfastTBO']) & (df['medfastTBO'] < df['slowTBO'])

        # --- DELAYED ENTRY (BEKLEYEN AVCİ) ALGORİTMASI ---
        # TBO kesişimi + Hacim + ADX koşullarının birikmesini takip eder
        df['long_entry_ready'] = (
            df['is_3_green'] &
            df['volume_ok'] &
            df['adx_strong'] &
            (df['plus_di'] > df['minus_di'])
        )
        df['short_entry_ready'] = (
            df['is_3_red'] &
            df['volume_ok'] &
            df['adx_strong'] &
            (df['minus_di'] > df['plus_di'])
        )

        # "Bekleyen Avcı" - Koşullar birikti mi? İlk kez tüm şartlar aynı anda gerçekleşti mi?
        df['long_signal'] = df['long_entry_ready'] & (~df['long_entry_ready'].shift(1).fillna(False))
        df['short_signal'] = df['short_entry_ready'] & (~df['short_entry_ready'].shift(1).fillna(False))

        # --- İŞLEMDEN ÇIKIŞ SİNYALLERİ (3 Kuralı Bozulduğunda) ---
        df['long_exit'] = (~df['is_3_green']) & (df['is_3_green'].shift(1).fillna(False))
        df['short_exit'] = (~df['is_3_red']) & (df['is_3_red'].shift(1).fillna(False))

        # --- MOMENTUM DIVERGENCE (TREND YORULMAS) - %50 TP SİNYALLERİ ---
        # ADX 40'ın üzerinde ve DI'lar boynun bükülmesi (divergence)
        df['adx_fatigue'] = df['adx'] > 40

        # LONG için TP sinyali: ADX yoruldu + DI kesişimi (bearish divergence)
        df['long_tp_signal'] = (
            df['adx_fatigue'] &
            df['minus_di_cross'] &
            (df['is_3_green'])  # Hala trend varsa ama yoruldu
        )

        # SHORT için TP sinyali: ADX yoruldu + DI kesişimi (bullish divergence)
        df['short_tp_signal'] = (
            df['adx_fatigue'] &
            df['plus_di_cross'] &
            (df['is_3_red'])  # Hala trend varsa ama yoruldu
        )

        return df