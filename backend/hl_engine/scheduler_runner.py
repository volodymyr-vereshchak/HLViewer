"""File-arrival poller.

The data source (AS4 network share) used to be polled on a fixed hourly cron
(:30). But new zip snapshots started arriving a few minutes later (:32-:35), so
a fixed-time run kept picking up the previous hour's file. Instead of a clock,
we now react to the *file*: every couple of minutes we check whether the newest
zip on each configured path changed, and if so we run the update.

Detection is mtime/size polling (not an OS watcher) because the source is an
SMB/UNC share, where inotify/watchdog events don't propagate reliably.

What has been read is kept in `hostlib_archive_log`, and an archive that is in
it is not read again. Which archive to read used to be decided from the file
NAME — it was taken apart into a source and a snapshot time, and only the
newest snapshot of each source was read — so a source that renamed its files
would have gone unread without a word, and the whole history of the share was
re-read daily because a day-old record was forgotten. The name now decides
nothing: read once, written down, done.

The path is the folder the archives arrive in — the administrator points it at
the inbox — and everything in it is read. What was there yesterday the source
itself moves into its archive folders, which is why a day's worth of files is
all the poller ever looks at.

An archive is taken in when it is not in the log, has not changed for a
minute, and opens as a whole zip. A failed update is logged as such and comes
round again, but not more often than every fifteen minutes.

One that does not open as a zip at all is written down as `broken`: it is
corrupt or was never copied to the end. The extraction passes over it (see
UnzipUtils.unzip_files), so nothing waits on it. Re-copied, it arrives with
another size or time — a different key — and is tried again at once.

Rows are kept for a week after their file has left the folder; a file still
lying there is remembered for as long as it lies there, so nothing is read
twice because a log entry aged out. A folder that lists as empty is not taken
at its word — an unmounted share says exactly what an emptied folder says.

An update asked for BY HAND does not consult the log at all — it reads
everything on the path. It is asked for because something has to be read
again: an EIC code added after the files arrived, an archive cleared, a
reading rule corrected.
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
from utils.files_utils import all_zips, zip_is_complete

setup_logging()
logger = logging.getLogger(__name__)

# How often we scan the configured paths for a new file.
POLL_INTERVAL_SEC = 120
# A file is only considered "ready" once its newest zip stopped changing for this
# long — guards against reading a half-uploaded archive.
SETTLE_SECONDS = 60

# How long the log remembers an archive that has left the folder. One still
# lying there is remembered for as long as it lies there, however old —
# forgetting it would only mean reading it again for nothing. The days after
# it is gone are not for the file, they are for the folder: a share that is
# briefly unreachable lists as empty, and nothing that a single empty listing
# says should be acted upon (see _forget_what_is_gone).
FORGET_AFTER = timedelta(days=7)
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
    """Every archive under `path`, newest first, with what the log keys it by.
    Blocking filesystem work — run in a thread."""
    if not os.path.isdir(path):
        return []
    found = []
    for archive in all_zips(path):
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
    """path -> archives ready to be read and not read yet, newest first."""
    paths = await _active_paths()
    now = datetime.now()
    on_disk = {path: await asyncio.to_thread(_candidates, path) for path in paths}

    async with async_session_factory() as session:
        rows = (await session.execute(select(HostlibArchiveLog))).scalars().all()
    # Newest row wins: a failed update leaves a row behind, and the run that
    # succeeds afterwards must not be shadowed by it.
    known: dict[tuple, HostlibArchiveLog] = {}
    for row in sorted(rows, key=lambda r: r.processed_at):
        known[_key(row.path, row.filename, row.size, row.file_mtime)] = row

    pending: dict[str, list[dict]] = {}
    for path in paths:
        for archive in on_disk[path]:
            seen = known.get(_key(path, archive["filename"], archive["size"],
                                  archive["mtime"]))
            if seen is not None:
                # Read, or broken, or a failed update still within RETRY_AFTER.
                if seen.status != "error" or seen.processed_at >= now - RETRY_AFTER:
                    continue
            age = (now - archive["mtime"]).total_seconds()
            if age < SETTLE_SECONDS:
                logger.info("Файл %r на %r ще пишеться (%.0f с) — чекаю",
                            archive["filename"], path, age)
                continue
            if not await asyncio.to_thread(zip_is_complete, archive["file"]):
                logger.warning("Архів %r на %r не відкривається як zip — "
                               "пропускаю його, доки не перезаллють",
                               archive["filename"], path)
                await _mark_broken(path, archive)
                continue
            pending.setdefault(path, []).append(archive)

    await _forget_what_is_gone(on_disk, known, now)
    return pending


async def _forget_what_is_gone(on_disk: dict[str, list[dict]],
                               known: dict, now: datetime) -> None:
    """Drop rows for archives that left the folder a week ago or more.

    Only those: a file still lying on the share stays in the log however old
    it is, because forgetting it is exactly what would have it read again.

    A folder that came back empty is left alone entirely. An unmounted share,
    a path renamed, a network that blinked — `os.walk` says the same thing for
    all of them as for a folder somebody emptied, and on that reading the
    whole log for that path would be dropped and every archive read a second
    time when it came back.
    """
    speaking = {path for path, archives in on_disk.items() if archives}
    here = {(path, a["filename"]) for path, archives in on_disk.items()
            for a in archives}
    gone = [row.id for row in known.values()
            if row.path in speaking
            and (row.path, row.filename) not in here
            and row.processed_at < now - FORGET_AFTER]
    if not gone:
        return
    async with async_session_factory() as session:
        await session.execute(
            delete(HostlibArchiveLog).where(HostlibArchiveLog.id.in_(gone))
        )
        await session.commit()


def _key(path: str, filename: str, size: int, mtime: datetime) -> tuple:
    """What tells one archive from the next copy of it: where it lies, what it
    is called, how big it is, and when it was written.

    Whole seconds: the file time makes the round trip through the database and
    through st_mtime's float, and a microsecond lost on the way would make
    every tick believe it is looking at a new file.
    """
    return path, filename, size, mtime.replace(microsecond=0)


async def _mark_broken(path: str, archive: dict) -> None:
    """Remember an archive that does not open, so it is passed over quietly.

    It is written down as handled rather than left pending: the extraction
    skips it (see UnzipUtils.unzip_files), so nothing is waiting on it, and
    without a record of it the poller would open it again every two minutes
    for as long as it sits on the share.
    """
    async with async_session_factory() as session:
        session.add(HostlibArchiveLog(
            path=path,
            filename=archive["filename"],
            size=archive["size"],
            file_mtime=archive["mtime"],
            status="broken",
            error="файл не відкривається як zip — пошкоджений або недокопійований",
            processed_at=datetime.now(),
        ))
        await session.commit()


async def _log(pending: dict[str, list[dict]], failed: set[str]) -> None:
    """Write down what this run did with each archive it took.

    One row per archive, replaced rather than added to: a path that fails
    twice would otherwise leave two rows saying the same thing, and the log is
    kept for as long as the file lies in the folder.
    """
    now = datetime.now()
    async with async_session_factory() as session:
        for path, archives in pending.items():
            status = "error" if path in failed else "ok"
            for archive in archives:
                await session.execute(
                    delete(HostlibArchiveLog).where(
                        HostlibArchiveLog.path == path,
                        HostlibArchiveLog.filename == archive["filename"],
                    )
                )
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


def _listed(archives: list[dict], show: int = 8) -> str:
    """A few names for the log. The whole list ran to 530 files and 24 000
    characters, once every fifteen minutes."""
    names = [a["filename"] for a in archives[:show]]
    rest = len(archives) - len(names)
    return ", ".join(names) + (f" та ще {rest}" if rest > 0 else "")


async def poll_once() -> None:
    pending = await _pending()
    if not pending:
        return

    logger.info(
        "Нові архіви — запускаю оновлення: %s",
        "; ".join(f"{path}: {_listed(archives)}"
                  for path, archives in pending.items()),
    )

    failed: set[str] = set()

    archives_by_path = {path: [a["file"] for a in archives]
                        for path, archives in pending.items()}

    async def work(session, progress):
        failed.update(await update_hostlibs(
            session=session, progress=progress,
            archives_by_path=archives_by_path))

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
