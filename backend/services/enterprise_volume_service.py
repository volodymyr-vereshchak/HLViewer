"""
Shared pipeline for the enterprise volume endpoints.

/enterprise/volumes/ and /enterprise/volumes_virtual/ used to carry two ~150-line
copies of the same flow: parse the date range, resolve devices, call the DPD API
for one branch, then aggregate device records into per-(line, period) responses.
The only real differences are captured by two parameters:

- ``line_remap``: physical line_id → list of reported line_ids (itself and/or
  virtual parents). None keeps the device's own line_id (plain endpoint); with
  a mapping, devices whose line is not in it are skipped (not requested).
- ``none_volume_as_zero``: the virtual endpoint has always reported missing
  volumes as 0.0, the plain one preserves None (the frontend shows a gap).

Data model (v4, user decisions 2026-07-12): the DB archive tables
(dpd_daily_archive / dpd_hourly_archive) are the PRIMARY source. The scheduler
refreshes the last DPD_ARCHIVE_WINDOW_DAYS twice a day (dpd_archive_refresh);
reads inside that window never touch the DPD API. Ranges older than a device's
coverage (dpd_device_coverage.loaded_from) are backfilled from DPD on demand,
per device, then served from the DB like everything else.

v5 (device history): the archive is keyed by the CORRECTOR, not by the
metering point. Reads work in ASSIGNMENTS — "device D stood at point E from …
to …", one per history entry — and each reads only its own window, so a
replaced corrector's data can never land in the previous device's periods.

Polling deliberately ignores those windows: a device is asked for the whole
requested range, once, and everything it answers with is stored under it. The
data is the corrector's own wherever it stood, so keeping all of it costs one
request instead of several and leaves the archive ready for the next point the
device is moved to. Windows are a reading rule, not a fetching one.
"""

import logging
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from time import perf_counter
from typing import Callable, Dict, List, Optional

from sqlalchemy import text

from backend.db.engine import async_session_factory
from backend.db.dao.dpd_archive_dao import DpdArchiveDao
from backend.db.models.enterprise_models import DeviceVolume, EnterpriseVolumeResponse
from backend.services import device_history
from backend.services.dpd_client import DPDClient
from backend.services.enterprise_mappings import volume_field_for_device
from backend.settings import backend_settings
from backend.utils.dpd_units import normalize_press_unit

logger = logging.getLogger(__name__)


def parse_date_range(from_date: str, to_date: str) -> tuple[datetime, datetime]:
    """Parse YYYY-MM-DD query params (tolerating time suffixes). Raises
    ValueError on bad format or from_date > to_date."""
    date_from = datetime.strptime(from_date.split(" ")[0].split("T")[0], "%Y-%m-%d")
    date_to = datetime.strptime(to_date.split(" ")[0].split("T")[0], "%Y-%m-%d")
    if date_from > date_to:
        raise ValueError("from_date must be <= to_date")
    return date_from, date_to


def pick_branch_id(devices: List[Dict]) -> Optional[int]:
    """DPD credentials are per branch; all requested devices belong to one."""
    return next((d["branch_id"] for d in devices if d.get("branch_id")), None)


def request_window(date_from: datetime, date_to: datetime, period_type: str):
    """The stamp window a from/to date range denotes. Both our endpoint and
    DPD take bare dates; for hourly data a date is a commercial day, so
    from=D1&to=D2 means [D1 CONTRACT_HOUR .. D2+1 CONTRACT_HOUR-1h] — exactly
    what DPD returns for that range. Daily: calendar days D1..D2.

    Public because callers must narrow device assignments by the SAME window
    the read uses: filtering by the bare dates would drop a corrector
    installed at 14:00 on the last requested day."""
    if period_type == "hourly":
        contract_hour = backend_settings.get("CONTRACT_HOUR", 7)
        start = datetime.combine(date_from.date(), datetime.min.time())
        end = datetime.combine(date_to.date(), datetime.min.time())
        return (
            start + timedelta(hours=contract_hour),
            end + timedelta(days=1, hours=contract_hour - 1),
        )
    return (
        datetime.combine(date_from.date(), datetime.min.time()),
        datetime.combine(date_to.date(), datetime.min.time()),
    )


def _has_data(record: Dict) -> bool:
    """False for DPD's skeleton records: a stamp with both volume fields null
    carries no data (yet). Skeletons are never stored in the archive."""
    return record.get("dvstAlwrk") is not None or record.get("dvwrkAlwrk") is not None


