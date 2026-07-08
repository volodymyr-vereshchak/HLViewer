"""
Shared pipeline for the enterprise volume endpoints.

/enterprise/volumes/ and /enterprise/volumes_virtual/ used to carry two ~150-line
copies of the same flow: parse the date range, resolve devices, call the DPD API
for one branch, then aggregate device records into per-(line, period) responses.
The only real differences are captured by two parameters:

- ``line_remap``: physical line_id → reported line_id (virtual parent). None
  keeps the device's own line_id (plain endpoint); with a mapping, devices whose
  line is not in it are skipped (they were not requested).
- ``none_volume_as_zero``: the virtual endpoint has always reported missing
  volumes as 0.0, the plain one preserves None (the frontend shows a gap).
"""

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from time import perf_counter
from typing import Dict, List, Optional

from sqlalchemy import text

from backend.db.engine import async_session_factory
from backend.db.dao.dpd_cache_dao import DpdCacheDao
from backend.db.models.enterprise_models import DeviceVolume, EnterpriseVolumeResponse
from backend.services.dpd_client import DPDClient
from backend.services.enterprise_mappings import volume_field_for_device
from backend.settings import backend_settings

logger = logging.getLogger(__name__)

# Everything a poll returns is cached and lives 7 days sliding: every read
# touches fetched_at (at most once per TOUCH_MIN_AGE to avoid write churn)
# and delete_older_than prunes rows nobody viewed for a week. A row fetched
# after its gas day closed (CONTRACT_HOUR boundary) is FINAL and is served
# as-is, never re-validated. A row fetched while its day was still open is a
# snapshot of an unfinished day: the day stays a "gap" and is re-polled on
# every request (each poll overwrites the snapshot; the snapshot itself only
# serves as a fallback when the poll brings nothing back). Empty days and
# 404 devices are never cached and are likewise re-asked every request.
TOUCH_MIN_AGE = timedelta(hours=1)

# Only the fields the aggregation/UI actually reads are cached; device
# identifiers live in the row key and are restored on read. Cuts the JSONB
# payload (and upsert time) to a fraction of the raw DPD record.
_PAYLOAD_FIELDS = ("date", "period", "dvstAlwrk", "dvwrkAlwrk", "press", "temper", "pressUnit")


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


def _day_closed_at(day: date) -> datetime:
    """When gas day D becomes final: D+1 at CONTRACT_HOUR (07:00). A row
    fetched before that moment is a snapshot of an unfinished day."""
    contract_hour = backend_settings.get("CONTRACT_HOUR", 7)
    return datetime.combine(day + timedelta(days=1), datetime.min.time()) + timedelta(
        hours=contract_hour
    )


def _slim_record(record: Dict) -> Dict:
    return {k: record[k] for k in _PAYLOAD_FIELDS if k in record}


def _restore_ids(payload: list, row) -> list:
    """Re-attach device identifiers stripped by _slim_record on write.

    setdefault keeps pre-whitelist rows (full raw records) readable as-is."""
    restored = []
    for rec in payload:
        rec = dict(rec)
        rec.setdefault("serNum", row.ser_num)
        rec.setdefault("mfDev", row.mf_dev)
        rec.setdefault("typeDev", row.type_dev)
        rec.setdefault("chNum", row.ch_num)
        restored.append(rec)
    return restored


def _record_day(record: Dict, period_type: str) -> Optional[date]:
    """Calendar day of a raw DPD record (cache key granularity)."""
    raw = record.get("date") or record.get("period")
    if not raw:
        return None
    parsed = _parse_record_period(raw, period_type)
    if parsed is None:
        return None
    return parsed.date() if isinstance(parsed, datetime) else parsed


def _device_key(device: Dict) -> tuple:
    return (device["serNum"], device["mfDev"], device["typeDev"], device["chNum"])


