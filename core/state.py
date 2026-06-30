import asyncio
import logging

logger = logging.getLogger(__name__)

class GlobalState:
    """
    Sistem genelindeki asenkron kilitleri tutan Singleton sınıf. 
    Aynı anda çift emir gönderilmesini engeller (Race Condition koruması).
    """
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(GlobalState, cls).__new__(cls)
            cls._instance._init_state()
        return cls._instance

    def _init_state(self):
        # İşlem semaforları: Her sembol için özel bir kilit oluşturur
        self._locks = {}

    def get_symbol_lock(self, symbol: str) -> asyncio.Lock:
        """Belirli bir sembol için asenkron kilit döndürür."""
        if symbol not in self._locks:
            self._locks[symbol] = asyncio.Lock()
        return self._locks[symbol]

state = GlobalState()