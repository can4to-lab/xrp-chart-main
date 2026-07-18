import pytest

from core.database import db


class DummyConn:
    def __init__(self):
        self.query = None
        self.args = None

    async def execute(self, query, *args):
        self.query = query
        self.args = args

    async def fetchval(self, query, *args):
        self.query = query
        self.args = args
        return 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class DummyPool:
    def __init__(self):
        self.conn = DummyConn()

    def acquire(self):
        return self

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_insert_trade_diagnostic_uses_database_table():
    db.pool = DummyPool()

    await db.insert_trade_diagnostic(
        symbol="SOLUSDT",
        side="LONG",
        pnl=-50.0,
        reason="STOP",
        adx=18.0,
        regime="RANGE",
        volume=1500.0,
        vol_sma=2000.0,
        atr=1.1,
        btc_trend="SIDEWAYS",
    )

    assert "INSERT INTO trade_diagnostics" in db.pool.conn.query
    assert db.pool.conn.args[0] == "SOLUSDT"
