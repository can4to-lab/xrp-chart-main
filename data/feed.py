# core/config.py
import os
from dotenv import load_dotenv

# .env dosyasındaki gizli bilgileri sisteme yükler
load_dotenv()


class Config:
    # Borsa Ayarları
    BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
    BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY", "")
    TESTNET = os.getenv("TESTNET", "True").lower() in ("true", "1", "t")

    # Telegram Bildirim Ayarları
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

    # Strateji Ayarları
    TIMEFRAME = "5m"              # Mum periyodu (Örn: 15m, 1h)
    LEVERAGE = 10                 # Varsayılan kaldıraç
    TRADE_SIZE_USDT = 20.0        # İşlem başına kullanılacak marjin (Dolar)


config = Config()
