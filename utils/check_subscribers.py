import asyncio
from backend.db.engine import async_session_factory
from backend.db.dao.telegram_user_dao import TelegramUserDao

async def check_subscribers():
    async with async_session_factory() as session:
        users = await TelegramUserDao(session=session).get_all_user_ids()
        print(f'Подписчики: {users}')
        return users

if __name__ == "__main__":
    subscribers = asyncio.run(check_subscribers())
    print(f"Найдено подписчиков: {len(subscribers)}") 