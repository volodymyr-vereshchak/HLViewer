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
"""

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from time import perf_counter
from typing import Callable, Dict, List, Optional

from sqlalchemy import text

from backend.db.engine import async_session_factory
from backend.db.dao.dpd_archive_dao import DpdArchiveDao
from backend.db.models.enterprise_models import DeviceVolume, EnterpriseVolumeResponse
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


def _request_window(date_from: datetime, date_to: datetime, period_type: str):
    """The stamp window a from/to date range denotes. Both our endpoint and
    DPD take bare dates; for hourly data a date is a commercial day, so
    from=D1&to=D2 means [D1 CONTRACT_HOUR .. D2+1 CONTRACT_HOUR-1h] — exactly
    what DPD returns for that range. Daily: calendar days D1..D2."""
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


def _device_key(device: Dict) -> tuple:
    return (device["serNum"], device["mfDev"], device["typeDev"], device["chNum"])


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

    Raises LookupError when a backfill is needed but no device carries a
    branch_id, ValueError when the branch has no/incomplete credentials
    (DPDClient.for_branch), and whatever the HTTP client raises when the DPD
    API is unreachable mid-backfill."""
    window_from, window_to = _request_window(date_from, date_to, period_type)
    requested_from = date_from.date()
    ent_by_id: Dict[int, Dict] = {
        d["id"]: d for d in devices if d.get("id") is not None
    }
    if len(ent_by_id) < len(devices):
        logger.warning(
            f"{len(devices) - len(ent_by_id)} device(s) without enterprise id "
            f"skipped in fetch_dpd_volumes"
        )
    if not ent_by_id:
        return []

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
                    ent_id: (requested_from, date_to.date())
                    for ent_id in ent_by_id
                }
                if events_cb is not None:
                    events_cb({"type": "progress", "done": 0,
                               "total": len(spans)})
                    events_cb({"type": "status", "phase": "waiting"})
                await _lock_backfill(session, spans, period_type)
                try:
                    await _run_backfill(
                        session, dao, ent_by_id, spans, period_type,
                        requested_from, events_cb,
                    )
                except Exception:
                    logger.exception(
                        "Live DPD poll failed — serving archive data as fallback"
                    )
            else:
                backfill = await _plan_backfill(
                    dao, ent_by_id, period_type, requested_from, date_to
                )
                if events_cb is not None:
                    events_cb({"type": "progress", "done": 0, "total": len(backfill)})
                if backfill:
                    if events_cb is not None:
                        events_cb({"type": "status", "phase": "waiting"})
                    await _lock_backfill(session, backfill, period_type)
                    # A concurrent request may have backfilled while we waited.
                    backfill = await _plan_backfill(
                        dao, {e: ent_by_id[e] for e in backfill}, period_type,
                        requested_from, date_to,
                    )
                    if backfill:
                        await _run_backfill(
                            session, dao, ent_by_id, backfill, period_type,
                            requested_from, events_cb,
                        )
            if events_cb is not None:
                events_cb({"type": "status", "phase": "aggregating"})

            # The DB is the sole source for the response.
            rows = await dao.load_range(
                list(ent_by_id), period_type, window_from, window_to
            )
            await dao.touch_accessed(
                list(ent_by_id), period_type, window_from, window_to
            )

    records: List[Dict] = []
    for row in rows:
        device = ent_by_id.get(row["enterprise_id"])
        if device is None:
            continue
        stamp = row["stamp"]
        records.append({
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


async def _plan_backfill(
    dao: DpdArchiveDao,
    ent_by_id: Dict[int, Dict],
    period_type: str,
    requested_from: date,
    date_to: datetime,
) -> Dict[int, tuple]:
    """enterprise_id -> (span_from_date, span_to_date) for devices whose
    coverage does not reach requested_from. Never-fetched devices (added
    after the last scheduler run, or after a cache wipe) backfill the whole
    requested range; covered devices only the missing head."""
    coverage = await dao.get_coverage(list(ent_by_id), period_type)
    spans: Dict[int, tuple] = {}
    for ent_id in ent_by_id:
        loaded_from = coverage.get(ent_id)
        if loaded_from is None:
            spans[ent_id] = (requested_from, date_to.date())
        elif requested_from < loaded_from:
            spans[ent_id] = (requested_from, loaded_from - timedelta(days=1))
    return spans


async def _lock_backfill(session, backfill: Dict[int, tuple], period_type: str):
    """Sorted per-device advisory xact locks: concurrent backfills of the
    same devices serialize (the follower then finds coverage already lowered
    and skips), disjoint ones run in parallel. Backfills are rare — no
    registry needed."""
    keys = sorted(f"dpd-backfill-{ent_id}-{period_type}" for ent_id in backfill)
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
    ent_by_id: Dict[int, Dict],
    backfill: Dict[int, tuple],
    period_type: str,
    requested_from: date,
    events_cb: Optional[Callable[[Dict], None]],
) -> None:
    poll_devices = [ent_by_id[ent_id] for ent_id in backfill]
    branch_id = pick_branch_id(poll_devices)
    if branch_id is None:
        raise LookupError("Could not determine branch for requested lines")

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

    device_ranges = {
        _device_key(ent_by_id[ent_id]): span_bounds(*span)
        for ent_id, span in backfill.items()
    }
    poll_from = min(r[0] for r in device_ranges.values())
    poll_to = max(r[1] for r in device_ranges.values())
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
        device_ranges=device_ranges,
        progress_cb=progress_cb,
    )
    poll_secs = perf_counter() - t_poll

    ent_by_quad = {_device_key(d): ent_id for ent_id, d in ent_by_id.items()}
    rows = []
    for record in fresh_records:
        if not _has_data(record):
            continue
        stamp = _record_stamp(record, period_type)
        if stamp is None:
            continue
        ent_id = ent_by_quad.get((record["serNum"], record["mfDev"],
                                  record["typeDev"], record["chNum"]))
        if ent_id is None:
            continue
        rows.append({
            "enterprise_id": ent_id,
            "stamp": stamp if isinstance(stamp, datetime)
            else datetime.combine(stamp, datetime.min.time()),
            "dvst_alwrk": record.get("dvstAlwrk"),
            "dvwrk_alwrk": record.get("dvwrkAlwrk"),
            "press": record.get("press"),
            "temper": record.get("temper"),
            "press_unit": normalize_press_unit(record.get("pressUnit")),
        })
    # Deduplicate by (enterprise, stamp) — DPD can repeat a record.
    unique = {(r["enterprise_id"], r["stamp"]): r for r in rows}
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
) -> List[EnterpriseVolumeResponse]:
    """Group raw DPD records into per-(line_id, period) responses."""
    t_start = perf_counter()
    device_map = {
        (d["serNum"], d["mfDev"], d["typeDev"], d["chNum"]): d for d in devices
    }
    aggregated = defaultdict(lambda: {"total": 0.0, "devices": []})

    for record in volumes_data:
        device_key = (
            record.get("serNum"),
            record.get("mfDev"),
            record.get("typeDev"),
            record.get("chNum"),
        )
        device_info = device_map.get(device_key)
        if not device_info:
            logger.warning(f"Device not found in mappings: {device_key}")
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
        record_period = _parse_record_period(raw_period, period_type)
        if record_period is None:
            continue

        pressure_unit = normalize_press_unit(record.get("pressUnit"))

        for line_key in line_keys:
            key = (line_key, record_period)
            if volume is not None:
                aggregated[key]["total"] += volume
            aggregated[key]["devices"].append(
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
            device_count=len(data["devices"]),
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
