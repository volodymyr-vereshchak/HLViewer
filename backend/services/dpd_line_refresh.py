"""Init and scheduled refresh of DPD line archives.

A DPD line's archive is fed from the DPD API through its device history:
each device's records are stored only within its validity window
[installed_from, next device's installed_from) — so after a corrector
replacement the new device's archive (which may contain pre-replacement
data belonging to another metering point) can never touch the line's
periods that belong to the old device.

Two entry points, both guarded by the per-line dpd_line_job lock (same
pattern as dpd_refresh_job, but one row per line):

- run_init(line_id): the admin "initialization" button — wipes the line's
  daily+hourly archives and re-polls every device's full window.
- run_update_all(): scheduler hook, fired right after a hostlib update —
  for every active line, polls from the last archived period to now for
  the currently active device only.
"""
import asyncio
import logging
from datetime import date, datetime, timedelta

import sqlalchemy as sa

from backend.db.engine import async_session_factory
from backend.db.dao.dpd_line_dao import DpdLineArchiveDao, DpdLineDao
from backend.db.models.dpd_line_model import DpdLine
from backend.services.dpd_client import DPDClient
from backend.services.enterprise_mappings import volume_field_for_device
from backend.settings import backend_settings
from backend.utils.dpd_units import normalize_press_unit

logger = logging.getLogger(__name__)

# An init polls every device twice (daily + hourly); a heartbeat older than
# this means the running process died — the lock may be taken over.
STALE_SECONDS = 1800

# Defensive chunking: windows longer than this are fetched in yearly pieces
# (the API handled multi-year spans in one request in live tests, but a
# bounded request size keeps a single degraded call from stalling the run).
CHUNK_DAYS = 366


def device_windows(
    devices: list[dict],
) -> list[tuple[dict, datetime, datetime | None]]:
    """(device, win_from, win_to) per history entry, ordered by installed_from.
    win_to is the next device's installed_from; None = open-ended (current)."""
    ordered = sorted(devices, key=lambda d: d["installed_from"])
    result = []
    for i, dev in enumerate(ordered):
        win_to = ordered[i + 1]["installed_from"] if i + 1 < len(ordered) else None
        result.append((dev, dev["installed_from"], win_to))
    return result


def _parse_stamp(raw, period_type: str) -> datetime | None:
    try:
        if period_type == "hourly":
            clean = str(raw).split(".")[0]
            try:
                return datetime.strptime(clean, "%Y-%m-%dT%H:%M:%S")
            except ValueError:
                return datetime.strptime(clean, "%Y-%m-%dT%H:%M")
        return datetime.strptime(str(raw).split("T")[0], "%Y-%m-%d")
    except Exception:
        return None


def _attribution_stamp(stamp: datetime, period_type: str) -> datetime:
    """The moment a record is attributed at for window filtering.

    Hourly records use their exact stamp. A daily record for date d covers
    the commercial day d CONTRACT_HOUR:00 → d+1 CONTRACT_HOUR:00 and is
    attributed to the device whose window covers the commercial-day start."""
    if period_type == "hourly":
        return stamp
    contract_hour = backend_settings.get("CONTRACT_HOUR", 7)
    return stamp + timedelta(hours=contract_hour)


def _records_to_rows(
    records: list[dict],
    dpd_line_id: int,
    device: dict,
    win_from: datetime,
    win_to: datetime | None,
    period_type: str,
) -> list[dict]:
    """Convert DPD API records to archive rows, applying the volume-field
    mapping, skipping skeleton (null) records and window-filtering."""
    volume_field = volume_field_for_device(device["mfDev"], device["typeDev"])
    rows: dict[datetime, dict] = {}
    for record in records:
        volume = record.get(volume_field)
        if volume is None:
            continue  # skeleton row — DPD returns the whole range padded
        stamp = _parse_stamp(record.get("date") or record.get("period"),
                             period_type)
        if stamp is None:
            continue
        at = _attribution_stamp(stamp, period_type)
        if at < win_from or (win_to is not None and at >= win_to):
            continue  # belongs to another device's window (or to no one)
        rows[stamp] = {
            "dpd_line_id": dpd_line_id,
            "stamp": stamp,
            "volume": volume,
            "pressure": record.get("press"),
            "temperature": record.get("temper"),
            "press_unit": normalize_press_unit(record.get("pressUnit")),
        }
    return list(rows.values())


def _chunk_spans(
    span_from: datetime, span_to: datetime
) -> list[tuple[datetime, datetime]]:
    spans = []
    cursor = span_from
    while cursor < span_to:
        nxt = min(cursor + timedelta(days=CHUNK_DAYS), span_to)
        spans.append((cursor, nxt))
        cursor = nxt
    return spans or [(span_from, span_to)]


