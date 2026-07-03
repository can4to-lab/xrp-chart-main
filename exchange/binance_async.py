# exchange/binance_async.py
import ccxt.async_support as ccxt
import logging
import asyncio
from core.config import config

logger = logging.getLogger(__name__)


class BinanceFuturesClient:
    """Binance USDT-M Vadeli İşlemler asenkron bağlantı katmanı. Retry ve exponential backoff desteklenir."""

    def __init__(self, api_key: str, secret_key: str, testnet: bool = False):
        self.exchange = ccxt.binance({
            'apiKey': api_key,
            'secret': secret_key,
            'enableRateLimit': True,
            'timeout': int(config.API_TIMEOUT_SECONDS * 1000),
            'options': {
                'defaultType': 'future',  # Spot değil, USDT-M Futures
                'adjustForTimeDifference': True
            }
        })
        self.api_semaphore = asyncio.Semaphore(config.MAX_CONCURRENT_API_CALLS)
        
        if testnet:
            # Demo Trading modu (set_sandbox_mode yerine)
            # Binance Demo Trading: https://developers.binance.com/docs/binance-spot-api-docs/demo-mode
            self.exchange.enable_demo_trading(True)
            logger.info(
                "Binance Futures DEMO TRADING modunda baslatildi. (Gercek para KULLANILMIYOR)")
        else:
            logger.warning(
                "DIKKAT: Binance Futures GERCEK hesapta baslatildi!")

    async def _retry_with_backoff(self, coro_func, max_retries: int = 3, initial_delay: float = 1.0, max_delay: float = 30.0):
        """
        Exponential backoff ile retry mekanizması.
        max_retries: Maksimum retry sayısı
        initial_delay: İlk retry arası bekleme süresi (saniye)
        max_delay: Maksimum bekleme süresi
        """
        delay = initial_delay
        for attempt in range(max_retries):
            try:
                async with self.api_semaphore:
                    return await asyncio.wait_for(coro_func(), timeout=config.API_TIMEOUT_SECONDS)
            except asyncio.TimeoutError as timeout_err:
                if attempt < max_retries - 1:
                    logger.warning(f"⚠️ API çağrısı timeout {attempt + 1}/{max_retries} (Bekleme: {delay}s): {timeout_err}")
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, max_delay)
                else:
                    logger.error(f"❌ API çağrısı timeout oldu (son deneme): {timeout_err}")
                    raise
            except Exception as e:
                if attempt < max_retries - 1:
                    logger.warning(f"⚠️ API çağrısı retry {attempt + 1}/{max_retries} (Bekleme: {delay}s): {type(e).__name__}")
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, max_delay)  # Exponential backoff (max_delay'e kadar)
                else:
                    logger.error(f"❌ API çağrısı başarısız (son deneme): {str(e)}")
                    raise

    async def fetch_ohlcv_with_retry(self, symbol: str, timeframe: str = '1h', limit: int = 100):
        """OHLCV verilerini exponential backoff ile retry yaparak çek."""
        return await self._retry_with_backoff(
            lambda: self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit),
            max_retries=3,
            initial_delay=2.0,
            max_delay=10.0
        )

    async def setup_margin_and_leverage(self, symbol: str, leverage: int, margin_type: str = 'ISOLATED') -> bool:
        """İşlem öncesi sembol için Kaldıraç ve Marjin tipini ayarlar. Retry mekanizması ve delay içerir."""
        try:
            # Adım 1: Tüm açık emirleri iptal et (marjin değişikliği için gerekli)
            # Retry mekanizması: emirleri iptal etmeyi 3 kez dene
            cancel_retries = 3
            for attempt in range(cancel_retries):
                try:
                    await self.exchange.cancel_all_orders(symbol)
                    logger.info(f"[{symbol}] Marjin ayarlamadan once tum emirler iptal edildi (Deneme {attempt + 1}/{cancel_retries}).")
                    await asyncio.sleep(0.5)  # Emirlerin iptal işlenebilmesi için kısa bekle
                    break  # Başarılı, döngüden çık
                except Exception as cancel_err:
                    if attempt < cancel_retries - 1:
                        logger.warning(f"[{symbol}] Emirler iptal retry {attempt + 1}/{cancel_retries}: {cancel_err}")
                        await asyncio.sleep(0.2)  # Retry arası kısa gecikme
                    else:
                        logger.warning(f"[{symbol}] Emirler iptal başarısız (son deneme): {cancel_err}")

            # Adım 2: Marjin Tipini Ayarla (Cross / Isolated) - Retry mekanizması
            margin_retries = 3
            for attempt in range(margin_retries):
                try:
                    await self.exchange.set_margin_mode(margin_type, symbol)
                    logger.info(f"[{symbol}] Marjin tipi {margin_type} olarak ayarlandi (Deneme {attempt + 1}/{margin_retries}).")
                    await asyncio.sleep(0.3)  # Marjin ayarının işlenmesi için bekle
                    break  # Başarılı, döngüden çık
                except ccxt.MarginModeAlreadySet:
                    logger.info(f"[{symbol}] Marjin tipi zaten {margin_type} ayarlandı.")
                    break  # Zaten ayarlandıysa döngüden çık
                except Exception as e:
                    if attempt < margin_retries - 1:
                        logger.warning(f"[{symbol}] Marjin ayarı retry {attempt + 1}/{margin_retries}: {str(e)}")
                        await asyncio.sleep(0.2)  # Retry arası gecikme
                    else:
                        logger.warning(f"[{symbol}] Marjin ayarlanırken uyari (son deneme): {str(e)}")

            # Adım 3: Kaldıracı Ayarla - Retry mekanizması
            leverage_retries = 3
            for attempt in range(leverage_retries):
                try:
                    await self.exchange.set_leverage(leverage, symbol)
                    logger.info(f"[{symbol}] Kaldirac {leverage}x olarak ayarlandi (Deneme {attempt + 1}/{leverage_retries}).")
                    await asyncio.sleep(0.3)  # Kaldıraç ayarının işlenmesi için bekle
                    return True  # Başarılı, kaldır
                except Exception as e:
                    if attempt < leverage_retries - 1:
                        logger.warning(f"[{symbol}] Kaldirac ayarı retry {attempt + 1}/{leverage_retries}: {str(e)}")
                        await asyncio.sleep(0.2)  # Retry arası gecikme
                    else:
                        logger.error(f"[{symbol}] Kaldirac ayarlanamadi (son deneme): {str(e)}")
                        return False
        
        except Exception as e:
            logger.error(f"[{symbol}] setup_margin_and_leverage() kritik hatası: {str(e)}")
            return False

    async def close(self):
        """Asenkron bağlantıyı temiz bir şekilde kapatır."""
        await self.exchange.close()
        logger.info("Binance API baglantisi guvenle kapatildi.")