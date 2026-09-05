"""Scheduled/manual refresh of the DPD archive tables.

Polls the last DPD_ARCHIVE_WINDOW_DAYS of daily and hourly data for every
corrector that stood at an active enterprise inside that window, in every
branch with DPD credentials; upserts the archive tables, lowers coverage and
prunes retention. Each device is asked once for the whole window regardless of
how many points it served in it — the archive is the corrector's, and which
point sees which stretch is settled when the data is read. Runs from the
scheduler process
(twice a day, see scheduler_runner) and from the admin endpoint
POST /enterprise/archive/refresh — both guarded by the single-row
dpd_refresh_job lock (same pattern as update_job_lock), so only one refresh
runs at a time across all uvicorn workers and the scheduler."""
import asyncio
import logging
import time
from datetime import date, datetime, timedelta

import sqlalchemy as sa

from backend.db.engine import async_session_factory
from backend.db.dao.dpd_archive_dao import DpdArchiveDao
from backend.services.dpd_client import DPDClient
from backend.services.enterprise_mappings import get_devices_for_branch_db
from backend.settings import backend_settings
from backend.utils.dpd_units import normalize_press_unit

logger = logging.getLogger(__name__)

# A refresh polls every device twice (daily + hourly); a heartbeat older than
# this means the running process died — the lock may be taken over.
STALE_SECONDS = 1800


_ENSURE_ROW = (
    "INSERT INTO dpd_refresh_job (id, status) VALUES (1, 'idle') "
    "ON CONFLICT (id) DO NOTHING"
)


def parse_refresh_times(raw) -> list[str]:
    """Normalise a list (or comma-separated string) of HH:MM into a sorted,
    deduplicated list. Anything unparseable is dropped, not guessed at."""
    items = raw.split(",") if isinstance(raw, str) else list(raw or [])
    times: set[str] = set()
    for item in items:
        parts = str(item).strip().split(":")
        if len(parts) != 2:
            continue
        try:
            hour, minute = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            times.add(f"{hour:02d}:{minute:02d}")
    return sorted(times)


def default_refresh_times() -> list[str]:
    """DPD_REFRESH_TIMES from the environment — what applies while the admin
    panel has nothing stored."""
    return parse_refresh_times(backend_settings["DPD_REFRESH_TIMES"])


async def read_refresh_times() -> list[str]:
    """The schedule the DPD refresh runs on: what the admin panel stored, or
    the environment default while nothing is stored."""
    async with async_session_factory() as session:
        stored = (await session.execute(sa.text(
            "SELECT refresh_times FROM dpd_refresh_job WHERE id = 1"
        ))).scalar()
    return parse_refresh_times(stored) or default_refresh_times()


async def write_refresh_times(times: list[str]) -> list[str]:
    """Store the schedule and return what now applies.

    An empty selection is not "never refresh" — it clears the column, which
    puts DPD_REFRESH_TIMES from the environment back in charge (the default
    10:00 and 16:00). Switching automatic updates off is not something an
    empty input should mean."""
    clean = parse_refresh_times(times)
    async with async_session_factory() as session:
        await session.execute(sa.text(_ENSURE_ROW))
        await session.execute(
            sa.text("UPDATE dpd_refresh_job SET refresh_times = :t WHERE id = 1"),
            {"t": ",".join(clean) if clean else None},
        )
        await session.commit()
    return clean or default_refresh_times()


async def acquire() -> bool:
    """Atomically claim the refresh job. False if one is already running."""
    async with async_session_factory() as session:
        # The singleton row is seeded by the migration, but survive a wiped
        # table (tests TRUNCATE everything; manual cleanups happen).
        await session.execute(sa.text(_ENSURE_ROW))
        result = await session.execute(
            sa.text(
                """
                UPDATE dpd_refresh_job
                SET status = 'running', started_at = now(), updated_at = now(),
                    finished_at = NULL, error = NULL,
                    progress_done = NULL, progress_total = NULL
                WHERE id = 1
                  AND (status <> 'running'
                       OR updated_at < now() - make_interval(secs => :stale))
                RETURNING id
                """
            ),
            {"stale": STALE_SECONDS},
        )
        acquired = result.scalar() is not None
        await session.commit()
        return acquired