def _record_stamp(record: Dict, period_type: str):
    """Stamp of a raw DPD record: datetime for hourly, date for daily,
    None when unparseable (record skipped)."""
    raw = record.get("date") or record.get("period")
    if not raw:
        return None
    return _parse_record_period(raw, period_type)


async def fetch_dpd_volumes(
    devices: List[Dict],
    date_from: datetime,
    date_to: datetime,
    period_type: str,
    events_cb: Optional[Callable[[Dict], None]] = None,
    live: bool = False,
    hours: Optional[List[int]] = None,
) -> List[Dict]:
    """Raw volume records for the requested devices/range, served from the DB
    archive; DPD is contacted only to backfill ranges older than a device's
    coverage.

    ``live=True`` (the enterprise poll page): the WHOLE requested range is
    re-polled from the DPD API for every device first (results upserted into
    the archive), so the response carries fresh data; when the API poll
    fails, the error is logged and the archive serves whatever it has —
    DB data is the fallback, not the primary source.

    ``events_cb`` (optional, SYNCHRONOUS, must be non-blocking) receives
    progress events for the streaming endpoint:
    {"type":"status","phase":"waiting"} before backfill locks,
    {"type":"progress","done":N,"total":M} as backfill device polls complete
    (total=0 when nothing needs backfilling),
    {"type":"status","phase":"aggregating"} before the DB read/aggregation.

    ``hours`` (hourly only) restricts the response to those wall-clock hours.
    Backfill is unaffected — a range is polled and stored whole, so the
    archive stays complete for the next reader; only what comes back out is
    narrowed.

    Raises LookupError when a backfill is needed but no device carries a
    branch_id, ValueError when the branch has no/incomplete credentials
    (DPDClient.for_branch), and whatever the HTTP client raises when the DPD
    API is unreachable mid-backfill."""
    window_from, window_to = request_window(date_from, date_to, period_type)
    requested_from = date_from.date()
    # Assignments, keyed by history entry: the same corrector may appear twice
    # (two metering points, two windows), so the identity quadruple cannot key
    # anything here.
    by_assignment: Dict[int, Dict] = {
        d["assignment_id"]: d for d in devices if d.get("assignment_id") is not None
    }
    if len(by_assignment) < len(devices):
        logger.warning(
            f"{len(devices) - len(by_assignment)} device(s) without an "
            f"assignment id skipped in fetch_dpd_volumes"
        )
    if not by_assignment:
        return []
    # Coverage and backfill are per DEVICE — a corrector shared by two points
    # over time is fetched once and read by both.
    device_ids = sorted({d["device_id"] for d in by_assignment.values()})

    async with async_session_factory() as session:
        async with session.begin():
            # Safety nets: never queue on backfill locks forever, and let
            # Postgres kill a leaked idle-in-transaction connection.
            await session.execute(text("SET LOCAL lock_timeout = '300s'"))
            await session.execute(
                text("SET LOCAL idle_in_transaction_session_timeout = '1800s'")
            )
            dao = DpdArchiveDao(session)

            if live:
                # Fresh poll of the full range for every device; the archive
                # both caches the result and covers devices that failed.
                spans = {
                    device_id: (requested_from, date_to.date())
                    for device_id in device_ids
                }
                if events_cb is not None:
                    events_cb({"type": "progress", "done": 0,
                               "total": len(by_assignment)})
                    events_cb({"type": "status", "phase": "waiting"})
                await _lock_backfill(session, spans, period_type)
                try:
                    await _run_backfill(
                        session, dao, by_assignment, spans, period_type,
                        requested_from, date_to, events_cb,
                    )
                except Exception:
                    logger.exception(
                        "Live DPD poll failed — serving archive data as fallback"
                    )
            else:
                backfill = await _plan_backfill(
                    dao, device_ids, period_type, requested_from, date_to
                )
                if events_cb is not None:
                    events_cb({"type": "progress", "done": 0, "total": len(backfill)})
                if backfill:
                    if events_cb is not None:
                        events_cb({"type": "status", "phase": "waiting"})
                    await _lock_backfill(session, backfill, period_type)
                    # A concurrent request may have backfilled while we waited.
                    backfill = await _plan_backfill(
                        dao, list(backfill), period_type, requested_from, date_to,
                    )
                    if backfill:
                        await _run_backfill(
                            session, dao, by_assignment, backfill, period_type,
                            requested_from, date_to, events_cb,
                        )
            if events_cb is not None:
                events_cb({"type": "status", "phase": "aggregating"})

            # The DB is the sole source for the response. Each assignment reads
            # its own device's archive clipped to the window it was in force
            # for, so a stretch no device covered simply yields nothing.
            read_windows = []
            for assignment_id, a in by_assignment.items():
                span = _read_window(a, window_from, window_to, period_type)
                if span is None:
                    continue
                read_windows.append({
                    "tag": assignment_id, "device_id": a["device_id"],
                    "win_from": span[0], "win_to": span[1],
                })
            rows = await dao.load_windows(period_type, read_windows, hours)
            await dao.touch_windows(period_type, read_windows)

    records: List[Dict] = []
    for row in rows:
        device = by_assignment.get(row["tag"])
        if device is None:
            continue
        stamp = row["stamp"]
        records.append({
            # The assignment the record belongs to. Consumers resolve the
            # device (and therefore the volume field) through it, so a point
            # whose corrector changed mid-range reads each stretch with the
            # model that actually measured it.
            "tag": row["tag"],
            "serNum": device["serNum"],
            "mfDev": device["mfDev"],
            "typeDev": device["typeDev"],
            "chNum": device["chNum"],
            "date": stamp.isoformat(),
            "dvstAlwrk": row["dvst_alwrk"],
            "dvwrkAlwrk": row["dvwrk_alwrk"],
            "press": row["press"],
            "temper": row["temper"],
            # Rows written before the unit was normalised on ingest may still
            # hold the literal "None" — clean them on the way out too.
            "pressUnit": normalize_press_unit(row["press_unit"]),
        })
    return records


