"""File-arrival poller.

The data source (AS4 network share) used to be polled on a fixed hourly cron
(:30). But new zip snapshots started arriving a few minutes later (:32-:35), so
a fixed-time run kept picking up the previous hour's file. Instead of a clock,
we now react to the *file*: every couple of minutes we check whether the newest
zip on each configured path changed, and if so we run the update.

Detection is mtime/size polling (not an OS watcher) because the source is an
SMB/UNC share, where inotify/watchdog events don't propagate reliably.

The "last processed" signature is kept in process memory: no DB migration, and
since the archive upsert is idempotent a restart costs at most one redundant run.
"""
import asyncio
import logging
import time
from datetime import datetime, timedelta

from sqlmodel import select

from backend.db.engine import async_session_factory
from backend.db.models.lumg_model import LumgDataPath
from backend.hl_engine.main import update_hostlibs
from backend.hl_engine.update_job_lock import run_guarded_update
from backend.logging_config import setup_logging
from backend.services import dpd_archive_refresh, dpd_line_refresh
from backend.utils.path_utils import resolve_stored_path
from utils.files_utils import newest_zip_signature

setup_logging()
logger = logging.getLogger(__name__)

# How often we scan the configured paths for a new file.
POLL_INTERVAL_SEC = 120
# A file is only considered "ready" once its newest zip stopped changing for this
# long — guards against reading a half-uploaded archive.
SETTLE_SECONDS = 60

# resolved_path -> signature of the last batch we processed. In-memory by design.
_last_sig: dict[str, frozenset] = {}

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


async def _scan() -> dict[str, tuple[frozenset, float]]:
    """Compute (signature, max_mtime) per unique active path. The filesystem
    walk/stat is blocking, so it runs in a thread."""
    paths = await _active_paths()
    sigs: dict[str, tuple[frozenset, float]] = {}
    for path in paths:
        sigs[path] = await asyncio.to_thread(newest_zip_signature, path)
    return sigs


async def poll_once() -> None:
    sigs = await _scan()
    now = time.time()

    changed = False
    for path, (sig, max_mtime) in sigs.items():
        if sig == _last_sig.get(path):
            continue  # nothing new on this path
        if max_mtime and (now - max_mtime) < SETTLE_SECONDS:
            # Newest file is still fresh — may be mid-upload. Leave _last_sig
            # untouched so we re-check (and catch it) on the next tick.
            logger.info(
                "New file on %r still settling (%.0fs old) — waiting",
                path, now - max_mtime,
            )
            continue
        changed = True

    if not changed:
        return

    logger.info("New data detected — triggering update")

    async def work(session, progress):
        await update_hostlibs(session=session, progress=progress)

    ran = await run_guarded_update(work)
    if ran:
        # Commit the signatures we actually processed (settled ones only); leave
        # any still-settling path unrecorded so it triggers again once ready.
        for path, (sig, max_mtime) in sigs.items():
            if max_mtime == 0 or (now - max_mtime) >= SETTLE_SECONDS:
                _last_sig[path] = sig
        logger.info("Update finished")
        # DPD lines refresh alongside the hostlib update (user decision):
        # detached so it never blocks the poll loop or the hostlib lock;
        # per-line dpd_line_job locks dedupe against manual inits.
        task = asyncio.create_task(dpd_line_refresh.run_update_all())
        _dpd_line_tasks.add(task)
        task.add_done_callback(_dpd_line_tasks.discard)
    else:
        # A manual update is in progress; retry on the next tick (signatures
        # deliberately not committed).
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