class _ProgressWriter:
    """Throttled progress_done writes for the admin progress bar.

    get_volumes calls its progress_cb synchronously on the polling path, so
    the callback only stores the latest value and fires a detached write task
    at most every INTERVAL seconds — it can never slow a device request down.
    Writes are guarded by status='running' (they double as heartbeats), so a
    late task after finalize is a no-op."""

    INTERVAL = 2.0

    def __init__(self, total: int):
        self.total = total
        self.done = 0
        self._offset = 0
        self._last_write = 0.0
        self._task: asyncio.Task | None = None

    def segment_cb(self, device_count: int):
        """Callback for one get_volumes call; advances the base offset."""
        offset = self._offset
        self._offset = offset + device_count

        def cb(done: int, _total: int) -> None:
            self.done = offset + done
            self._maybe_write()

        return cb

    def skip_to(self, expected_offset: int) -> None:
        """Jump over devices that will not be polled (e.g. branch failed)."""
        self._offset = max(self._offset, expected_offset)
        self.done = self._offset
        self._maybe_write()

    def _maybe_write(self) -> None:
        now = time.monotonic()
        if self._task is not None and not self._task.done():
            return
        if now - self._last_write < self.INTERVAL:
            return
        self._last_write = now
        self._task = asyncio.get_running_loop().create_task(self._write())

    async def _write(self) -> None:
        try:
            async with async_session_factory() as session:
                await session.execute(
                    sa.text(
                        "UPDATE dpd_refresh_job SET progress_done = :done, "
                        "updated_at = now() WHERE id = 1 AND status = 'running'"
                    ),
                    {"done": self.done},
                )
                await session.commit()
        except Exception:
            logger.debug("DPD refresh: progress write failed", exc_info=True)


async def _write_progress_total(total: int) -> None:
    async with async_session_factory() as session:
        await session.execute(
            sa.text(
                "UPDATE dpd_refresh_job SET progress_total = :total, "
                "progress_done = 0, updated_at = now() "
                "WHERE id = 1 AND status = 'running'"
            ),
            {"total": total},
        )
        await session.commit()


async def _heartbeat() -> None:
    async with async_session_factory() as session:
        await session.execute(sa.text(
            "UPDATE dpd_refresh_job SET updated_at = now() "
            "WHERE id = 1 AND status = 'running'"
        ))
        await session.commit()


async def _finalize(status: str, error: str | None) -> None:
    # Only closes a RUNNING job: acquire() can take over a stale lock, so a
    # process that hung and then woke up must not write its result over the
    # run that replaced it.
    async with async_session_factory() as session:
        await session.execute(
            sa.text(
                "UPDATE dpd_refresh_job SET status = :status, error = :error, "
                "progress_done = NULL, progress_total = NULL, "
                "finished_at = now(), updated_at = now() "
                "WHERE id = 1 AND status = 'running'"
            ),
            {"status": status, "error": error},
        )
        await session.commit()


