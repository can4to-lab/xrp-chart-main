import os
from dotenv import load_dotenv

# .env dosyasındaki gizli bilgileri sisteme yükler
load_dotenv()

class Config:
    # Borsa Ayarları
    BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "").strip()
    BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY", "").strip()
    TESTNET = os.getenv("TESTNET", "True").lower() in ("true", "1", "t")
    
    # Telegram Bildirim Ayarları
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    
    # Strateji Ayarları
    TIMEFRAME = "15m"              # Mum periyodu
    LEVERAGE = 10                 # Varsayılan kaldıraç
    TRADE_SIZE_USDT = 20.0        # İşlem başına kullanılacak marjin (Dolar)

config = Config()