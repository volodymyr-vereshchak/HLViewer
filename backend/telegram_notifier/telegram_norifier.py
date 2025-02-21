import asyncio
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message

from backend.db.dao.telegram_user_dao import TelegramUserDao
from backend.db.engine import async_session_factory
from backend.db.models.telegram_user_model import TelegramUserCreate
from backend.settings import backend_settings
from utils.logger import logger_setup


class TelegramBot:
    def __init__(self):
        self.token = backend_settings.get("BOT_TOKEN")
        self.bot = Bot(token=self.token)
        self.dp = Dispatcher(storage=MemoryStorage())
        self.logger = logger_setup("backend")
        self.users_dao = TelegramUserDao
        self.subscribers = set()

        self.dp.message.register(self.start, Command("start"))
        self.dp.message.register(self.stop, Command("stop"))
        self.dp.message.register(self.unknown_command)

    async def start(self, message: Message):
        """Add subscriber"""
        try:
            async with async_session_factory() as session:
                result = await self.users_dao(session=session).activate_user_or_create(
                    message.from_user.id
                )
            if result:
                await message.answer("Вы подписались на уведомления по ГРС!")
            else:
                await message.answer("Вы уже подписаны на обновления!")
        except Exception as e:
            await message.answer(
                "Произошла ошибка при попытке подписки, попробуйте позже!"
            )

    async def stop(self, message: Message):
        """Delete subscriber"""
        try:
            async with async_session_factory() as session:
                result = await self.users_dao(
                    session=session
                ).deactivate_user_by_user_id(message.from_user.id)
            if result:
                await message.answer("Вы отписались от уведомлений.")
            else:
                await message.answer("Вы не были подписаны на уведомления!")
        except Exception as e:
            await message.answer(
                "Произошла ошибка при попытке отписаться, попробуйте позже!"
            )

    @staticmethod
    async def unknown_command(message: Message):
        await message.answer("Неизвестная команда. Используйте /start или /stop.")

    async def send_updates(self, message: str):
        """Send message to all subscribers"""
        async with async_session_factory() as session:
            subscribers = await self.users_dao(session=session).get_all_user_ids()

        tasks = []
        for user_id in subscribers:
            tasks.append(self._send_message(user_id, message))

        await asyncio.gather(*tasks)

    async def _send_message(self, user_id: int, message: str):
        try:
            await self.bot.send_message(user_id, message, parse_mode="HTML")
        except Exception as e:
            self.logger.error(f"Ошибка отправки сообщения {user_id}: {e}")

    async def run(self):
        """Run bot"""
        try:
            await self.dp.start_polling(self.bot)
        except Exception as e:
            self.logger.error(f"Ошибка запуска бота: {e}")

    async def stop_bot(self):
        """Stop bot"""
        await self.bot.close()
        await self.dp.stop_polling()


if __name__ == "__main__":
    asyncio.run(TelegramBot().run())