async def read_status() -> dict:
    async with async_session_factory() as session:
        row = (await session.execute(sa.text(
            """
            SELECT status, started_at, finished_at, error,
                   progress_done, progress_total, refresh_times,
                   (status = 'running'
                    AND updated_at < now() - make_interval(secs => :stale)) AS is_stale
            FROM dpd_refresh_job WHERE id = 1
            """
        ), {"stale": STALE_SECONDS})).mappings().first()
    # The schedule rides along on the same row the admin card already polls;
    # the default goes with it so the panel can say what "nothing chosen" means
    # without hardcoding hours the environment may have changed.
    env_times = default_refresh_times()
    base = {"refresh_times": env_times, "default_refresh_times": env_times}
    if row is None:
        return {**base, "status": "idle", "started_at": None,
                "finished_at": None, "error": None,
                "progress_done": None, "progress_total": None}
    base["refresh_times"] = parse_refresh_times(row["refresh_times"]) or env_times
    if row["is_stale"]:
        return {**base, "status": "error", "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "error": "Оновлення перервано (процес зупинився)",
                "progress_done": None, "progress_total": None}
    return {**base, "status": row["status"], "started_at": row["started_at"],
            "finished_at": row["finished_at"], "error": row["error"],
            "progress_done": row["progress_done"],
            "progress_total": row["progress_total"]}


async def last_started_at() -> datetime | None:
    async with async_session_factory() as session:
        return (await session.execute(sa.text(
            "SELECT started_at FROM dpd_refresh_job WHERE id = 1"
        ))).scalar()


def distinct_devices(assignments: list) -> dict:
    """device_id → one assignment carrying it.

    A corrector that served two points inside the window appears twice in the
    assignment list, but it is one device with one archive and is polled once.
    Every assignment of it carries the same identity quadruple, so whichever
    comes first is a fine stand-in for the request."""
    by_device: dict = {}
    for a in assignments:
        by_device.setdefault(a["device_id"], a)
    return by_device


async def _branch_ids_with_credentials() -> list[int]:
    async with async_session_factory() as session:
        rows = await session.execute(sa.text(
            "SELECT branch_id FROM grmu_branch_dpd_credential"
        ))
        return [r[0] for r in rows]


async def _refresh_branch(
    branch_id: int,
    devices: list,
    window_from: date,
    today: date,
    progress: _ProgressWriter,
) -> None:
    if not devices:
        logger.info(f"DPD refresh: branch {branch_id} has no active devices")
        return
    async with async_session_factory() as session:
        client = await DPDClient.for_branch(branch_id, session)

    contract_hour = backend_settings.get("CONTRACT_HOUR", 7)
    # `devices` are ASSIGNMENTS (one per history entry); what gets polled is
    # the distinct correctors among them. Every device that stood somewhere
    # inside the refresh window is asked for the WHOLE window, not for its own
    # slice of it: the archive is the corrector's, so all of the answer is its
    # own data, and which point sees which stretch is decided on the read.
    by_device = distinct_devices(devices)
    device_ids = sorted(by_device)
    # No per-device `range`: every one is asked for the same whole span, so the
    # client's own from/to applies.
    poll_devices = [{**a, "tag": device_id} for device_id, a in by_device.items()]
    for period_type in ("daily", "hourly"):
        if period_type == "hourly":
            span = (
                datetime.combine(window_from, datetime.min.time())
                + timedelta(hours=contract_hour),
                datetime.combine(today, datetime.min.time())
                + timedelta(days=1, hours=contract_hour - 1),
            )
        else:
            span = (
                datetime.combine(window_from, datetime.min.time()),
                datetime.combine(today, datetime.min.time()),
            )
        # get_volumes builds and closes its own HTTP pool per call.
        records = await client.get_volumes(
            poll_devices, span[0], span[1], type_request=period_type,
            progress_cb=progress.segment_cb(len(poll_devices)),
        )
        rows = {}
        for record in records:
            if record.get("dvstAlwrk") is None and record.get("dvwrkAlwrk") is None:
                continue  # skeleton — no data yet
            device_id = record.get("tag")
            if device_id not in by_device:
                continue
            raw = record.get("date") or record.get("period")
            try:
                if period_type == "hourly":
                    clean = str(raw).split(".")[0]
                    try:
                        stamp = datetime.strptime(clean, "%Y-%m-%dT%H:%M:%S")
                    except ValueError:
                        stamp = datetime.strptime(clean, "%Y-%m-%dT%H:%M")
                else:
                    stamp = datetime.strptime(str(raw).split("T")[0], "%Y-%m-%d")
            except Exception:
                continue
            rows[(device_id, stamp)] = {
                "device_id": device_id,
                "stamp": stamp,
                "dvst_alwrk": record.get("dvstAlwrk"),
                "dvwrk_alwrk": record.get("dvwrkAlwrk"),
                "press": record.get("press"),
                "temper": record.get("temper"),
                "press_unit": normalize_press_unit(record.get("pressUnit")),
            }
        async with async_session_factory() as session:
            async with session.begin():
                dao = DpdArchiveDao(session)
                await dao.upsert_records(period_type, list(rows.values()))
                await dao.lower_loaded_from(
                    device_ids, period_type, window_from
                )
        logger.info(
            f"DPD refresh: branch {branch_id} {period_type} — "
            f"{len(records)} records fetched, {len(rows)} stored"
        )
        await _heartbeat()


