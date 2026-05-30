import asyncio
from pyinstrument import Profiler
from backend.db.engine import async_session_factory
from backend.hl_engine.main import update_hostlibs

async def run():
    profiler = Profiler(async_mode='enabled')
    profiler.start()

    async with async_session_factory() as session:
        await update_hostlibs(session=session, lumg_id=2)

    profiler.stop()
    print(profiler.output_text(unicode=True, color=False, timeline=False, show_all=False))

asyncio.run(run())
