import asyncio
from backend.db.engine import async_session_factory
from backend.db.dao.line_dao import LineDao

async def check_lines():
    async with async_session_factory() as session:
        dao = LineDao(session=session)
        lines = await dao.get_all()
        print(f'Всего линий в БД: {len(lines)}')
        print('ID и названия линий:')
        for line in lines:
            print(f'ID: {line.id}, name: {line.name}')

if __name__ == "__main__":
    asyncio.run(check_lines()) 