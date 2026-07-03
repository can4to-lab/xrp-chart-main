import asyncpg
import asyncio
import logging
import os
from core.config import config

logger = logging.getLogger(__name__)

class Database:
    """PostgreSQL bağlantı havuzunu ve işlem kayıtlarını yöneten SSOT sınıfı."""
    
    def __init__(self):
        self.pool = None

    async def connect(self):
        """Veritabanına bağlanır ve tabloları otomatik oluşturur."""
        db_url = os.getenv("DATABASE_URL")
        
        if not db_url:
            logger.warning("⚠️ DATABASE_URL bulunamadı! Veritabanı işlemleri pas geçilecek (Sadece test modu).")
            return

        delay = 1.0
        for attempt in range(3):
            try:
                self.pool = await asyncpg.create_pool(
                    dsn=db_url,
                    min_size=config.DB_POOL_MIN_SIZE,
                    max_size=config.DB_POOL_MAX_SIZE,
                    max_inactive_connection_lifetime=300,
                    command_timeout=60,
                    statement_cache_size=100
                )
                logger.info("🗄️ PostgreSQL Veritabanı bağlantısı başarıyla kuruldu.")
                await self._create_tables()
                return
            except Exception as e:
                logger.error(f"❌ Veritabanı bağlantı hatası (deneme {attempt + 1}/3): {e}")
                if attempt < 2:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 10)
                else:
                    logger.critical("❌ Veritabanına bağlanılamıyor. Bot sınırlı modda çalışacak.")
                    self.pool = None
                    return

    async def _create_tables(self):
        """İşlemlerin tutulacağı 'trades' tablosunu oluşturur (Yoksa yaratır)."""
        query_table = """
        CREATE TABLE IF NOT EXISTS trades (
            id SERIAL PRIMARY KEY,
            symbol VARCHAR(20) NOT NULL,
            side VARCHAR(10) NOT NULL,
            leverage INT NOT NULL,
            size FLOAT NOT NULL,
            entry_price FLOAT NOT NULL,
            stop_price FLOAT,
            close_price FLOAT,
            stop_order_id VARCHAR(100),
            tp_taken BOOLEAN NOT NULL DEFAULT FALSE,
            strategy_type VARCHAR(50) NOT NULL DEFAULT 'UNKNOWN',
            status VARCHAR(20) NOT NULL DEFAULT 'OPEN',
            pnl FLOAT DEFAULT 0.0,
            reason VARCHAR(255),
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
        
        if self.pool:
            async with self.pool.acquire() as conn:
                await conn.execute(query_table)
                await conn.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS tp_taken BOOLEAN NOT NULL DEFAULT FALSE;")
                await conn.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS strategy_type VARCHAR(50) NOT NULL DEFAULT 'UNKNOWN';")
                await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_symbol_open ON trades (symbol) WHERE status = 'OPEN';")
                logger.debug("Veritabanı tabloları ve şeması kontrol edildi/güncellendi.")

    async def insert_pending_trade(self, symbol: str, side: str, leverage: int, size: float, entry_price: float, stop_price: float) -> int:
        """Yeni açılan bir işlemi 'PENDING' statüsüyle veritabanına kaydeder."""
        if not self.pool:
            return None

        query = """
            INSERT INTO trades (symbol, side, leverage, size, entry_price, stop_price, status)
            VALUES ($1, $2, $3, $4, $5, $6, 'PENDING') RETURNING id
        """
        async with self.pool.acquire() as conn:
            trade_id = await conn.fetchval(query, symbol, side, leverage, size, entry_price, stop_price)
            logger.debug(f"[{symbol}] 💾 Yeni PENDING işlem kaydı oluşturuldu. (ID: {trade_id})")
            return trade_id

    async def confirm_trade(self, trade_id: int, stop_order_id: str):
        """PENDING kaydı OPEN olarak onaylar ve stop order kimliğini ekler."""
        if not self.pool:
            return

        query = """
            UPDATE trades
            SET status = 'OPEN', stop_order_id = $1, updated_at = CURRENT_TIMESTAMP
            WHERE id = $2
        """
        async with self.pool.acquire() as conn:
            await conn.execute(query, stop_order_id, trade_id)
            logger.debug(f"[trade_id={trade_id}] 💾 PENDING işlem OPEN olarak onaylandı.")

    async def mark_trade_failed(self, symbol: str, close_price: float = None, pnl: float = 0.0, reason: str = None):
        """PENDING veya başarısız işlemi FAILED statüsüyle kaydeder."""
        if not self.pool:
            return

        query = """
            UPDATE trades
            SET status = 'FAILED', close_price = $1, pnl = $2, reason = $3, updated_at = CURRENT_TIMESTAMP
            WHERE symbol = $4 AND status IN ('PENDING', 'OPEN')
        """
        async with self.pool.acquire() as conn:
            await conn.execute(query, close_price, pnl, reason, symbol)
            logger.debug(f"[{symbol}] 💾 İşlem FAILED olarak kaydedildi. Reason: {reason}")

    async def insert_trade(self, symbol: str, side: str, leverage: int, size: float, entry_price: float, stop_price: float, strategy_type: str = 'UNKNOWN') -> int:
        """Yeni açılan bir işlemi 'OPEN' (Açık) statüsüyle veritabanına kaydeder."""
        if not self.pool:
            return None
        
        query = """
            INSERT INTO trades (symbol, side, leverage, size, entry_price, stop_price, strategy_type)
            VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id
        """
        async with self.pool.acquire() as conn:
            trade_id = await conn.fetchval(query, symbol, side, leverage, size, entry_price, stop_price, strategy_type)
            logger.debug(f"[{symbol}] 💾 Yeni işlem veritabanına kaydedildi. (ID: {trade_id}, strategy_type={strategy_type})")
            return trade_id

    async def close_trade(self, symbol: str, close_price: float, pnl: float):
        """Açık olan işlemi bulur, 'CLOSED' olarak işaretler ve Kâr/Zarar (PnL) ile kapanış fiyatını yazar."""
        if not self.pool: return
        
        query = """
            UPDATE trades 
            SET status = 'CLOSED', pnl = $1, close_price = $2, updated_at = CURRENT_TIMESTAMP
            WHERE symbol = $3 AND status = 'OPEN'
        """
        async with self.pool.acquire() as conn:
            await conn.execute(query, pnl, close_price, symbol)
            logger.debug(f"[{symbol}] 💾 İşlem veritabanında kapatıldı. (Fiyat: {close_price:.4f}, PnL: {pnl:.2f} USDT)")

    async def get_open_trade(self, symbol: str) -> dict:
        """Belirli bir sembol için açık işlem olup olmadığını kontrol eder."""
        if not self.pool: return None
        
        query = "SELECT id, side, size, entry_price, stop_price, stop_order_id, tp_taken, strategy_type FROM trades WHERE symbol = $1 AND status = 'OPEN' LIMIT 1"
        async with self.pool.acquire() as conn:
            record = await conn.fetchrow(query, symbol)
            return dict(record) if record else None

    async def get_all_open_trades(self) -> list:
        """Veritabanındaki tüm açık işlemleri getirir (Self-Healing için)."""
        if not self.pool: return []
        
        query = "SELECT id, symbol, side, leverage, size, entry_price, stop_price, stop_order_id, tp_taken, strategy_type FROM trades WHERE status = 'OPEN'"
        async with self.pool.acquire() as conn:
            records = await conn.fetch(query)
            return [dict(record) for record in records]

    async def update_trade_stop(self, symbol: str, new_stop_price: float, stop_order_id: str = None, tp_taken: bool = None):
        """Açık işlemin stop loss fiyatını, order ID'sini ve TP durumunu günceller."""
        if not self.pool: return
        
        if stop_order_id is not None and tp_taken is not None:
            query = """
                UPDATE trades 
                SET stop_price = $1, stop_order_id = $2, tp_taken = $3, updated_at = CURRENT_TIMESTAMP
                WHERE symbol = $4 AND status = 'OPEN'
            """
            async with self.pool.acquire() as conn:
                await conn.execute(query, new_stop_price, stop_order_id, tp_taken, symbol)
                logger.debug(f"[{symbol}] 💾 Stop loss ve TP durumu güncellendi: {new_stop_price:.4f}, tp_taken={tp_taken}")
        elif stop_order_id is not None:
            query = """
                UPDATE trades 
                SET stop_price = $1, stop_order_id = $2, updated_at = CURRENT_TIMESTAMP
                WHERE symbol = $3 AND status = 'OPEN'
            """
            async with self.pool.acquire() as conn:
                await conn.execute(query, new_stop_price, stop_order_id, symbol)
                logger.debug(f"[{symbol}] 💾 Stop loss güncellendi: {new_stop_price:.4f} (Order ID: {stop_order_id})")
        elif tp_taken is not None:
            query = """
                UPDATE trades 
                SET tp_taken = $1, updated_at = CURRENT_TIMESTAMP
                WHERE symbol = $2 AND status = 'OPEN'
            """
            async with self.pool.acquire() as conn:
                await conn.execute(query, tp_taken, symbol)
                logger.debug(f"[{symbol}] 💾 TP durumu güncellendi: tp_taken={tp_taken}")
        else:
            query = """
                UPDATE trades 
                SET stop_price = $1, updated_at = CURRENT_TIMESTAMP
                WHERE symbol = $2 AND status = 'OPEN'
            """
            async with self.pool.acquire() as conn:
                await conn.execute(query, new_stop_price, symbol)
                logger.debug(f"[{symbol}] 💾 Stop loss fiyatı güncellendi: {new_stop_price:.4f}")
    
    async def close(self):
        """Bağlantı havuzunu güvenle kapatır."""
        if self.pool:
            await self.pool.close()
            logger.info("🗄️ Veritabanı bağlantısı güvenle kapatıldı.")

db = Database()
