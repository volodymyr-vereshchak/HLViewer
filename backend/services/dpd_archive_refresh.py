"""Scheduled/manual refresh of the DPD archive tables.

Tops up daily and hourly data for every corrector that EVER stood at an
active enterprise, in every branch with DPD credentials. Each device is
carried on from the newest period its own archive already holds, up to the day
its service ended (today while it is still installed); one with nothing stored
is read from FIRST_EVER. Each is asked once regardless of how many points it
served — the archive is the corrector's, and which point sees which stretch is
settled when the data is read.

Reaching back over the whole history is what makes a point entered today
arrive complete: it may have had three correctors over three years, and only
the one standing now would be found by looking at the present. It costs
nothing on a settled installation, because a corrector that has been taken off
is offered a range ending where its service ended and drops out of the plan as
soon as its archive reaches that day.

There used to be a fixed window here (DPD_ARCHIVE_WINDOW_DAYS, 30) and it was
never a catch-up: it kept the recent tail fresh while a read older than a
device's coverage backfilled the missing head on demand, and a prune raised
coverage behind it. Both of those were removed — reads never call the API now,
and nothing is pruned — which left the window as the only automatic mechanism
while still being sized as a tail refresher. Asking each device for what it
actually lacks replaces it: a device polled this morning costs a day, one
nobody has touched costs the months it missed, and the 30-day re-read of
everything (≈346k hourly records a run, five times a day) is gone.

Runs from the scheduler process (see scheduler_runner) and from the admin
endpoint POST /enterprise/archive/refresh — both guarded by the single-row
dpd_refresh_job lock (same pattern as update_job_lock), so only one refresh
runs at a time across all uvicorn workers and the scheduler."""
import asyncio
import logging
import time
from datetime import date, datetime, timedelta

import sqlalchemy as sa

from backend.db.engine import async_session_factory
from backend.db.dao.dpd_archive_dao import DpdArchiveDao
from backend.services.archive_cleanup import FIRST_EVER
from backend.services.dpd_client import DPDClient
from backend.services.enterprise_mappings import get_devices_for_branch_db
from backend.settings import backend_settings
from backend.utils.dpd_units import normalize_press_unit

logger = logging.getLogger(__name__)

# A refresh polls every device at least twice (daily + hourly); a heartbeat
# older than this means the running process died — the lock may be taken over.
STALE_SECONDS = 1800

# One poll asks DPD for one device over one stretch of days, and the answers of
# a whole batch are held in memory before they are handed to COPY. Two caps
# keep that piece small: a device with nothing stored is read from FIRST_EVER,
# which for hourly data is years at 24 records a day, and the first run after a
# wipe has every device in that state at once. Worst case per batch is
# _POLL_BATCH × _CHUNK_DAYS["hourly"] × 24 ≈ 48k records; the steady state is a
# single day per device, so one batch covers 64 of them.
_CHUNK_DAYS = {"daily": 366, "hourly": 31}
_POLL_BATCH = 64



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


