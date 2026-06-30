import aiohttp
import logging
from core.config import config

logger = logging.getLogger(__name__)

class TelegramNotifier:
    def __init__(self):
        self.bot_token = config.TELEGRAM_BOT_TOKEN
        # Virgülle ayrılmış ID'leri listeye çeviriyoruz
        raw_chat_id = str(config.TELEGRAM_CHAT_ID)
        self.chat_ids = [cid.strip() for cid in raw_chat_id.split(',')] if raw_chat_id else []
        self.api_url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

    async def send_message(self, message: str):
        if not self.bot_token or not self.chat_ids:
            return

        async with aiohttp.ClientSession() as session:
            # Listedeki her bir ID'ye sırayla mesajı gönder
            for chat_id in self.chat_ids:
                if not chat_id:
                    continue
                    
                payload = {
                    "chat_id": chat_id,
                    "text": message,
                    "parse_mode": "HTML"
                }

                try:
                    async with session.post(self.api_url, json=payload) as response:
                        if response.status != 200:
                            logger.warning(f"⚠️ Telegram mesajı ({chat_id}) ID'sine iletilemedi. Kod: {response.status}")
                except Exception as e:
                    logger.error(f"❌ Telegram API Hatası ({chat_id}): {e}")

notifier = TelegramNotifier()