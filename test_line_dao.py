import asyncio
from backend.db.engine import async_session_factory
from backend.db.dao.line_dao import LineDao

async def test():
    async with async_session_factory() as session:
        lines = await LineDao(session=session).get_all()
        print(f'Found {len(lines)} lines')
        for line in lines[:5]:
            print(f'  Line {line.id}: {line.name}')

asyncio.run(test())
