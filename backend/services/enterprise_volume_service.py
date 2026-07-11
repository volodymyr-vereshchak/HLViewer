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

import hashlib
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from time import perf_counter
from typing import Callable, Dict, List, Optional
from uuid import uuid4

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
# responses) plus 404 devices, each device over its own missing span.
# Polled days that returned nothing still get an empty row — the mark that
# the range was TRIED: a hole older than HOLE_RETRY_WINDOW whose day was
# already tried is permanent (the data will never appear at DPD) and is not
# re-polled, so ancient holes stop stretching the poll spans. Untried ranges
# (cold cache, cache cleared) are always fetched regardless of age. fetched_at
# carries no freshness meaning — it only drives the 7-day sliding
# retention: reads touch it (at most once per TOUCH_MIN_AGE to avoid write
# churn) and delete_older_than prunes rows nobody viewed for a week.
# For hourly data a bare date range means commercial days, mirroring DPD:
# from=D1&to=D2 denotes the stamp window [D1 07:00 .. D2+1 06:00].
TOUCH_MIN_AGE = timedelta(hours=1)

# Missing stamps older than this are re-asked only while their day was never
# polled; once tried, they are permanent holes. Late-arriving telemetry lands
# within a couple of days — 3 is a safe margin (user decision 2026-07-11).
HOLE_RETRY_WINDOW = timedelta(days=3)

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


# ── Active-poll registry ─────────────────────────────────────────────────────
# Concurrency rule (user decision 2026-07-11): polls run in PARALLEL unless a
# later request is fully contained (window AND devices) in one already talking
# to DPD — then it waits on that poll's advisory lock and is served from
# cache. Small overlaps of parallel polls are made safe by the SQL-side
# payload merge in DpdCacheDao.upsert_days.

_POLL_REGISTRY_MUTEX = "dpd-poll-registry"


def _device_hash(key4: tuple) -> int:
    """Stable cross-process 64-bit device id (Python's hash() is salted)."""
    raw = "{}-{}-{}-{}".format(*key4).encode()
    return int.from_bytes(
        hashlib.blake2b(raw, digest_size=8).digest(), "big", signed=True
    )


async def _find_or_register_poll(
    main_session, period_type: str,
    window_from: datetime, window_to: datetime, device_hashes: List[int],
):
    """Containment check + registration, atomically under a registry mutex.

    Returns ("wait", lock_key) when a running poll fully covers this request,
    else ("lead", own lock_key). For a leader the EXCLUSIVE advisory lock is
    taken on main_session BEFORE the registry row becomes visible (the aux
    transaction commits after), so no follower can grab the shared lock while
    the leader does not hold the exclusive one yet."""
    async with async_session_factory() as aux:
        async with aux.begin():
            await aux.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                {"k": _POLL_REGISTRY_MUTEX},
            )
            # Lazy cleanup: rows of crashed/forgotten polls. Nobody can hang
            # on them anyway — the advisory lock died with its transaction.
            await aux.execute(
                text("DELETE FROM dpd_active_poll WHERE started_at < :cutoff"),
                {"cutoff": datetime.now() - timedelta(hours=1)},
            )
            row = (await aux.execute(
                text(
                    "SELECT lock_key FROM dpd_active_poll "
                    "WHERE period_type = :pt AND window_from <= :wf "
                    "AND window_to >= :wt "
                    "AND device_hashes @> CAST(:hashes AS bigint[]) "
                    "ORDER BY started_at LIMIT 1"
                ),
                {"pt": period_type, "wf": window_from, "wt": window_to,
                 "hashes": device_hashes},
            )).first()
            if row:
                return "wait", row[0]

            lock_key = f"dpd-poll-{uuid4().hex}"
            await main_session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                {"k": lock_key},
            )
            await aux.execute(
                text(
                    "INSERT INTO dpd_active_poll (lock_key, period_type, "
                    "window_from, window_to, device_hashes, started_at) "
                    "VALUES (:k, :pt, :wf, :wt, CAST(:hashes AS bigint[]), :now)"
                ),
                {"k": lock_key, "pt": period_type, "wf": window_from,
                 "wt": window_to, "hashes": device_hashes,
                 "now": datetime.now()},
            )
            return "lead", lock_key