async def fetch_dpd_volumes(
    devices: List[Dict],
    date_from: datetime,
    date_to: datetime,
    period_type: str,
) -> List[Dict]:
    """Raw volume records for the requested devices/range, DPD polled only for
    what the Postgres cache cannot serve.

    The whole read-poll-write cycle runs inside one transaction holding a
    per-branch advisory lock: identical or overlapping requests from any
    uvicorn worker (browser auto-retries, double-fired frontend fetches,
    several users) serialize here, and every follower finds the leader's rows
    already cached instead of launching its own DPD poll. It also means at
    most one poll per branch hits DPD at a time, capping the load at one
    connection pool.

    Raises LookupError when no device carries a branch_id, ValueError when the
    branch has no/incomplete credentials (DPDClient.for_branch), and whatever
    the HTTP client raises when the API is unreachable."""
    branch_id = pick_branch_id(devices)
    if branch_id is None:
        raise LookupError("Could not determine branch for requested lines")

    day_from, day_to = date_from.date(), date_to.date()
    requested_days = [
        day_from + timedelta(days=i) for i in range((day_to - day_from).days + 1)
    ]

    async with async_session_factory() as session:
        async with session.begin():
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"dpd-branch-{branch_id}"},
            )

            dao = DpdCacheDao(session)
            now = datetime.now()

            # (serNum, mfDev, typeDev, chNum, day) -> cached payload. Rows
            # fetched after their day closed are final; the rest are
            # unfinished-day snapshots (kept only as a poll fallback).
            cached: Dict[tuple, list] = {}
            final_keys: set = set()
            touch_ids: List[int] = []
            for row in await dao.load_range(devices, period_type, day_from, day_to):
                key5 = (row.ser_num, row.mf_dev, row.type_dev, row.ch_num, row.day)
                cached[key5] = _restore_ids(row.payload, row)
                if row.fetched_at >= _day_closed_at(row.day):
                    final_keys.add(key5)
                if now - row.fetched_at >= TOUCH_MIN_AGE:
                    touch_ids.append(row.id)

            # Days without a final row are polled, each device over its own
            # missing span — a device with no cached data at all must not
            # force the whole branch to re-download the full range. The
            # still-open gas day, days DPD returned nothing for and 404
            # devices come up missing every request by construction.
            poll_devices: List[Dict] = []
            device_spans: Dict[tuple, tuple] = {}
            for device in devices:
                key = _device_key(device)
                device_missing = [
                    d for d in requested_days if key + (d,) not in final_keys
                ]
                if device_missing:
                    poll_devices.append(device)
                    device_spans[key] = (min(device_missing), max(device_missing))

            fresh_groups: Dict[tuple, list] = defaultdict(list)
            if poll_devices:
                poll_from = min(span[0] for span in device_spans.values())
                poll_to = max(span[1] for span in device_spans.values())
                narrower = sum(
                    1 for span in device_spans.values() if span != (poll_from, poll_to)
                )
                logger.info(
                    f"DPD cache: {len(final_keys)} device-days final, polling "
                    f"{len(poll_devices)}/{len(devices)} devices within "
                    f"{poll_from}..{poll_to} ({period_type}, "
                    f"{narrower} narrower per-device spans)"
                )
                client = await DPDClient.for_branch(branch_id, session)
                t_poll = perf_counter()
                fresh_records = await client.get_volumes(
                    poll_devices,
                    datetime.combine(poll_from, datetime.min.time()),
                    datetime.combine(poll_to, datetime.min.time()),
                    type_request=period_type,
                    device_ranges={
                        key: (
                            datetime.combine(span[0], datetime.min.time()),
                            datetime.combine(span[1], datetime.min.time()),
                        )
                        for key, span in device_spans.items()
                    },
                )
                poll_secs = perf_counter() - t_poll

                t_store = perf_counter()
                for record in fresh_records:
                    record_day = _record_day(record, period_type)
                    if record_day is not None:
                        fresh_groups[(record["serNum"], record["mfDev"],
                                      record["typeDev"], record["chNum"],
                                      record_day)].append(record)
                # Cache every polled day inside the device's own span (the
                # hourly to+1 widening in dpd_client returns records past the
                # span — that partial tail day must not be cached). An
                # unfinished day's row is simply overwritten by the next poll.
                upsert_rows = [
                    {
                        "ser_num": ser_num,
                        "mf_dev": mf_dev,
                        "type_dev": type_dev,
                        "ch_num": ch_num,
                        "period_type": period_type,
                        "day": record_day,
                        "payload": [_slim_record(r) for r in payload],
                        "fetched_at": now,
                    }
                    for (ser_num, mf_dev, type_dev, ch_num, record_day), payload
                    in fresh_groups.items()
                    if (span := device_spans.get((ser_num, mf_dev, type_dev, ch_num)))
                    and span[0] <= record_day <= span[1]
                ]
                if upsert_rows:
                    await dao.upsert_days(upsert_rows)
                await dao.delete_older_than(now)
                logger.info(
                    f"DPD cache: poll {poll_secs:.1f}s ({len(fresh_records)} records), "
                    f"store {perf_counter() - t_store:.1f}s ({len(upsert_rows)} rows)"
                )
            else:
                logger.info(
                    f"DPD cache: full hit, {len(final_keys)} device-days for "
                    f"{len(devices)} devices {day_from}..{day_to} ({period_type})"
                )

            if touch_ids:
                # Sliding TTL: reading final rows keeps them alive another week.
                await dao.touch(touch_ids, now)

    # Merge: for a polled device, days inside its span come from the fresh
    # poll (that day was fully re-fetched, a cached copy would duplicate it);
    # everything else from cache. A cached day inside the span is still
    # served when the poll brought nothing for it (unfinished-day snapshot as
    # fallback). Fresh records past a span (the hourly widening tail) are
    # used only when the cache has no copy of that day.
    records: List[Dict] = []
    for key5, group in fresh_groups.items():
        span = device_spans.get(key5[:4])
        in_span = span is not None and span[0] <= key5[4] <= span[1]
        if in_span or key5 not in cached:
            records.extend(group)
    for key5, payload in cached.items():
        span = device_spans.get(key5[:4])
        if (
            span is not None
            and span[0] <= key5[4] <= span[1]
            and key5 in fresh_groups
        ):
            continue  # replaced by the fresh re-poll of that day
        records.extend(payload)
    return records


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
    line_remap: Optional[Dict[int, int]] = None,
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

        line_key = device_info["line_id"]
        if line_remap is not None:
            line_key = line_remap.get(line_key)
            if line_key is None:
                logger.debug(
                    f"Physical line {device_info['line_id']} not in requested lines, skipping"
                )
                continue

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

        pressure_unit = record.get("pressUnit")
        if isinstance(pressure_unit, str):
            pressure_unit = pressure_unit.strip() or None

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
