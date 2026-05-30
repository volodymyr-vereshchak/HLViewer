import asyncio
import logging
import os
import shutil
from collections import defaultdict
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
from backend.utils.path_utils import resolve_stored_path
from backend.hl_engine.edit_engine import EditEngine
from backend.hl_engine.hourly_engine import HourlyEngine
from backend.hl_engine.param_engine import ParamEngine
from backend.hl_engine.sys_engine import SysEngine
from backend.settings import backend_settings
from utils.files_utils import UnzipUtils


def _cleanup_orphan_temp_dirs():
    """Remove leftover __temp_*__ dirs from previous crashes or force-kills."""
    hostlibs_root = os.path.join(os.getcwd(), "hostlibs")
    if not os.path.exists(hostlibs_root):
        return
    for name in os.listdir(hostlibs_root):
        if name.startswith("__temp_") and name.endswith("__"):
            path = os.path.join(hostlibs_root, name)
            try:
                shutil.rmtree(path)
                logger.info(f"Cleaned up orphan temp dir: {path}")
            except Exception as e:
                logger.warning(f"Failed to clean orphan temp dir {path}: {e}")


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


WORKERS = [
    (DailyEngine,  DailyArchiveDao,  DAILY_ARCHIVE_CONSTRAINT),
    (HourlyEngine, HourlyArchiveDao, HOURLY_ARCHIVE_CONSTRAINT),
    (EditEngine,   EditArchiveDao,   EDIT_ARCHIVE_CONSTRAINT),
    (SysEngine,    SysArchiveDao,    SYS_ARCHIVE_CONSTRAINT),
    (ParamEngine,  ParamDao,         PARAM_CONSTRAINT),
]


async def _run_engine(engine, path, archive_dao, constraint, chunk_size, lumg_id_val):
    """Run a single engine in its own session."""
    async with update_session_factory() as worker_session:
        await update_worker(engine, path, archive_dao, constraint, chunk_size, worker_session, lumg_id_val)


async def _run_all_engines_parallel(path: str, lumg_id_val: int, chunk_size: int):
    """Run all 5 archive engines in parallel for one (path, lumg_id) pair."""
    await asyncio.gather(*[
        _run_engine(engine, path, archive_dao, constraint, chunk_size, lumg_id_val)
        for engine, archive_dao, constraint in WORKERS
    ])


async def update_hostlibs(session: AsyncSession, lumg_id: int | None = None, progress: dict | None = None):
    _cleanup_orphan_temp_dirs()
    query = select(LumgDataPath).where(LumgDataPath.active == True)
    if lumg_id is not None:
        query = query.where(LumgDataPath.lumg_id == lumg_id)
    result = await session.execute(query)
    lumg_paths = result.scalars().all()

    if not lumg_paths:
        logger.warning("No active LumgDataPath found in DB — skipping hostlib update")
        return

    if progress is not None:
        for lp in lumg_paths:
            progress[lp.lumg_id] = "pending"

    chunk_size = backend_settings.get("CHUNK_SIZE")

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

    async def _process_lumg_direct(lumg_path, temp_path: str):
        """Mode 2: write all files from temp_path directly into this LUMG (no EIS routing)."""
        if progress is not None:
            progress[lumg_path.lumg_id] = "queued"
        async with _sem:
            if progress is not None:
                progress[lumg_path.lumg_id] = "running"
            try:
                await _run_all_engines_parallel(temp_path, lumg_path.lumg_id, chunk_size)
                if progress is not None:
                    progress[lumg_path.lumg_id] = "done"
            except Exception as e:
                logger.error(f"Error updating lumg_id={lumg_path.lumg_id}: {e}", exc_info=True)
                if progress is not None:
                    progress[lumg_path.lumg_id] = "error"

    async def _process_path_group(path: str, lumg_path_list: list):
        """Unzip once, find EIS dirs once, route data correctly."""
        if not os.path.exists(path):
            logger.error(f"Hostlib path does not exist: {path!r} - skipping {len(lumg_path_list)} LUMGs")
            if progress is not None:
                for lp in lumg_path_list:
                    progress[lp.lumg_id] = "error"
            return
        try:
            async with UnzipUtils(path) as unzip_utils:
                logger.info(f"Unzipped {path!r} -> {unzip_utils.temp_path} (shared by {len(lumg_path_list)} LUMGs)")
                eis_dirs = _find_eis_dirs(unzip_utils.temp_path)
                if eis_dirs:
                    # Mode 1: EIS routing — discover dirs ONCE for the whole path group,
                    # then process each dir exactly once. Previously each LUMG called
                    # _find_eis_dirs independently, causing N×M redundant writes.
                    targeted_ids = {lid for _, lid in eis_dirs}
                    # Path-owner LUMGs with no EIS codes in this path: nothing to do.
                    if progress is not None:
                        for lp in lumg_path_list:
                            if lp.lumg_id not in targeted_ids:
                                progress[lp.lumg_id] = "done"
                    async with _sem:
                        if progress is not None:
                            for lid in targeted_ids:
                                progress[lid] = "running"
                        try:
                            for eis_path, resolved_lumg_id in eis_dirs:
                                await _run_all_engines_parallel(eis_path, resolved_lumg_id, chunk_size)
                            if progress is not None:
                                for lid in targeted_ids:
                                    progress[lid] = "done"
                        except Exception as e:
                            logger.error(f"EIS routing error for {path!r}: {e}", exc_info=True)
                            if progress is not None:
                                for lid in targeted_ids:
                                    if progress.get(lid) != "done":
                                        progress[lid] = "error"
                else:
                    # Mode 2: direct — each LUMG in this path group gets all files.
                    await asyncio.gather(*[
                        _process_lumg_direct(lp, unzip_utils.temp_path)
                        for lp in lumg_path_list
                    ])
        except Exception as e:
            logger.error(f"Error processing path {path!r}: {e}", exc_info=True)
            if progress is not None:
                for lp in lumg_path_list:
                    if progress.get(lp.lumg_id) not in ("done", "error"):
                        progress[lp.lumg_id] = "error"

    # ── Group LUMGs by path — unzip each unique path only once ─────────────────
    path_groups: dict[str, list] = defaultdict(list)
    for lp in lumg_paths:
        path_groups[str(resolve_stored_path(lp.path))].append(lp)

    logger.info(
        f"Starting update: {len(lumg_paths)} LUMGs across {len(path_groups)} unique path(s)"
    )

    await asyncio.gather(*[
        _process_path_group(path, lumg_list)
        for path, lumg_list in path_groups.items()
    ])


async def update_direct(path: str, lumg_id: int, session: AsyncSession, progress: dict | None = None):
    """Mode 2 forced: read all files from path directly into lumg_id, ignoring EIS codes."""
    if progress is not None:
        progress[lumg_id] = "running"

    chunk_size = backend_settings.get("CHUNK_SIZE")
    path = str(resolve_stored_path(path))

    if not os.path.exists(path):
        logger.error(f"Direct update path does not exist: {path!r}")
        if progress is not None:
            progress[lumg_id] = "error"
        return

    try:
        async with UnzipUtils(path) as unzip_utils:
            await _run_all_engines_parallel(unzip_utils.temp_path, lumg_id, chunk_size)
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
