import asyncio
from aiogram import Bot

from backend.settings import backend_settings
from utils.logger import logger_setup


class TelegramNotifier:
    def __init__(self) -> None:
        self.logger = logger_setup("backend")
        self.token = backend_settings.get("BOT_TOKEN")
        self.chat_id = backend_settings.get("CHAT_ID")
        self.bot = Bot(token=self.token)

    async def send_message(self, message):
        try:
            async with self.bot as bot:
                await bot.send_message(
                    chat_id=self.chat_id, text=message, parse_mode="HTML"
                )

        except Exception as e:
            self.logger.error(f"Failed to send message to {self.chat_id}:\n{e}")


if __name__ == "__main__":
    asyncio.run(TelegramNotifier().send_message("Hi, group!"))
