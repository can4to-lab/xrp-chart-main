import asyncpg
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

        try:
            self.pool = await asyncpg.create_pool(db_url)
            logger.info("🗄️ PostgreSQL Veritabanı bağlantısı başarıyla kuruldu.")
            await self._create_tables()
        except Exception as e:
            logger.error(f"❌ Veritabanı bağlantı hatası: {e}")

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
            status VARCHAR(10) DEFAULT 'OPEN',
            pnl FLOAT DEFAULT 0.0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
        
        # Eğer tablo önceden oluşturulduysa, gerekli sütunları ekle (Migration)
        query_migration_close = "ALTER TABLE trades ADD COLUMN IF NOT EXISTS close_price FLOAT;"
        query_migration_stop_id = "ALTER TABLE trades ADD COLUMN IF NOT EXISTS stop_order_id VARCHAR(50);"
        
        if self.pool:
            async with self.pool.acquire() as conn:
                await conn.execute(query_table)
                await conn.execute(query_migration_close)
                await conn.execute(query_migration_stop_id)
                logger.debug("Veritabanı tabloları ve şeması kontrol edildi/güncellendi.")

    async def insert_trade(self, symbol: str, side: str, leverage: int, size: float, entry_price: float, stop_price: float) -> int:
        """Yeni açılan bir işlemi 'OPEN' (Açık) statüsüyle veritabanına kaydeder."""
        if not self.pool: return None
        
        query = """
            INSERT INTO trades (symbol, side, leverage, size, entry_price, stop_price)
            VALUES ($1, $2, $3, $4, $5, $6) RETURNING id
        """
        async with self.pool.acquire() as conn:
            trade_id = await conn.fetchval(query, symbol, side, leverage, size, entry_price, stop_price)
            logger.debug(f"[{symbol}] 💾 Yeni işlem veritabanına kaydedildi. (ID: {trade_id})")
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
        
        query = "SELECT id, side, size, entry_price, stop_price FROM trades WHERE symbol = $1 AND status = 'OPEN' LIMIT 1"
        async with self.pool.acquire() as conn:
            record = await conn.fetchrow(query, symbol)
            return dict(record) if record else None

    async def get_all_open_trades(self) -> list:
        """Veritabanındaki tüm açık işlemleri getirir (Self-Healing için)."""
        if not self.pool: return []
        
        query = "SELECT id, symbol, side, leverage, size, entry_price, stop_price FROM trades WHERE status = 'OPEN'"
        async with self.pool.acquire() as conn:
            records = await conn.fetch(query)
            return [dict(record) for record in records]

    async def update_trade_stop(self, symbol: str, new_stop_price: float, stop_order_id: str = None):
        """Açık işlemin stop loss fiyatını ve order ID'sini günceller."""
        if not self.pool: return
        
        if stop_order_id:
            query = """
                UPDATE trades 
                SET stop_price = $1, stop_order_id = $2, updated_at = CURRENT_TIMESTAMP
                WHERE symbol = $3 AND status = 'OPEN'
            """
            async with self.pool.acquire() as conn:
                await conn.execute(query, new_stop_price, stop_order_id, symbol)
                logger.debug(f"[{symbol}] 💾 Stop loss güncellendi: {new_stop_price:.4f} (Order ID: {stop_order_id})")
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
