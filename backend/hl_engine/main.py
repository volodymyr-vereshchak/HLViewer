import asyncio
import os
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.dao.daily_archive_dao import DailyArchiveDao
from backend.db.dao.edit_archive_dao import EditArchiveDao
from backend.db.dao.hourly_archive_dao import HourlyArchiveDao
from backend.db.dao.sys_archive_dao import SysArchiveDao
from backend.db.engine import async_session_factory
from backend.db.models import (
    DAILY_ARCHIVE_CONSTRAINT,
    HOURLY_ARCHIVE_CONSTRAINT,
    EDIT_ARCHIVE_CONSTRAINT,
    SYS_ARCHIVE_CONSTRAINT,
)
from backend.hl_engine.daily_engine import DailyEngine
from backend.hl_engine.edit_engine import EditEngine
from backend.hl_engine.hourly_engine import HourlyEngine
from backend.hl_engine.sys_engine import SysEngine
from backend.settings import backend_settings
from utils.files_utils import UnzipUtils


async def bulk_upsert_worker(archives_list, dao, constraint_list):
    async with async_session_factory() as session:
        await dao(session=session).bulk_upsert(archives_list, constraint_list)


async def update_archive(archive_gen, dao, constraint_list: list, session):
    tasks = []

    async for archives_list in archive_gen:
        task = bulk_upsert_worker(archives_list, dao, constraint_list)
        tasks.append(task)

    await asyncio.gather(*tasks)


async def update_worker(
    engine, path: str, archive_dao, constraint, chunk_size: int, session: AsyncSession
):
    archive_engine = engine(path=path, chunk_size=chunk_size, session=session)
    archives_gen = archive_engine.read()
    await update_archive(archives_gen, archive_dao, constraint, session)


async def update_hostlibs(session: AsyncSession):
    # current_directory = os.getcwd()
    # path = os.path.join(current_directory, backend_settings.get("HOSTLIB_PATH"))
    path = backend_settings.get("HOSTLIB_PATH")
    chunk_size = backend_settings.get("CHUNK_SIZE")

    with UnzipUtils(path) as unzip_utils:
        workers = [
            (DailyEngine, DailyArchiveDao, DAILY_ARCHIVE_CONSTRAINT),
            (HourlyEngine, HourlyArchiveDao, HOURLY_ARCHIVE_CONSTRAINT),
            (EditEngine, EditArchiveDao, EDIT_ARCHIVE_CONSTRAINT),
            (SysEngine, SysArchiveDao, SYS_ARCHIVE_CONSTRAINT),
        ]

        for engine, archive_dao, constraint in workers:
            await update_worker(
                engine,
                unzip_utils.temp_path,
                archive_dao,
                constraint,
                chunk_size,
                session,
            )


if __name__ == "__main__":
    # path_dir = "D:/Projects/HLViewer/HLViewer/develop_data/ASK/hostlib"
    async def update():
        async with async_session_factory() as session:
            await update_hostlibs(session=session)

    asyncio.run(update())