def _read_window(assignment: Dict, window_from, window_to, period_type):
    """The stamp span an assignment contributes to a request, or None.

    Hourly rows carry the moment they describe, so the window applies as is.

    Daily rows are stored at midnight but describe the commercial day that
    OPENS at CONTRACT_HOUR, so the overlap is worked out in attribution space
    and mapped back onto the stored stamps afterwards.
    """
    win_from, win_to = assignment["win_from"], assignment["win_to"]
    if period_type == "hourly":
        return device_history.clip(win_from, win_to, window_from, window_to)

    shift = timedelta(hours=backend_settings.get("CONTRACT_HOUR", 7))
    span = device_history.clip(
        win_from, win_to, window_from + shift, window_to + shift
    )
    if span is None:
        return None
    start, end = span[0] - shift, span[1] - shift
    # A corrector fitted part-way through a commercial day does not own that
    # day: it had already opened under the previous device, which is what the
    # attribution rule says and what the write path stores. Rounding the start
    # up is what keeps the changeover day from being counted at both devices.
    if start.time() != time.min:
        start = datetime.combine(start.date() + timedelta(days=1), time.min)
        if start > end:
            return None
    return start, end


async def _plan_backfill(
    dao: DpdArchiveDao,
    device_ids: List[int],
    period_type: str,
    requested_from: date,
    date_to: datetime,
) -> Dict[int, tuple]:
    """device_id -> (span_from_date, span_to_date) for devices whose
    coverage does not reach requested_from. Never-fetched devices (added
    after the last scheduler run, or after a cache wipe) backfill the whole
    requested range; covered devices only the missing head."""
    coverage = await dao.get_coverage(list(device_ids), period_type)
    spans: Dict[int, tuple] = {}
    for device_id in device_ids:
        loaded_from = coverage.get(device_id)
        if loaded_from is None:
            spans[device_id] = (requested_from, date_to.date())
        elif requested_from < loaded_from:
            spans[device_id] = (requested_from, loaded_from - timedelta(days=1))
    return spans


async def _lock_backfill(session, backfill: Dict[int, tuple], period_type: str):
    """Sorted per-device advisory xact locks: concurrent backfills of the
    same devices serialize (the follower then finds coverage already lowered
    and skips), disjoint ones run in parallel. Backfills are rare — no
    registry needed."""
    keys = sorted(f"dpd-backfill-{device_id}-{period_type}" for device_id in backfill)
    await session.execute(
        text(
            "SELECT pg_advisory_xact_lock(hashtextextended(k, 0)) "
            "FROM unnest(CAST(:keys AS text[])) AS k"
        ),
        {"keys": keys},
    )


