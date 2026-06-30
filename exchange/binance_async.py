# exchange/binance_async.py
import ccxt.async_support as ccxt
import logging

logger = logging.getLogger(__name__)


class BinanceFuturesClient:
    """Binance USDT-M Vadeli İşlemler asenkron bağlantı katmanı."""

    def __init__(self, api_key: str, secret_key: str, testnet: bool = False):
        self.exchange = ccxt.binance({
            'apiKey': api_key,
            'secret': secret_key,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'future',  # Spot değil, USDT-M Futures
                'adjustForTimeDifference': True
            }
        })
        
        if testnet:
            # Demo Trading modu (set_sandbox_mode yerine)
            # Binance Demo Trading: https://developers.binance.com/docs/binance-spot-api-docs/demo-mode
            self.exchange.enable_demo_trading(True)
            logger.info(
                "Binance Futures DEMO TRADING modunda başlatıldı. (Gerçek para KULLANILMIYOR)")
        else:
            logger.warning(
                "DİKKAT: Binance Futures GERÇEK hesapta başlatıldı!")

    async def setup_margin_and_leverage(self, symbol: str, leverage: int, margin_type: str = 'ISOLATED') -> bool:
        """İşlem öncesi sembol için Kaldıraç ve Marjin tipini ayarlar."""
        try:
            # Marjin Tipini Ayarla (Cross / Isolated)
            await self.exchange.set_margin_mode(margin_type, symbol)
            logger.info(
                f"[{symbol}] Marjin tipi {margin_type} olarak ayarlandı.")
        except ccxt.MarginModeAlreadySet:
            pass  # Zaten İzole/Cross ise hatayı yoksay
        except Exception as e:
            logger.warning(f"[{symbol}] Marjin ayarlanırken uyarı: {str(e)}")

        try:
            # Kaldıracı Ayarla
            await self.exchange.set_leverage(leverage, symbol)
            logger.info(f"[{symbol}] Kaldıraç {leverage}x olarak ayarlandı.")
            return True
        except Exception as e:
            logger.error(f"[{symbol}] Kaldıraç ayarlanamadı: {str(e)}")
            return False

    async def close(self):
        """Asenkron bağlantıyı temiz bir şekilde kapatır."""
        await self.exchange.close()
        logger.info("Binance API bağlantısı güvenle kapatıldı.")