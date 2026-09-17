"""File-arrival poller.

The data source (AS4 network share) used to be polled on a fixed hourly cron
(:30). But new zip snapshots started arriving a few minutes later (:32-:35), so
a fixed-time run kept picking up the previous hour's file. Instead of a clock,
we now react to the *file*: every couple of minutes we check whether the newest
zip on each configured path changed, and if so we run the update.

Detection is mtime/size polling (not an OS watcher) because the source is an
SMB/UNC share, where inotify/watchdog events don't propagate reliably.

What has been taken in is kept in `hostlib_archive_log`, for a day. It lived
in this process's memory, which a restart erased and nothing else could see.
A day is enough: every source folder gains a new snapshot each hour, and
reading an archive twice inserts nothing — a closed record never changes in a
later snapshot, and rows are only added where their key is absent.

An archive is taken in when it is the newest of its source, is not in the log
as done, has not changed for a minute, and opens as a whole zip. A failed one
is logged as such and tried again, but not more often than every fifteen
minutes — an archive that cannot be read will not read on the next tick either.
"""
import asyncio
import logging
import time
from datetime import datetime, timedelta

import os

from sqlmodel import delete, select

from backend.db.engine import async_session_factory
from backend.db.models.hostlib_archive_log_model import HostlibArchiveLog
from backend.db.models.lumg_model import LumgDataPath
from backend.hl_engine.main import update_hostlibs
from backend.hl_engine.update_job_lock import run_guarded_update
from backend.logging_config import setup_logging
from backend.services import dpd_archive_refresh, dpd_line_refresh
from backend.utils.path_utils import resolve_stored_path
from utils.files_utils import UnzipUtils, zip_is_complete

setup_logging()
logger = logging.getLogger(__name__)

# How often we scan the configured paths for a new file.
POLL_INTERVAL_SEC = 120
# A file is only considered "ready" once its newest zip stopped changing for this
# long — guards against reading a half-uploaded archive.
SETTLE_SECONDS = 60

# How long the log remembers an archive. Snapshots arrive hourly, so a day
# covers every one that can still be the newest.
LOG_KEEP = timedelta(days=1)
# How long a failed archive waits before it is tried again.
RETRY_AFTER = timedelta(minutes=15)

# Strong refs to detached DPD-line update tasks (bare create_task results may
# be garbage-collected before completion).
_dpd_line_tasks: set = set()


