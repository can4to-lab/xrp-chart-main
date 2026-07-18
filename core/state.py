import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class ExitReason(Enum):
    STOP = "STOP"
    TP = "TP"
    TRAIL = "TRAIL"
    SIGNAL = "SIGNAL"
    MANUAL = "MANUAL"
    RECONCILIATION = "RECONCILIATION"


@dataclass
class StopSnapshot:
    time: datetime
    side: str
    price: float
    adx: float
    di_plus: float
    di_minus: float
    atr: float
    regime: str


@dataclass
class TradeClosedEvent:
    symbol: str
    side: str
    reason: ExitReason
    price: float
    pnl: float = 0.0
    timestamp: Optional[datetime] = None
    adx: float = 0.0
    di_plus: float = 0.0
    di_minus: float = 0.0
    atr: float = 0.0
    regime: str = "UNKNOWN"
    volume: float = 0.0
    vol_sma: float = 0.0
    btc_trend: str = "UNKNOWN"

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.utcnow()


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
                'tp_taken': False,
                'strategy_type': None,
                'last_stop_snapshot': None,
                'last_exit_reason': None,
            }
        return self._state[symbol]

    def reset_for_new_trade(self, symbol: str):
        """Yeni işlem için durumu sıfırlar, ancak snapshot bilgisi korunur."""
        existing = self._state.get(symbol, {})
        self._state[symbol] = {
            'in_position': False,
            'side': None,
            'entry_price': 0.0,
            'size': 0.0,
            'tp_taken': False,
            'strategy_type': None,
            'last_stop_snapshot': existing.get('last_stop_snapshot'),
            'last_exit_reason': existing.get('last_exit_reason'),
        }

    def set_tp_taken(self, symbol: str):
        """TP sinyali işlendiğinde bayrağı True yapar."""
        if symbol in self._state:
            self._state[symbol]['tp_taken'] = True


state = GlobalState()