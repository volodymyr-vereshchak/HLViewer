"""Scheduled/manual refresh of the DPD archive tables.

Polls the last DPD_ARCHIVE_WINDOW_DAYS of daily and hourly data for every
active enterprise of every branch with DPD credentials, upserts the archive
tables, lowers coverage and prunes retention. Runs from the scheduler process
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

logger = logging.getLogger(__name__)

# A refresh polls every device twice (daily + hourly); a heartbeat older than
# this means the running process died — the lock may be taken over.
STALE_SECONDS = 1800


async def acquire() -> bool:
    """Atomically claim the refresh job. False if one is already running."""
    async with async_session_factory() as session:
        # The singleton row is seeded by the migration, but survive a wiped
        # table (tests TRUNCATE everything; manual cleanups happen).
        await session.execute(sa.text(
            "INSERT INTO dpd_refresh_job (id, status) VALUES (1, 'idle') "
            "ON CONFLICT (id) DO NOTHING"
        ))
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
    async with async_session_factory() as session:
        await session.execute(
            sa.text(
                "UPDATE dpd_refresh_job SET status = :status, error = :error, "
                "progress_done = NULL, progress_total = NULL, "
                "finished_at = now(), updated_at = now() WHERE id = 1"
            ),
            {"status": status, "error": error},
        )
        await session.commit()


async def read_status() -> dict:
    async with async_session_factory() as session:
        row = (await session.execute(sa.text(
            """
            SELECT status, started_at, finished_at, error,
                   progress_done, progress_total,
                   (status = 'running'
                    AND updated_at < now() - make_interval(secs => :stale)) AS is_stale
            FROM dpd_refresh_job WHERE id = 1
            """
        ), {"stale": STALE_SECONDS})).mappings().first()
    if row is None:
        return {"status": "idle", "started_at": None, "finished_at": None,
                "error": None, "progress_done": None, "progress_total": None}
    if row["is_stale"]:
        return {"status": "error", "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "error": "Оновлення перервано (процес зупинився)",
                "progress_done": None, "progress_total": None}
    return {"status": row["status"], "started_at": row["started_at"],
            "finished_at": row["finished_at"], "error": row["error"],
            "progress_done": row["progress_done"],
            "progress_total": row["progress_total"]}


async def last_started_at() -> datetime | None:
    async with async_session_factory() as session:
        return (await session.execute(sa.text(
            "SELECT started_at FROM dpd_refresh_job WHERE id = 1"
        ))).scalar()


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
    ent_by_quad = {
        (d["serNum"], d["mfDev"], d["typeDev"], d["chNum"]): d["id"]
        for d in devices if d.get("id") is not None
    }
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
            devices, span[0], span[1], type_request=period_type,
            progress_cb=progress.segment_cb(len(devices)),
        )
        rows = {}
        for record in records:
            if record.get("dvstAlwrk") is None and record.get("dvwrkAlwrk") is None:
                continue  # skeleton — no data yet
            ent_id = ent_by_quad.get((record["serNum"], record["mfDev"],
                                      record["typeDev"], record["chNum"]))
            if ent_id is None:
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
            press_unit = record.get("pressUnit")
            rows[(ent_id, stamp)] = {
                "enterprise_id": ent_id,
                "stamp": stamp,
                "dvst_alwrk": record.get("dvstAlwrk"),
                "dvwrk_alwrk": record.get("dvwrkAlwrk"),
                "press": record.get("press"),
                "temper": record.get("temper"),
                "press_unit": press_unit.strip() if isinstance(press_unit, str)
                else press_unit,
            }
        async with async_session_factory() as session:
            async with session.begin():
                dao = DpdArchiveDao(session)
                await dao.upsert_records(period_type, list(rows.values()))
                await dao.lower_loaded_from(
                    list(ent_by_quad.values()), period_type, window_from
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
        async with async_session_factory() as session:
            for branch_id in branch_ids:
                try:
                    branch_devices[branch_id] = await get_devices_for_branch_db(
                        branch_id, session
                    )
                except Exception:
                    logger.exception(
                        f"DPD refresh: failed to load devices of branch {branch_id}"
                    )
                    branch_devices[branch_id] = []
        total = 2 * sum(len(d) for d in branch_devices.values())
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
            expected_offset += 2 * len(devices)
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