async def _run_backfill(
    session,
    dao: DpdArchiveDao,
    by_assignment: Dict[int, Dict],
    backfill: Dict[int, tuple],
    period_type: str,
    requested_from: date,
    date_to: datetime,
    events_cb: Optional[Callable[[Dict], None]],
) -> None:
    """Poll DPD for the devices in `backfill` and store what comes back.

    One request per DEVICE over the whole missing span — install windows are
    deliberately NOT applied here. The archive belongs to the corrector, so
    everything it answers with is its own data and worth keeping: a device that
    later turns up at another point is already loaded, and a window edited
    afterwards only moves the boundary of what the point reads. Narrowing the
    poll to windows would buy nothing and split one request into several.

    Which stretch of that data any point sees is decided on the way out, in
    `_read_window`.
    """
    contract_hour = backend_settings.get("CONTRACT_HOUR", 7)

    def span_bounds(span_from: date, span_to: date):
        """Datetime span handed to the DPD client: for hourly, commercial-day
        bounds (the client maps them back to bare DPD dates); for daily,
        midnights of the dates themselves."""
        if period_type == "hourly":
            return (
                datetime.combine(span_from, datetime.min.time())
                + timedelta(hours=contract_hour),
                datetime.combine(span_to, datetime.min.time())
                + timedelta(days=1, hours=contract_hour - 1),
            )
        return (
            datetime.combine(span_from, datetime.min.time()),
            datetime.combine(span_to, datetime.min.time()),
        )

    # One entry per device. Two assignments of the same corrector carry the
    # same identity quadruple, so either serves as the request; the tag is the
    # device, which is also what the archive is keyed by.
    poll_devices = []
    for a in by_assignment.values():
        device_id = a["device_id"]
        span = backfill.get(device_id)
        if span is None or any(d["tag"] == device_id for d in poll_devices):
            continue
        poll_devices.append({**a, "tag": device_id, "range": span_bounds(*span)})

    if not poll_devices:
        # Nothing to ask — still record that the span was asked for, so the
        # empty stretch is not re-planned on every request.
        await dao.lower_loaded_from(list(backfill), period_type, requested_from)
        return

    branch_id = pick_branch_id(poll_devices)
    if branch_id is None:
        raise LookupError("Could not determine branch for requested lines")

    poll_from = min(d["range"][0] for d in poll_devices)
    poll_to = max(d["range"][1] for d in poll_devices)
    logger.info(
        f"DPD archive: backfilling {len(poll_devices)} devices within "
        f"{poll_from}..{poll_to} ({period_type})"
    )

    progress_cb = None
    if events_cb is not None:
        progress_cb = lambda done, total: events_cb(
            {"type": "progress", "done": done, "total": total}
        )
    client = await DPDClient.for_branch(branch_id, session)
    t_poll = perf_counter()
    fresh_records = await client.get_volumes(
        poll_devices,
        poll_from,
        poll_to,
        type_request=period_type,
        progress_cb=progress_cb,
    )
    poll_secs = perf_counter() - t_poll

    polled_ids = {d["tag"] for d in poll_devices}
    rows = []
    for record in fresh_records:
        if not _has_data(record):
            continue
        stamp = _record_stamp(record, period_type)
        if stamp is None:
            continue
        # The tag is the device the record was asked for.
        device_id = record.get("tag")
        if device_id not in polled_ids:
            continue
        stamp_dt = (
            stamp if isinstance(stamp, datetime)
            else datetime.combine(stamp, datetime.min.time())
        )
        rows.append({
            "device_id": device_id,
            "stamp": stamp_dt,
            "dvst_alwrk": record.get("dvstAlwrk"),
            "dvwrk_alwrk": record.get("dvwrkAlwrk"),
            "press": record.get("press"),
            "temper": record.get("temper"),
            "press_unit": normalize_press_unit(record.get("pressUnit")),
        })
    # Deduplicate by (device, stamp) — DPD can repeat a record.
    unique = {(r["device_id"], r["stamp"]): r for r in rows}
    await dao.upsert_records(period_type, list(unique.values()))
    # The whole span was ASKED, even where DPD had nothing: coverage lowers
    # to the requested start so the empty stretches are not re-asked forever.
    await dao.lower_loaded_from(list(backfill), period_type, requested_from)
    logger.info(
        f"DPD archive: backfill poll {poll_secs:.1f}s "
        f"({len(fresh_records)} records, {len(unique)} stored)"
    )