# ─── per-line job lock / status ───────────────────────────────────────────────


async def acquire(dpd_line_id: int, kind: str) -> bool:
    """Atomically claim the line's job. False if one is already running."""
    async with async_session_factory() as session:
        await session.execute(
            sa.text(
                "INSERT INTO dpd_line_job (dpd_line_id, kind, status) "
                "VALUES (:id, :kind, 'idle') "
                "ON CONFLICT (dpd_line_id) DO NOTHING"
            ),
            {"id": dpd_line_id, "kind": kind},
        )
        result = await session.execute(
            sa.text(
                """
                UPDATE dpd_line_job
                SET kind = :kind, status = 'running', started_at = now(),
                    updated_at = now(), finished_at = NULL, error = NULL,
                    progress_done = NULL, progress_total = NULL
                WHERE dpd_line_id = :id
                  AND (status <> 'running'
                       OR updated_at < now() - make_interval(secs => :stale))
                RETURNING dpd_line_id
                """
            ),
            {"id": dpd_line_id, "kind": kind, "stale": STALE_SECONDS},
        )
        acquired = result.scalar() is not None
        await session.commit()
        return acquired


async def read_status(dpd_line_id: int) -> dict:
    async with async_session_factory() as session:
        row = (await session.execute(sa.text(
            """
            SELECT kind, status, started_at, finished_at, error,
                   progress_done, progress_total,
                   (status = 'running'
                    AND updated_at < now() - make_interval(secs => :stale)) AS is_stale
            FROM dpd_line_job WHERE dpd_line_id = :id
            """
        ), {"id": dpd_line_id, "stale": STALE_SECONDS})).mappings().first()
    if row is None:
        return {"kind": None, "status": "idle", "started_at": None,
                "finished_at": None, "error": None,
                "progress_done": None, "progress_total": None}
    if row["is_stale"]:
        return {"kind": row["kind"], "status": "error",
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "error": "Оновлення перервано (процес зупинився)",
                "progress_done": None, "progress_total": None}
    return {"kind": row["kind"], "status": row["status"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"], "error": row["error"],
            "progress_done": row["progress_done"],
            "progress_total": row["progress_total"]}


async def _write_progress(
    dpd_line_id: int, done: int | None, total: int | None
) -> None:
    async with async_session_factory() as session:
        await session.execute(
            sa.text(
                "UPDATE dpd_line_job SET progress_done = :done, "
                "progress_total = :total, updated_at = now() "
                "WHERE dpd_line_id = :id AND status = 'running'"
            ),
            {"id": dpd_line_id, "done": done, "total": total},
        )
        await session.commit()


async def _heartbeat(dpd_line_id: int) -> None:
    async with async_session_factory() as session:
        await session.execute(
            sa.text(
                "UPDATE dpd_line_job SET updated_at = now() "
                "WHERE dpd_line_id = :id AND status = 'running'"
            ),
            {"id": dpd_line_id},
        )
        await session.commit()


async def _finalize(dpd_line_id: int, status: str, error: str | None) -> None:
    async with async_session_factory() as session:
        await session.execute(
            sa.text(
                "UPDATE dpd_line_job SET status = :status, error = :error, "
                "progress_done = NULL, progress_total = NULL, "
                "finished_at = now(), updated_at = now() "
                "WHERE dpd_line_id = :id"
            ),
            {"id": dpd_line_id, "status": status, "error": error},
        )
        await session.commit()


# ─── init (admin button: full wipe + reload) ──────────────────────────────────


async def init_line_locked(dpd_line_id: int) -> None:
    """Full reload of a line's archives. The line's job lock MUST already be
    acquired (kind='init')."""
    status, error = "done", None
    try:
        async with async_session_factory() as session:
            line = await session.get(DpdLine, dpd_line_id)
            if line is None:
                raise ValueError(f"DPD line {dpd_line_id} not found")
            devices = await DpdLineDao(session).get_devices_resolved(dpd_line_id)
            client = await DPDClient.for_branch(line.branch_id, session)
        if not devices:
            raise ValueError("У лінії немає жодного приладу")

        windows = device_windows(devices)
        total = 2 * len(windows)
        done = 0
        await _write_progress(dpd_line_id, done, total)

        # Wipe first: re-init semantics (also clears rows that no longer
        # belong to any window after a history correction).
        async with async_session_factory() as session:
            async with session.begin():
                await DpdLineArchiveDao(session).delete_line(dpd_line_id)

        now = datetime.now()
        for device, win_from, win_to in windows:
            span_to = win_to or now
            for period_type in ("daily", "hourly"):
                # Keyed by stamp: DPD accepts only date bounds, so adjacent
                # yearly chunks overlap at the boundary day and re-fetch the
                # same records — an upsert batch must not contain duplicates.
                by_stamp: dict[datetime, dict] = {}
                if win_from < span_to:
                    for chunk_from, chunk_to in _chunk_spans(win_from, span_to):
                        records = await client.get_volumes(
                            [device], chunk_from, chunk_to,
                            type_request=period_type,
                        )
                        for row in _records_to_rows(
                            records, dpd_line_id, device,
                            win_from, win_to, period_type,
                        ):
                            by_stamp[row["stamp"]] = row
                rows = list(by_stamp.values())
                if rows:
                    async with async_session_factory() as session:
                        async with session.begin():
                            await DpdLineArchiveDao(session).upsert_records(
                                period_type, rows
                            )
                done += 1
                await _write_progress(dpd_line_id, done, total)
                logger.info(
                    f"DPD line {dpd_line_id} init: device {device['serNum']} "
                    f"{period_type} — {len(rows)} rows stored"
                )
    except Exception as e:
        logger.exception(f"DPD line {dpd_line_id} init failed")
        status, error = "error", str(e) or e.__class__.__name__
    finally:
        try:
            await _finalize(dpd_line_id, status, error)
        except Exception:
            logger.exception(
                f"Failed to persist DPD line {dpd_line_id} init state"
            )


async def run_init(dpd_line_id: int) -> bool:
    """Claim the line's lock and run a full init. False if already running."""
    if not await acquire(dpd_line_id, "init"):
        return False
    await init_line_locked(dpd_line_id)
    return True


# ─── scheduled incremental update ─────────────────────────────────────────────


async def _update_line_locked(line: DpdLine, client: DPDClient) -> None:
    """Incremental refresh of one line: from the last archived period (or the
    active device's installed_from on an empty archive) to now, for the
    currently active device only. The line's job lock MUST be acquired."""
    status, error = "done", None
    try:
        async with async_session_factory() as session:
            devices = await DpdLineDao(session).get_devices_resolved(line.id)
        now = datetime.now()
        active = [d for d in devices if d["installed_from"] <= now]
        if not active:
            return  # no device installed yet — nothing to poll
        device = active[-1]
        win_from = device["installed_from"]

        for period_type in ("daily", "hourly"):
            async with async_session_factory() as session:
                last = await DpdLineArchiveDao(session).last_period(
                    line.id, period_type
                )
            if last is None:
                span_from = win_from
            elif period_type == "daily":
                # Re-fetch the last stored day too: recent records mutate as
                # the commercial day completes; the upsert is idempotent.
                span_from = max(
                    datetime.combine(last, datetime.min.time()), win_from
                )
            else:
                span_from = max(last, win_from)
            if span_from > now:
                continue
            records = await client.get_volumes(
                [device], span_from, now, type_request=period_type,
            )
            rows = _records_to_rows(
                records, line.id, device, win_from, None, period_type,
            )
            if rows:
                async with async_session_factory() as session:
                    async with session.begin():
                        await DpdLineArchiveDao(session).upsert_records(
                            period_type, rows
                        )
            logger.info(
                f"DPD line {line.id} update: {period_type} "
                f"from {span_from} — {len(rows)} rows stored"
            )
            await _heartbeat(line.id)
    except Exception as e:
        logger.exception(f"DPD line {line.id} update failed")
        status, error = "error", str(e) or e.__class__.__name__
    finally:
        try:
            await _finalize(line.id, status, error)
        except Exception:
            logger.exception(
                f"Failed to persist DPD line {line.id} update state"
            )


async def run_update_all() -> None:
    """Scheduler hook: incrementally refresh every active DPD line. Never
    raises; one broken line/branch must not kill the rest."""
    try:
        async with async_session_factory() as session:
            lines = await DpdLineDao(session).get_all(active=True)
        if not lines:
            return
        by_branch: dict[int, list[DpdLine]] = {}
        for line in lines:
            by_branch.setdefault(line.branch_id, []).append(line)
        logger.info(
            f"DPD lines update: {len(lines)} lines in {len(by_branch)} branches"
        )
        for branch_id, branch_lines in by_branch.items():
            try:
                async with async_session_factory() as session:
                    client = await DPDClient.for_branch(branch_id, session)
            except Exception:
                logger.exception(
                    f"DPD lines update: no usable credentials for branch "
                    f"{branch_id}, skipping {len(branch_lines)} lines"
                )
                continue
            for line in branch_lines:
                if not await acquire(line.id, "update"):
                    logger.info(
                        f"DPD line {line.id}: job already running, skipped"
                    )
                    continue
                await _update_line_locked(line, client)
    except Exception:
        logger.exception("DPD lines update failed")