async def _active_paths() -> list[str]:
    """Unique resolved paths of all active LumgDataPath rows."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(LumgDataPath).where(LumgDataPath.active == True)  # noqa: E712
        )
        rows = result.scalars().all()
    # Multiple LUMGs can share one path; dedupe to scan each path once.
    return sorted({str(resolve_stored_path(r.path)) for r in rows})


def _candidates(path: str) -> list[dict]:
    """The newest archive of each source under `path`, with what the log keys
    it by. Blocking filesystem work — run in a thread."""
    if not os.path.isdir(path):
        return []
    found = []
    for archive in UnzipUtils._latest_zip_per_dir(path):
        try:
            stat = os.stat(archive)
        except OSError:
            continue                    # vanished between listing and stat
        found.append({
            "file": archive,
            "filename": os.path.relpath(archive, path),
            "size": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime),
        })
    return found


async def _pending() -> dict[str, list[dict]]:
    """path -> archives ready to be taken in and not taken in yet."""
    paths = await _active_paths()
    now = datetime.now()
    async with async_session_factory() as session:
        # A day old is forgotten: newer snapshots have superseded it.
        await session.execute(
            delete(HostlibArchiveLog).where(HostlibArchiveLog.processed_at < now - LOG_KEEP)
        )
        await session.commit()
        rows = (await session.execute(select(HostlibArchiveLog))).scalars().all()
    done = {(r.path, r.filename, r.size) for r in rows if r.status == "ok"}
    failed_recently = {
        (r.path, r.filename, r.size) for r in rows
        if r.status == "error" and r.processed_at >= now - RETRY_AFTER
    }

    pending: dict[str, list[dict]] = {}
    for path in paths:
        for archive in await asyncio.to_thread(_candidates, path):
            key = (path, archive["filename"], archive["size"])
            if key in done or key in failed_recently:
                continue
            age = (now - archive["mtime"]).total_seconds()
            if age < SETTLE_SECONDS:
                logger.info("New file %r on %r still settling (%.0fs old) — waiting",
                            archive["filename"], path, age)
                continue
            if not await asyncio.to_thread(zip_is_complete, archive["file"]):
                logger.info("New file %r on %r does not open as a zip yet — waiting",
                            archive["filename"], path)
                continue
            pending.setdefault(path, []).append(archive)
    return pending


async def _log(pending: dict[str, list[dict]], failed: set[str]) -> None:
    now = datetime.now()
    async with async_session_factory() as session:
        for path, archives in pending.items():
            status = "error" if path in failed else "ok"
            for archive in archives:
                session.add(HostlibArchiveLog(
                    path=path,
                    filename=archive["filename"],
                    size=archive["size"],
                    file_mtime=archive["mtime"],
                    status=status,
                    error="оновлення цього шляху завершилось помилкою" if status == "error" else None,
                    processed_at=now,
                ))
        await session.commit()


async def poll_once() -> None:
    pending = await _pending()
    if not pending:
        return

    logger.info(
        "New data detected — triggering update: %s",
        "; ".join(f"{path}: {', '.join(a['filename'] for a in archives)}"
                  for path, archives in pending.items()),
    )

    failed: set[str] = set()

    async def work(session, progress):
        failed.update(await update_hostlibs(session=session, progress=progress))

    ran = await run_guarded_update(work)
    if ran:
        # Logged as done only where the update succeeded. A path whose group
        # errored out is logged as failed and comes round again after
        # RETRY_AFTER: marking it done would drop that batch for good, since
        # nothing re-triggers until the next file lands there.
        for path in failed & set(pending):
            logger.warning("Path %r failed — will retry in %s", path, RETRY_AFTER)
        await _log(pending, failed)
        logger.info("Update finished")
        # DPD lines refresh alongside the hostlib update (user decision):
        # detached so it never blocks the poll loop or the hostlib lock;
        # per-line dpd_line_job locks dedupe against manual inits.
        task = asyncio.create_task(dpd_line_refresh.run_update_all())
        _dpd_line_tasks.add(task)
        task.add_done_callback(_dpd_line_tasks.discard)
    else:
        # A manual update is in progress; nothing is logged, so the same
        # archives are pending again on the next tick.
        logger.info("Update already running (manual) — skipping this tick")


def _last_due_refresh(now: datetime, times: list[str]) -> datetime | None:
    """The most recent scheduled DPD-refresh moment at or before `now`
    (local clock). Yesterday's last slot when `now` is before today's first
    one; None when the schedule is empty."""
    slots = []
    for hhmm in times:
        hour, minute = (int(x) for x in hhmm.split(":"))
        slots.append(now.replace(hour=hour, minute=minute, second=0, microsecond=0))
    if not slots:
        return None
    slots.sort()
    passed = [s for s in slots if s <= now]
    return passed[-1] if passed else slots[-1] - timedelta(days=1)


async def maybe_refresh_dpd() -> None:
    """Run the DPD archive refresh when a scheduled slot has passed since the
    last run. A never-run job (fresh install, wiped archive) is due
    immediately — that is the initial month-long load.

    The schedule is re-read every tick: an admin editing it in the panel takes
    effect within one POLL_INTERVAL_SEC, without restarting this process."""
    now = datetime.now()
    due = _last_due_refresh(now, await dpd_archive_refresh.read_refresh_times())
    if due is None:
        return
    started = await dpd_archive_refresh.last_started_at()
    if started is not None and started >= due:
        return
    logger.info(
        "DPD archive refresh due (slot %s, last run %s) — starting", due, started
    )
    ran = await dpd_archive_refresh.run_refresh()
    if not ran:
        logger.info("DPD refresh already running elsewhere — skipping")


async def main():
    logger.info(
        "File-arrival poller started (interval=%ss, settle=%ss). Waiting for new files...",
        POLL_INTERVAL_SEC, SETTLE_SECONDS,
    )
    while True:
        try:
            await poll_once()
        except Exception:
            logger.exception("poll_once failed")
        try:
            await maybe_refresh_dpd()
        except Exception:
            logger.exception("maybe_refresh_dpd failed")
        await asyncio.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    asyncio.run(main())