async def execute_locked() -> None:
    """Run the refresh. The dpd_refresh_job lock MUST already be acquired."""
    status, error = "done", None
    try:
        today = date.today()
        window_from = today - timedelta(
            days=backend_settings["DPD_ARCHIVE_WINDOW_DAYS"]
        )
        branch_ids = await _branch_ids_with_credentials()
        # Load device lists up-front so the total device count (×2: daily +
        # hourly) is known before polling — that is the progress bar's 100%.
        branch_devices: dict[int, list] = {}
        # Widest span the two period types can ask for, so assignments that
        # ended before the refresh window are dropped before they are counted.
        contract_hour = backend_settings.get("CONTRACT_HOUR", 7)
        span_from = datetime.combine(window_from, datetime.min.time())
        span_to = datetime.combine(today, datetime.min.time()) + timedelta(
            days=1, hours=contract_hour - 1
        )
        async with async_session_factory() as session:
            for branch_id in branch_ids:
                try:
                    branch_devices[branch_id] = await get_devices_for_branch_db(
                        branch_id, session, range_from=span_from, range_to=span_to,
                    )
                except Exception:
                    logger.exception(
                        f"DPD refresh: failed to load devices of branch {branch_id}"
                    )
                    branch_devices[branch_id] = []
        # The bar counts POLLS, and a device is polled once per period type no
        # matter how many points it served inside the window.
        branch_polls = {
            b: len(distinct_devices(d)) for b, d in branch_devices.items()
        }
        total = 2 * sum(branch_polls.values())
        progress = _ProgressWriter(total)
        await _write_progress_total(total)
        logger.info(
            f"DPD refresh: starting for {len(branch_ids)} branches "
            f"({total // 2} devices), window {window_from}..{today}"
        )
        failures = []
        expected_offset = 0
        for branch_id in branch_ids:
            devices = branch_devices[branch_id]
            expected_offset += 2 * branch_polls[branch_id]
            try:
                await _refresh_branch(
                    branch_id, devices, window_from, today, progress
                )
            except Exception as e:
                # One broken branch must not kill the whole run.
                logger.exception(f"DPD refresh failed for branch {branch_id}")
                failures.append(f"branch {branch_id}: {e}")
                progress.skip_to(expected_offset)
        async with async_session_factory() as session:
            async with session.begin():
                dao = DpdArchiveDao(session)
                pruned_d = await dao.prune("daily")
                pruned_h = await dao.prune("hourly")
        logger.info(
            f"DPD refresh: finished (pruned daily={pruned_d}, hourly={pruned_h})"
        )
        if failures:
            status, error = "error", "; ".join(failures)[:2000]
    except Exception as e:
        logger.exception("DPD refresh failed")
        status, error = "error", str(e) or e.__class__.__name__
    finally:
        try:
            await _finalize(status, error)
        except Exception:
            logger.exception("Failed to persist DPD refresh final state")


async def run_refresh() -> bool:
    """Claim the lock and run the refresh. False if one is already running."""
    if not await acquire():
        return False
    await execute_locked()
    return True
