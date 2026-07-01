import logging
from core.config import config

logger = logging.getLogger(__name__)

class RiskManager:
    """
    Kasa (Bakiye) kontrolü, risk yönetimi ve işleme girilecek
    kripto miktarının (Size) hesaplanmasından sorumlu modül.
    """

    def __init__(self, client):
        self.client = client

    async def get_available_balance(self, asset: str = "USDT") -> float:
        """Binance Futures hesabındaki kullanılabilir (boşta duran) bakiyeyi çeker."""
        try:
            # CCXT üzerinden vadeli işlemler cüzdanını sorgula
            balance = await self.client.exchange.fetch_balance()
            free_balance = float(balance.get(asset, {}).get('free', 0.0))
            logger.debug(f"💰 Kullanılabilir Kasa Bakiyesi: {free_balance:.2f} {asset}")
            return free_balance
        except Exception as e:
            logger.error(f"❌ Bakiye çekilirken hata oluştu: {str(e)}")
            return 0.0

    async def calculate_margin(self, symbol: str) -> float:
        """
        Giriş sinyali geldiğinde o sembol için kullanılacak marjin miktarını 
        kasa durumuna ve config ayarlarına göre hesaplar.
        """
        try:
            available_balance = await self.get_available_balance()
            margin_to_use = float(config.TRADE_SIZE_USDT)

            # Eğer kasada config'de belirlediğimiz kadar (örneğin 20 USDT) yoksa:
            if available_balance < margin_to_use:
                logger.warning(f"[{symbol}] ⚠️ Yetersiz bakiye! İstenen: {margin_to_use} USDT, Mevcut: {available_balance:.2f} USDT")
                
                # Eğer mevcut bakiye 5 doların (Binance minimum işlem tutarı) altındaysa işlemi tamamen iptal et
                if available_balance < 5.0:
                    logger.error(f"[{symbol}] 🚫 Bakiye 5 USDT'nin altında olduğu için işlem pas geçildi.")
                    return 0.0
                
                # 5 dolardan fazlaysa, mevcuttaki tüm parayı (komisyon payı bırakarak) kullan
                margin_to_use = available_balance * 0.95 
                logger.info(f"[{symbol}] 💡 Mevcut kasanın %95'i ({margin_to_use:.2f} USDT) marjin olarak kullanılacak.")

            return margin_to_use
        except Exception as e:
            logger.error(f"[{symbol}] ❌ Marjin hesaplama hatası: {str(e)}")
            return 0.0

    async def calculate_position_size(self, symbol: str, entry_price: float) -> float:
        """
        Config dosyasındaki ayarları ve cüzdandaki parayı kontrol ederek
        kaç adet coin alınması/satılması gerektiğini hesaplar.
        """
        try:
            # 1. Kasadaki kullanılabilir bakiyeyi kontrol et
            margin_to_use = await self.calculate_margin(symbol)
            if margin_to_use == 0.0:
                return 0.0

            leverage = float(config.LEVERAGE)

            # 2. İşlem Büyüklüğünü (Kaldıraçlı Toplam Alım Gücü - Notional Value) Hesapla
            # Örnek: 20 USDT * 10x Kaldıraç = 200 USDT'lik işlem gücü
            notional_value = margin_to_use * leverage

            # 3. Alınacak Coin Adedini Hesapla
            # Örnek: 200 USDT / 0.50 (SOL Fiyatı) = 400 adet SOL
            raw_size = notional_value / entry_price
            
            # Not: Ondalık (precision) kırpmayı Engine dosyamız otomatik yaptığı için
            # biz burada düz matematik sonucunu döndürüyoruz.
            return raw_size

        except Exception as e:
            logger.error(f"[{symbol}] ❌ Büyüklük (Size) hesaplama hatası: {str(e)}")
            return 0.0