async def _unregister_poll(lock_key: str) -> None:
    try:
        async with async_session_factory() as aux:
            async with aux.begin():
                await aux.execute(
                    text("DELETE FROM dpd_active_poll WHERE lock_key = :k"),
                    {"k": lock_key},
                )
    except Exception:
        # A leftover row is harmless (lazy cleanup, nobody hangs on it).
        logger.exception(f"Failed to unregister DPD poll {lock_key}")


def _device_key(device: Dict) -> tuple:
    return (device["serNum"], device["mfDev"], device["typeDev"], device["chNum"])


async def fetch_dpd_volumes(
    devices: List[Dict],
    date_from: datetime,
    date_to: datetime,
    period_type: str,
    events_cb: Optional[Callable[[Dict], None]] = None,
) -> List[Dict]:
    """Raw volume records for the requested devices/range, DPD polled only for
    what the Postgres cache cannot serve.

    ``events_cb`` (optional, SYNCHRONOUS, must be non-blocking) receives
    progress events for streaming endpoints:
    {"type":"status","phase":"waiting"} while a containing poll runs,
    {"type":"progress","done":N,"total":M} as device polls complete,
    {"type":"status","phase":"aggregating"} once polling is over. It runs on
    the polling path — a slow consumer must drop events, never block here.

    Concurrency: polls run in parallel. A request waits ONLY when its window
    and device set are fully contained in a poll already talking to DPD (see
    the active-poll registry above) — it is then served from what that poll
    stored, instead of re-downloading the same data. Fully-cached requests
    never touch the registry and never wait. Concurrent writes to shared
    boundary rows are merged stamp-wise in SQL (DpdCacheDao.upsert_days).

    Raises LookupError when no device carries a branch_id, ValueError when the
    branch has no/incomplete credentials (DPDClient.for_branch), and whatever
    the HTTP client raises when the API is unreachable."""
    poll_key_holder: List[Optional[str]] = [None]
    try:
        return await _fetch_dpd_volumes_impl(
            devices, date_from, date_to, period_type, events_cb,
            poll_key_holder,
        )
    finally:
        # Runs on success, failure AND cancellation (the stream reaper awaits
        # this unwind), after the transaction — and with it the exclusive
        # advisory lock — is already gone.
        if poll_key_holder[0]:
            await _unregister_poll(poll_key_holder[0])