def _parse_record_period(raw, period_type: str):
    """Normalize the DPD record timestamp: datetime for hourly, date for daily.
    Returns None (record skipped) on an unparseable value."""
    try:
        if period_type == "hourly":
            if isinstance(raw, str):
                clean = raw.split(".")[0]  # strip microseconds
                try:
                    return datetime.strptime(clean, "%Y-%m-%dT%H:%M:%S")
                except ValueError:
                    return datetime.strptime(clean, "%Y-%m-%dT%H:%M")
            if isinstance(raw, datetime):
                return raw
        else:
            if isinstance(raw, str):
                return datetime.strptime(raw.split("T")[0], "%Y-%m-%d").date()
            if isinstance(raw, datetime):
                return raw.date()
            if isinstance(raw, date):
                return raw
    except Exception as e:
        logger.warning(f"Error parsing period {raw}: {e}")
        return None
    logger.warning(f"Invalid period value: {raw!r}")
    return None


def aggregate_volumes(
    volumes_data: List[Dict],
    devices: List[Dict],
    period_type: str,
    line_remap: Optional[Dict[int, list]] = None,
    none_volume_as_zero: bool = False,
    include_devices: bool = True,
) -> List[EnterpriseVolumeResponse]:
    """Group raw DPD records into per-(line_id, period) responses.

    `include_devices=False` returns the same totals without the per-device
    breakdown. Callers that only want line totals used to get it by emptying
    `.devices` afterwards, which meant building a quarter of a million
    pydantic objects — six seconds on a month over a branch — and dropping
    every one of them. The device COUNT is still exact.
    """
    t_start = perf_counter()
    # Keyed by assignment: a record's device is whichever one was in force at
    # its stamp, which is also what decides the volume field below.
    device_map = {
        d["assignment_id"]: d for d in devices if d.get("assignment_id") is not None
    }
    aggregated = defaultdict(lambda: {"total": 0.0, "devices": [], "count": 0})
    # One stamp per hour of the range, repeated once per device: a month over
    # a branch is a quarter of a million records over some seven hundred
    # distinct stamps. strptime was the most expensive call in this function
    # by a wide margin, so it runs once per distinct stamp instead of once
    # per record. Local to the call — stamps are request data, not a cache
    # worth keeping.
    period_cache: Dict = {}

    for record in volumes_data:
        device_info = device_map.get(record.get("tag"))
        if not device_info:
            logger.warning(f"Record with unknown assignment: {record.get('tag')}")
            continue

        # A physical line reports to every requested line it belongs to:
        # itself (when requested directly) and/or its virtual parents.
        if line_remap is not None:
            line_keys = line_remap.get(device_info["line_id"]) or []
            if not isinstance(line_keys, (list, tuple)):
                line_keys = [line_keys]
            if not line_keys:
                logger.debug(
                    f"Physical line {device_info['line_id']} not in requested lines, skipping"
                )
                continue
        else:
            line_keys = [device_info["line_id"]]

        # Most devices report the commercial volume in dvstAlwrk; a few models
        # (ТКБ, smart104) report it in dvwrkAlwrk — selected by device identity.
        volume = record.get(
            volume_field_for_device(device_info["mfDev"], device_info["typeDev"])
        )
        if volume is None and none_volume_as_zero:
            volume = 0.0

        raw_period = record.get("date") or record.get("period")
        if not raw_period:
            logger.warning(f"No date field in record: {record}")
            continue
        try:
            record_period = period_cache[raw_period]
        except KeyError:
            record_period = _parse_record_period(raw_period, period_type)
            period_cache[raw_period] = record_period
        if record_period is None:
            continue

        pressure_unit = (
            normalize_press_unit(record.get("pressUnit")) if include_devices else None
        )

        for line_key in line_keys:
            key = (line_key, record_period)
            entry = aggregated[key]
            if volume is not None:
                entry["total"] += volume
            entry["count"] += 1
            if include_devices:
                entry["devices"].append(
                    DeviceVolume(
                        serNum=device_info["serNum"],
                        mfDev=device_info["mfDev"],
                        typeDev=device_info["typeDev"],
                        chNum=device_info["chNum"],
                        enterprise_name=device_info.get("enterprise_name", ""),
                        volume=volume,
                        temperature=record.get("temper"),
                        pressure=record.get("press"),
                        pressure_unit=pressure_unit,
                    )
                )

    result = [
        EnterpriseVolumeResponse(
            line_id=line_id_val,
            period=period_val,
            total_volume=round(data["total"], 2),
            device_count=data["count"],
            devices=data["devices"],
        )
        for (line_id_val, period_val), data in aggregated.items()
    ]
    result.sort(key=lambda x: (x.line_id is None, x.line_id or 0, x.period))
    logger.info(
        f"Aggregated {len(volumes_data)} records into {len(result)} responses "
        f"in {perf_counter() - t_start:.1f}s"
    )
    return result
