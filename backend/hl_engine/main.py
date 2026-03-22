import asyncio
import logging
import os
from types import SimpleNamespace
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

logger = logging.getLogger(__name__)

from backend.db.dao.daily_archive_dao import DailyArchiveDao
from backend.db.dao.edit_archive_dao import EditArchiveDao
from backend.db.dao.hourly_archive_dao import HourlyArchiveDao
from backend.db.dao.param_dao import ParamDao
from backend.db.dao.sys_archive_dao import SysArchiveDao
from backend.db.engine import async_session_factory, update_session_factory
from backend.db.models import (
    DAILY_ARCHIVE_CONSTRAINT,
    HOURLY_ARCHIVE_CONSTRAINT,
    EDIT_ARCHIVE_CONSTRAINT,
    SYS_ARCHIVE_CONSTRAINT,
    PARAM_CONSTRAINT,
)
from backend.db.models.lumg_model import LumgDataPath, LumgEisCode
from backend.hl_engine.daily_engine import DailyEngine
from backend.hl_engine.edit_engine import EditEngine
from backend.hl_engine.hourly_engine import HourlyEngine
from backend.hl_engine.param_engine import ParamEngine
from backend.hl_engine.sys_engine import SysEngine
from backend.settings import backend_settings
from utils.files_utils import UnzipUtils


async def update_archive(archive_gen, dao, constraint_list: list, session):
    all_records = []
    async for archives_list in archive_gen:
        all_records.extend(archives_list)
    await dao(session=session).bulk_upsert_via_copy(all_records, constraint_list)


async def update_worker(
    engine, path: str, archive_dao, constraint, chunk_size: int, session: AsyncSession, lumg_id: int = 1
):
    archive_engine = engine(path=path, chunk_size=chunk_size, session=session, lumg_id=lumg_id)
    archives_gen = archive_engine.read()
    await update_archive(archives_gen, archive_dao, constraint, session)


async def update_hostlibs(session: AsyncSession, lumg_id: int | None = None, progress: dict | None = None):
    query = select(LumgDataPath).where(LumgDataPath.active == True)
    if lumg_id is not None:
        query = query.where(LumgDataPath.lumg_id == lumg_id)
    result = await session.execute(query)
    lumg_paths = result.scalars().all()

    # Fallback to env var for backwards compatibility (if table is empty)
    if not lumg_paths:
        env_path = backend_settings.get("HOSTLIB_PATH")
        if env_path:
            lumg_paths = [SimpleNamespace(lumg_id=1, path=env_path)]

    if not lumg_paths:
        logger.warning("No active LumgDataPath found in DB and no HOSTLIB_PATH env var set")
        return

    if progress is not None:
        for lp in lumg_paths:
            progress[lp.lumg_id] = "pending"

    chunk_size = backend_settings.get("CHUNK_SIZE")
    workers = [
        (DailyEngine, DailyArchiveDao, DAILY_ARCHIVE_CONSTRAINT),
        (HourlyEngine, HourlyArchiveDao, HOURLY_ARCHIVE_CONSTRAINT),
        (EditEngine, EditArchiveDao, EDIT_ARCHIVE_CONSTRAINT),
        (SysEngine, SysArchiveDao, SYS_ARCHIVE_CONSTRAINT),
        (ParamEngine, ParamDao, PARAM_CONSTRAINT),
    ]

    # Load all EIS code → lumg_id mappings once
    eis_result = await session.execute(select(LumgEisCode))
    eis_to_lumg: dict[str, int] = {e.eis_code: e.lumg_id for e in eis_result.scalars().all()}

    def _find_eis_dirs(base_path: str) -> list[tuple[str, int]]:
        """Walk subdirs recursively, return (abs_path, lumg_id) for EIS-mapped folders."""
        results = []
        for root, dirs, _ in os.walk(base_path):
            for d in dirs:
                if d in eis_to_lumg:
                    results.append((os.path.join(root, d), eis_to_lumg[d]))
        return results

    _sem = asyncio.Semaphore(6)

    async def _process_lumg(lumg_path):
        if progress is not None:
            progress[lumg_path.lumg_id] = "queued"
        async with _sem:
            if progress is not None:
                progress[lumg_path.lumg_id] = "running"
            if not os.path.exists(lumg_path.path):
                logger.error(f"Hostlib path does not exist: {lumg_path.path!r} — skipping lumg_id={lumg_path.lumg_id}")
                if progress is not None:
                    progress[lumg_path.lumg_id] = "error"
                return
            try:
                async with UnzipUtils(lumg_path.path) as unzip_utils:
                    eis_dirs = _find_eis_dirs(unzip_utils.temp_path)
                    if eis_dirs:
                        # Mode 1: EIS routing — each folder → its LUMG
                        for eis_path, resolved_lumg_id in eis_dirs:
                            for engine, archive_dao, constraint in workers:
                                async with update_session_factory() as worker_session:
                                    await update_worker(engine, eis_path, archive_dao, constraint, chunk_size, worker_session, resolved_lumg_id)
                    else:
                        # Mode 2: direct — all files → this LUMG
                        for engine, archive_dao, constraint in workers:
                            async with update_session_factory() as worker_session:
                                await update_worker(engine, unzip_utils.temp_path, archive_dao, constraint, chunk_size, worker_session, lumg_path.lumg_id)
                if progress is not None:
                    progress[lumg_path.lumg_id] = "done"
            except Exception as e:
                logger.error(f"Error updating lumg_id={lumg_path.lumg_id}: {e}", exc_info=True)
                if progress is not None:
                    progress[lumg_path.lumg_id] = "error"

    await asyncio.gather(*[_process_lumg(p) for p in lumg_paths])


async def update_direct(path: str, lumg_id: int, session: AsyncSession, progress: dict | None = None):
    """Mode 2 forced: read all files from path directly into lumg_id, ignoring EIS codes."""
    if progress is not None:
        progress[lumg_id] = "running"

    chunk_size = backend_settings.get("CHUNK_SIZE")
    workers = [
        (DailyEngine, DailyArchiveDao, DAILY_ARCHIVE_CONSTRAINT),
        (HourlyEngine, HourlyArchiveDao, HOURLY_ARCHIVE_CONSTRAINT),
        (EditEngine, EditArchiveDao, EDIT_ARCHIVE_CONSTRAINT),
        (SysEngine, SysArchiveDao, SYS_ARCHIVE_CONSTRAINT),
        (ParamEngine, ParamDao, PARAM_CONSTRAINT),
    ]

    if not os.path.exists(path):
        logger.error(f"Direct update path does not exist: {path!r}")
        if progress is not None:
            progress[lumg_id] = "error"
        return

    try:
        async with UnzipUtils(path) as unzip_utils:
            for engine, archive_dao, constraint in workers:
                async with update_session_factory() as worker_session:
                    await update_worker(engine, unzip_utils.temp_path, archive_dao, constraint, chunk_size, worker_session, lumg_id)
        if progress is not None:
            progress[lumg_id] = "done"
    except Exception as e:
        logger.error(f"Error in direct update lumg_id={lumg_id}: {e}", exc_info=True)
        if progress is not None:
            progress[lumg_id] = "error"


if __name__ == "__main__":

    async def update():
        async with async_session_factory() as session:
            await update_hostlibs(session=session)

    asyncio.run(update())