async def _fetch_dpd_volumes_impl(
    devices: List[Dict],
    date_from: datetime,
    date_to: datetime,
    period_type: str,
    events_cb: Optional[Callable[[Dict], None]],
    poll_key_holder: List[Optional[str]],
) -> List[Dict]:
    branch_id = pick_branch_id(devices)
    if branch_id is None:
        raise LookupError("Could not determine branch for requested lines")

    window_from, window_to = _request_window(date_from, date_to, period_type)
    expected = _expected_stamps(window_from, window_to, period_type)
    day_from, day_to = date_from.date(), date_to.date()

    async with async_session_factory() as session:
        async with session.begin():
            # Safety nets against a wedged poll: don't queue on advisory locks
            # forever (a stuck poll would otherwise stack every following
            # request and drain the connection pool — the whole app hangs),
            # and let Postgres kill a leaked idle-in-transaction connection as
            # a last resort. The cap is generous because a healthy poll keeps
            # the transaction idle the whole time it talks to DPD.
            await session.execute(text("SET LOCAL lock_timeout = '300s'"))
            await session.execute(
                text("SET LOCAL idle_in_transaction_session_timeout = '1800s'")
            )

            dao = DpdCacheDao(session)
            now = datetime.now()

            # (serNum, mfDev, typeDev, chNum) -> {stamp -> record}. Every
            # cached record is final. Rows are loaded one day beyond the
            # window on each side: the poll's commercial-date rounding can
            # return records on those days and the merge below must not lose
            # their cached siblings.
            cached: Dict[tuple, Dict] = defaultdict(dict)
            tried_days: set = set()  # (key4, day) rows that exist, even empty
            touch_ids: List[int] = []

            def _absorb_row(row) -> None:
                key4 = (row.ser_num, row.mf_dev, row.type_dev, row.ch_num)
                tried_days.add((key4, row.day))
                for rec in _restore_ids(row.payload, row):
                    stamp = _record_stamp(rec, period_type)
                    if stamp is not None and _has_data(rec):
                        cached[key4][stamp] = rec

            for row in await dao.load_range(
                devices,
                period_type,
                day_from - timedelta(days=1),
                day_to + timedelta(days=1),
            ):
                _absorb_row(row)
                if now - row.fetched_at >= TOUCH_MIN_AGE:
                    touch_ids.append(row.id)

            # A device is polled over the span of its own missing stamps —
            # unpublished/future hours, holes and 404 devices come up missing
            # every request by construction, while a device with no cached
            # data at all must not force the whole branch to re-download the
            # full range.
            hole_cutoff = now - HOLE_RETRY_WINDOW

            def _compute_missing(candidates: List[Dict]):
                polls: List[Dict] = []
                spans: Dict[tuple, tuple] = {}
                days: Dict[tuple, set] = {}
                for device in candidates:
                    key4 = _device_key(device)
                    missing = []
                    for s in expected:
                        if s in cached[key4]:
                            continue
                        if (
                            _as_dt(s) < hole_cutoff
                            and (key4, _stamp_day(s)) in tried_days
                        ):
                            continue  # permanent hole: tried, still no data
                        missing.append(s)
                    if missing:
                        polls.append(device)
                        spans[key4] = (
                            _as_dt(min(missing)), _as_dt(max(missing))
                        )
                        days[key4] = {_stamp_day(s) for s in missing}
                return polls, spans, days

            poll_devices, device_spans, device_days = _compute_missing(devices)

            if poll_devices:
                # Parallel by default. Wait ONLY when this request is fully
                # contained (window and devices) in a poll already talking to
                # DPD — then its data lands in cache within seconds and
                # re-downloading it would just double the DPD load. Partially
                # overlapping / disjoint requests proceed concurrently; the
                # SQL-side payload merge keeps shared boundary rows safe.
                mode, lock_key = await _find_or_register_poll(
                    session, period_type,
                    _as_dt(window_from), _as_dt(window_to),
                    sorted(_device_hash(_device_key(d)) for d in poll_devices),
                )
                if mode == "wait":
                    if events_cb is not None:
                        events_cb({"type": "status", "phase": "waiting"})
                    # Shared lock: waiters don't serialize behind each other.
                    await session.execute(
                        text("SELECT pg_advisory_xact_lock_shared("
                             "hashtextextended(:k, 0))"),
                        {"k": lock_key},
                    )
                    # The containing poll finished — re-read what it stored.
                    for device in poll_devices:
                        key4 = _device_key(device)
                        cached[key4] = {}
                        tried_days.difference_update(
                            {t for t in tried_days if t[0] == key4}
                        )
                    for row in await dao.load_range(
                        poll_devices,
                        period_type,
                        day_from - timedelta(days=1),
                        day_to + timedelta(days=1),
                    ):
                        _absorb_row(row)
                    # Leftovers (holes the leader couldn't fill) are polled
                    # below without registering — they are tiny by now.
                    poll_devices, device_spans, device_days = _compute_missing(
                        poll_devices
                    )
                else:
                    poll_key_holder[0] = lock_key

            if events_cb is not None:
                events_cb({"type": "progress", "done": 0,
                           "total": len(poll_devices)})

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
                progress_cb = None
                if events_cb is not None:
                    progress_cb = lambda done, total: events_cb(
                        {"type": "progress", "done": done, "total": total}
                    )
                fresh_records = await client.get_volumes(
                    poll_devices,
                    poll_from,
                    poll_to,
                    type_request=period_type,
                    device_ranges=device_spans,
                    progress_cb=progress_cb,
                )
                poll_secs = perf_counter() - t_poll
                if events_cb is not None:
                    # Covers store + merge + the caller's aggregation.
                    events_cb({"type": "status", "phase": "aggregating"})

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
                # winning over cached. Days we asked about but got nothing
                # for still get a row (possibly empty) — the "tried" mark
                # that turns their holes permanent after HOLE_RETRY_WINDOW.
                upsert_rows = []
                for device in poll_devices:
                    key4 = _device_key(device)
                    by_day: Dict[date, Dict] = defaultdict(dict)
                    for stamp, rec in fresh.get(key4, {}).items():
                        if _has_data(rec):
                            by_day[_stamp_day(stamp)][stamp] = rec
                    for day in device_days.get(key4, ()):
                        by_day.setdefault(day, {})
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
                if events_cb is not None:
                    events_cb({"type": "status", "phase": "aggregating"})

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

        pressure_unit = record.get("pressUnit")
        if isinstance(pressure_unit, str):
            pressure_unit = pressure_unit.strip() or None

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
