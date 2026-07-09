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

# Cache model: the gap unit is the record timestamp — an hour for hourly
# data, a day for daily (independent typeRequest endpoints on DPD's side).
# Every record a poll returns WITH DATA is final the moment it arrives and
# is cached, even outside the requested window; DPD also returns skeleton
# records with null volumes for stamps it has no data for (yet) — those are
# gaps, not data (see _has_data). A request re-polls only the stamps missing
# from the cache (unpublished/future hours, holes, null skeletons, empty
# responses) plus 404 devices, each device over its own missing span. fetched_at
# carries no freshness meaning — it only drives the 7-day sliding
# retention: reads touch it (at most once per TOUCH_MIN_AGE to avoid write
# churn) and delete_older_than prunes rows nobody viewed for a week.
# For hourly data a bare date range means commercial days, mirroring DPD:
# from=D1&to=D2 denotes the stamp window [D1 07:00 .. D2+1 06:00].
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
    return date_from.date(), date_to.date()


def _expected_stamps(window_from, window_to, period_type: str) -> list:
    """Every record stamp the window should contain: hourly datetimes or
    daily dates, both bounds inclusive."""
    if period_type == "hourly":
        hours = int((window_to - window_from).total_seconds() // 3600)
        return [window_from + timedelta(hours=i) for i in range(hours + 1)]
    return [
        window_from + timedelta(days=i)
        for i in range((window_to - window_from).days + 1)
    ]


def _slim_record(record: Dict) -> Dict:
    return {k: record[k] for k in _PAYLOAD_FIELDS if k in record}


def _has_data(record: Dict) -> bool:
    """False for DPD's skeleton records: a stamp with both volume fields null
    carries no data yet. Such records are never cached and never satisfy a
    stamp on read — the stamp stays a gap re-asked until real values appear.
    (Observed on prod 2026-07-09: an intraday poll cached a full commercial
    day of nulls and the holes were never re-polled.)"""
    return record.get("dvstAlwrk") is not None or record.get("dvwrkAlwrk") is not None


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


def _record_stamp(record: Dict, period_type: str):
    """Stamp of a raw DPD record: datetime for hourly, date for daily,
    None when unparseable (record skipped)."""
    raw = record.get("date") or record.get("period")
    if not raw:
        return None
    return _parse_record_period(raw, period_type)


def _stamp_day(stamp) -> date:
    """Calendar day a stamp is stored under (cache rows bucket by day)."""
    return stamp.date() if isinstance(stamp, datetime) else stamp


def _as_dt(stamp) -> datetime:
    return stamp if isinstance(stamp, datetime) else datetime.combine(
        stamp, datetime.min.time()
    )


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

    window_from, window_to = _request_window(date_from, date_to, period_type)
    expected = _expected_stamps(window_from, window_to, period_type)
    day_from, day_to = date_from.date(), date_to.date()

    async with async_session_factory() as session:
        async with session.begin():
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"dpd-branch-{branch_id}"},
            )

            dao = DpdCacheDao(session)
            now = datetime.now()

            # (serNum, mfDev, typeDev, chNum) -> {stamp -> record}. Every
            # cached record is final. Rows are loaded one day beyond the
            # window on each side: the poll's commercial-date rounding can
            # return records on those days and the merge below must not lose
            # their cached siblings.
            cached: Dict[tuple, Dict] = defaultdict(dict)
            touch_ids: List[int] = []
            for row in await dao.load_range(
                devices,
                period_type,
                day_from - timedelta(days=1),
                day_to + timedelta(days=1),
            ):
                key4 = (row.ser_num, row.mf_dev, row.type_dev, row.ch_num)
                for rec in _restore_ids(row.payload, row):
                    stamp = _record_stamp(rec, period_type)
                    if stamp is not None and _has_data(rec):
                        cached[key4][stamp] = rec
                if now - row.fetched_at >= TOUCH_MIN_AGE:
                    touch_ids.append(row.id)

            # A device is polled over the span of its own missing stamps —
            # unpublished/future hours, holes and 404 devices come up missing
            # every request by construction, while a device with no cached
            # data at all must not force the whole branch to re-download the
            # full range.
            poll_devices: List[Dict] = []
            device_spans: Dict[tuple, tuple] = {}
            for device in devices:
                key4 = _device_key(device)
                missing = [s for s in expected if s not in cached[key4]]
                if missing:
                    poll_devices.append(device)
                    device_spans[key4] = (_as_dt(min(missing)), _as_dt(max(missing)))

            fresh: Dict[tuple, Dict] = defaultdict(dict)
            if poll_devices:
                poll_from = min(span[0] for span in device_spans.values())
                poll_to = max(span[1] for span in device_spans.values())
                narrower = sum(
                    1 for span in device_spans.values() if span != (poll_from, poll_to)
                )
                cached_stamps = sum(len(v) for v in cached.values())
                logger.info(
                    f"DPD cache: {cached_stamps} stamps cached, polling "
                    f"{len(poll_devices)}/{len(devices)} devices within "
                    f"{poll_from}..{poll_to} ({period_type}, "
                    f"{narrower} narrower per-device spans)"
                )
                client = await DPDClient.for_branch(branch_id, session)
                t_poll = perf_counter()
                fresh_records = await client.get_volumes(
                    poll_devices,
                    poll_from,
                    poll_to,
                    type_request=period_type,
                    device_ranges=device_spans,
                )
                poll_secs = perf_counter() - t_poll

                t_store = perf_counter()
                for record in fresh_records:
                    stamp = _record_stamp(record, period_type)
                    if stamp is not None:
                        fresh[(record["serNum"], record["mfDev"],
                               record["typeDev"], record["chNum"])][stamp] = record

                # Everything the poll returned with data is cached (records
                # outside the requested window are final data too — they warm
                # the cache; null skeletons are not, see _has_data). Fresh
                # stamps are merged into the day rows they belong to, fresh
                # winning over cached, and only days that actually got new
                # records are written.
                upsert_rows = []
                for key4, stamps in fresh.items():
                    by_day: Dict[date, Dict] = defaultdict(dict)
                    for stamp, rec in stamps.items():
                        if _has_data(rec):
                            by_day[_stamp_day(stamp)][stamp] = rec
                    for day, day_stamps in by_day.items():
                        merged = {
                            s: r
                            for s, r in cached[key4].items()
                            if _stamp_day(s) == day
                        }
                        merged.update(day_stamps)
                        upsert_rows.append({
                            "ser_num": key4[0],
                            "mf_dev": key4[1],
                            "type_dev": key4[2],
                            "ch_num": key4[3],
                            "period_type": period_type,
                            "day": day,
                            "payload": [
                                _slim_record(merged[s]) for s in sorted(merged)
                            ],
                            "fetched_at": now,
                        })
                if upsert_rows:
                    await dao.upsert_days(upsert_rows)
                await dao.delete_older_than(now)
                logger.info(
                    f"DPD cache: poll {poll_secs:.1f}s ({len(fresh_records)} records), "
                    f"store {perf_counter() - t_store:.1f}s ({len(upsert_rows)} rows)"
                )
            else:
                logger.info(
                    f"DPD cache: full hit, "
                    f"{sum(len(v) for v in cached.values())} stamps for "
                    f"{len(devices)} devices {window_from}..{window_to} "
                    f"({period_type})"
                )

            if touch_ids:
                # Sliding retention: reading rows keeps them alive another week.
                await dao.touch(touch_ids, now)

    # Response: cached ∪ fresh per stamp (fresh wins), clipped to the window.
    records: List[Dict] = []
    for key4, stamps in cached.items():
        dev_fresh = fresh.get(key4, {})
        records.extend(
            rec
            for stamp, rec in stamps.items()
            if stamp not in dev_fresh and window_from <= stamp <= window_to
        )
    for stamps in fresh.values():
        records.extend(
            rec
            for stamp, rec in stamps.items()
            if window_from <= stamp <= window_to
        )
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