#: Job timestamps are written with the APPLICATION's clock, not the database's.
#:
#: Only the app containers carry TZ=Europe/Kyiv; Postgres runs on UTC, so
#: `now()` in SQL lands three hours behind everything else in this schema —
#: the migration that made these columns NOT NULL backfilled them with
#: `now() AT TIME ZONE 'Europe/Kyiv'`, which is the convention here.
#:
#: It was not only a wrong time on the admin card. The scheduler asks whether a
#: slot has passed since the last run by comparing `started_at` from the
#: database with `datetime.now()` in Python: a run at 15:51 local was written
#: down as 12:51, which is before the 15:00 slot, so the slot stayed due and
#: the refresh started again on the next tick — every two minutes for the three
#: hours it took UTC to reach the slot's wall-clock time. 1121 runs in a week
#: instead of 14 (logs of 21–28.09.2026), and with DPD credentials in place
#: each of those is a month-long poll of the whole branch.
#:
#: `updated_at` stays on the database clock: it is only ever compared with
#: `now()` inside the same statement, which is self-consistent.


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
                SET status = 'running', started_at = :started, updated_at = now(),
                    finished_at = NULL, error = NULL,
                    progress_done = NULL, progress_total = NULL
                WHERE id = 1
                  AND (status <> 'running'
                       OR updated_at < now() - make_interval(secs => :stale))
                RETURNING id
                """
            ),
            {"stale": STALE_SECONDS, "started": datetime.now()},
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
                "finished_at = :finished, updated_at = now() "
                "WHERE id = 1 AND status = 'running'"
            ),
            {"status": status, "error": error, "finished": datetime.now()},
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


def _chunks(start: date, end: date, period_type: str):
    """Walk start..end in stretches no longer than _CHUNK_DAYS, inclusive."""
    step = timedelta(days=_CHUNK_DAYS[period_type])
    cursor = start
    while cursor <= end:
        last = min(cursor + step - timedelta(days=1), end)
        yield cursor, last
        cursor = last + timedelta(days=1)


def _span(period_type: str, first: date, last: date,
          contract_hour: int) -> tuple[datetime, datetime]:
    """The datetimes one poll covers for the days first..last.

    Hourly reaches contract_hour into the day after `last`, because the gas
    day that is running now is filed under tomorrow's date; daily stops at
    `last` itself. Chunk boundaries therefore overlap by a few hours for
    hourly, which costs nothing — the upsert is idempotent — and guarantees
    no hour falls between two chunks.
    """
    if period_type == "hourly":
        return (
            datetime.combine(first, datetime.min.time())
            + timedelta(hours=contract_hour),
            datetime.combine(last, datetime.min.time())
            + timedelta(days=1, hours=contract_hour - 1),
        )
    return (
        datetime.combine(first, datetime.min.time()),
        datetime.combine(last, datetime.min.time()),
    )


def _carry_on_from(stamp, since: date | None) -> date:
    """Which day a device is polled from.

    With `since` the operator named the period and it applies to every device
    («Перечитати архів»). Otherwise the device's own archive decides: from the
    newest period it already holds, or from FIRST_EVER when it holds nothing.

    The newest stored period is re-read rather than skipped past: a record of
    the day that is still running keeps changing until the day closes, and
    re-reading one day is cheaper than being wrong about it.
    """
    if since is not None:
        return since
    if stamp is None:
        return FIRST_EVER
    return stamp.date() if isinstance(stamp, datetime) else stamp


def _served_until(assignments: list, window_to: date) -> dict[int, date]:
    """device_id -> the last day it is worth asking DPD about.

    `window_to` for a corrector still standing somewhere (win_to is None), and
    the end of its last assignment for one that has been taken off everywhere.
    A device that moved between two of our points is open again, so the later
    assignment decides.

    This is what makes it safe to consider the WHOLE history instead of only
    assignments that ended recently: a corrector removed two years ago is
    offered a range that ends where its service ended, so once its archive
    reaches that day it has nothing left to ask for and drops out of the plan
    by itself — while a point entered today still gets every corrector it ever
    had, each over its own stretch.
    """
    until: dict[int, date] = {}
    for assignment in assignments:
        device_id = assignment["device_id"]
        win_to = assignment["win_to"]
        if win_to is None:
            until[device_id] = window_to
            continue
        if until.get(device_id) == window_to:
            continue                    # already open through another point
        # win_to is the EXCLUSIVE end; its own date is kept because the gas day
        # that was running when the corrector came off is filed under it.
        ended = min(win_to.date(), window_to)
        if ended > until.get(device_id, date.min):
            until[device_id] = ended
    return until


async def _plan_branch(devices: list, since: date | None,
                       window_to: date, contract_hour: int) -> list[dict]:
    """One entry per poll this branch needs: a device, a period type and the
    stretch of days to ask for.

    `devices` are ASSIGNMENTS (one per history entry); what gets polled is the
    distinct correctors among them. A device that stood at two points is asked
    once over one stretch, not once per point: the archive is the corrector's,
    and which point sees which part is decided on the read.
    """
    by_device = distinct_devices(devices)
    if not by_device:
        return []
    device_ids = sorted(by_device)
    until = _served_until(devices, window_to)
    plan: list[dict] = []
    async with async_session_factory() as session:
        dao = DpdArchiveDao(session)
        for period_type in ("daily", "hourly"):
            # A dated re-read ignores what is stored, so do not ask for it.
            last = ({} if since is not None
                    else await dao.last_stamps(device_ids, period_type))
            for device_id in device_ids:
                start = _carry_on_from(last.get(device_id), since)
                # A chosen period applies whole, to every device: that is what
                # «Перечитати архів» is for. A routine run stops where the
                # device stopped serving.
                final = window_to if since is not None else until.get(
                    device_id, window_to
                )
                if start > final:
                    continue
                if (since is None and final < window_to and start == final):
                    # Taken off, and its archive already reaches the day it
                    # came off. Nothing more will ever arrive for it.
                    continue
                for first, chunk_to in _chunks(start, final, period_type):
                    plan.append({
                        "period_type": period_type,
                        "device": {
                            **by_device[device_id],
                            "tag": device_id,
                            "range": _span(period_type, first, chunk_to,
                                           contract_hour),
                        },
                    })
    return plan


def _rows_from(records: list, period_type: str, known: set) -> dict:
    """Records as archive rows, keyed by (device, period) so a day that two
    overlapping chunks both answered with is stored once."""
    rows: dict = {}
    for record in records:
        if record.get("dvstAlwrk") is None and record.get("dvwrkAlwrk") is None:
            continue  # skeleton — no data yet
        device_id = record.get("tag")
        if device_id not in known:
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
    return rows


async def _refresh_branch(branch_id: int, plan: list[dict],
                          progress: _ProgressWriter) -> None:
    if not plan:
        logger.info(f"DPD refresh: branch {branch_id} has nothing to poll")
        return
    async with async_session_factory() as session:
        client = await DPDClient.for_branch(branch_id, session)

    for period_type in ("daily", "hourly"):
        polls = [p for p in plan if p["period_type"] == period_type]
        fetched = stored = 0
        for start in range(0, len(polls), _POLL_BATCH):
            batch = [p["device"] for p in polls[start:start + _POLL_BATCH]]
            # Each device carries its own "range", which get_volumes honours
            # ahead of the from/to below; those only bound the batch.
            span_from = min(d["range"][0] for d in batch)
            span_to = max(d["range"][1] for d in batch)
            # get_volumes builds and closes its own HTTP pool per call.
            records = await client.get_volumes(
                batch, span_from, span_to, type_request=period_type,
                progress_cb=progress.segment_cb(len(batch)),
            )
            rows = _rows_from(records, period_type, {d["tag"] for d in batch})
            async with async_session_factory() as session:
                async with session.begin():
                    await DpdArchiveDao(session).upsert_records(
                        period_type, list(rows.values())
                    )
            fetched += len(records)
            stored += len(rows)
            await _heartbeat()
        logger.info(
            f"DPD refresh: branch {branch_id} {period_type} — "
            f"{len(polls)} polls, {fetched} records fetched, {stored} stored"
        )


async def execute_locked(since: date | None = None,
                         until: date | None = None) -> None:
    """Run the refresh. The dpd_refresh_job lock MUST already be acquired.

    Without dates this is the routine run: every device is carried on from the
    newest period its own archive holds, up to today. A device with nothing
    stored is read from FIRST_EVER, so a point entered today arrives with its
    history and so does one whose archive was wiped. There is no fixed window
    any more — the run asks for what is missing, which is a day or two for a
    device polled this morning and years for one nobody has touched.

    With dates it is «Перечитати архів»: the same work over a period somebody
    chose, for every device regardless of what is stored. That is what closes
    a stretch DPD never delivered, and the only thing that does — a read of
    the archive never calls the API. Every device is polled from the start of
    the period, not from its own install date: the archive belongs to the
    corrector, so everything it answers with is worth keeping, and a corrector
    that moved is read by each of its points through its own window anyway.
    """
    status, error = "done", None
    try:
        today = date.today()
        window_to = min(until or today, today)
        contract_hour = backend_settings.get("CONTRACT_HOUR", 7)
        # Every assignment of every active point, all the way back — not only
        # the ones in force now. A point entered today may have had three
        # correctors over three years, and each holds a stretch of its history
        # that nothing else can supply. What keeps this from re-polling the
        # whole past on every run is _served_until: a device that has been
        # taken off is offered a range ending where its service ended, so it
        # drops out as soon as its archive reaches that day.
        span_from = datetime.combine(since or FIRST_EVER, datetime.min.time())
        span_to = datetime.combine(window_to, datetime.min.time()) + timedelta(
            days=1, hours=contract_hour - 1
        )
        branch_ids = await _branch_ids_with_credentials()
        # Plan every branch up front: the progress bar's 100% is the number of
        # polls, and that is only known once each device's starting day is.
        branch_plan: dict[int, list] = {}
        device_count = 0
        async with async_session_factory() as session:
            for branch_id in branch_ids:
                try:
                    devices = await get_devices_for_branch_db(
                        branch_id, session,
                        range_from=span_from, range_to=span_to,
                    )
                except Exception:
                    logger.exception(
                        f"DPD refresh: failed to load devices of branch {branch_id}"
                    )
                    branch_plan[branch_id] = []
                    continue
                device_count += len(distinct_devices(devices))
                branch_plan[branch_id] = await _plan_branch(
                    devices, since, window_to, contract_hour
                )
        total = sum(len(p) for p in branch_plan.values())
        progress = _ProgressWriter(total)
        await _write_progress_total(total)
        scope = (f"period {since}..{window_to}" if since is not None
                 else f"carrying on to {window_to}")
        logger.info(
            f"DPD refresh: starting for {len(branch_ids)} branches "
            f"({device_count} devices, {total} polls), {scope}"
        )
        failures = []
        expected_offset = 0
        for branch_id in branch_ids:
            expected_offset += len(branch_plan[branch_id])
            try:
                await _refresh_branch(
                    branch_id, branch_plan[branch_id], progress
                )
            except Exception as e:
                # One broken branch must not kill the whole run.
                logger.exception(f"DPD refresh failed for branch {branch_id}")
                failures.append(f"branch {branch_id}: {e}")
                progress.skip_to(expected_offset)
        logger.info("DPD refresh: finished")
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
