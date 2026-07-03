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

    # Ölçeklenebilirlik ve eşzamanlılık ayarları
    MAX_CONCURRENT_API_CALLS = int(os.getenv("MAX_CONCURRENT_API_CALLS", "4"))
    API_TIMEOUT_SECONDS = float(os.getenv("API_TIMEOUT_SECONDS", "15"))
    DB_POOL_MIN_SIZE = int(os.getenv("DB_POOL_MIN_SIZE", "1"))
    DB_POOL_MAX_SIZE = int(os.getenv("DB_POOL_MAX_SIZE", "10"))
    EXECUTION_QUEUE_SIZE = int(os.getenv("EXECUTION_QUEUE_SIZE", "100"))
    
    # Strateji Ayarları
    TIMEFRAME = "15m"              # Mum periyodu
    LEVERAGE = 10                 # Varsayılan kaldıraç
    TRADE_SIZE_USDT = 20.0        # İşlem başına kullanılacak marjin (Dolar)

config = Config()