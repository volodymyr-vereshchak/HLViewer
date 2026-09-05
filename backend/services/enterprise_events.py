"""Alarms and interventions of enterprise devices, read live from the DPD API.

Nothing here is stored. The report answers "what went wrong in this window"
and is rebuilt on every run, so there is no archive to keep in step and no
migration behind it — the cost is one poll per opening, which the progress
stream makes visible.

The shape of DPD's answer drives everything below, so it is worth stating:

  * The list endpoint returns DEVICES that had events, not events. It is the
    cheap half of the pair and the reason a full report costs ~130 requests
    instead of ~2000 — see `collect`.
  * A single continuous alarm arrives SLICED into buckets, hourly for some
    vendors and daily (07:00 boundary) for others. One week of one alarm is
    175 rows. Counting rows would therefore count buckets, not alarms.
  * `duration` is NOT the bucket length: it is how long the alarm was actually
    up inside that bucket (a daily bucket can carry 960 seconds). Summing it
    across buckets is exact, which is why this module sums rather than merges
    intervals.
  * `retry` is how many times the alarm was RAISED inside the bucket, already
    counted by DPD. It is 0 while an alarm merely continues from the previous
    bucket, so a continuous alarm sums to zero appearances — see `_series`.
  * Some rows carry `end` = 2000-01-01, a sentinel that makes `duration` about
    593 days. Twelve such rows in a week-long sample outweighed every real row
    by 200x, so they are dropped rather than clamped.
"""

import asyncio
import json
import logging
from collections import defaultdict
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Callable, Dict, List, Optional

from backend.services import device_history
from backend.services.dpd_client import DPDClient
from backend.services.enterprise_mappings import get_devices_for_branch_db

logger = logging.getLogger(__name__)

# Rows whose end is this are DPD's "no end recorded" sentinel, not a date.
GARBAGE_END_PREFIX = "2000-01-01"

NAMES_PATH = Path(__file__).resolve().parent.parent / "data" / "dpd_event_names.json"

# How many device detail requests may be in flight. The client's own pool caps
# concurrency too; this keeps the report from monopolising it.
MAX_CONCURRENCY = 8

DPD_STAMP = "%Y-%m-%d %H:%M:%S"


@lru_cache(maxsize=1)
def event_names() -> Dict[str, str]:
    """Our copy of the event dictionary, keyed by DPD's own i18n key.

    DPD returns the key (`industry.acc.radmirtech.2`) and keeps the text in the
    frontend bundle it ships, so holding our own copy is what lets the wording
    be corrected without waiting on them. Keys DPD adds later simply fall
    through untranslated rather than breaking the report.
    """
    try:
        with NAMES_PATH.open(encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        logger.exception("Cannot read %s; events will show raw keys", NAMES_PATH)
        return {}


def _within(start: Optional[str], span) -> bool:
    """Does a DPD row fall inside an installation window?

    An alarm is attributed by when it STARTED: a row that began before the
    corrector was installed here belongs to its previous owner, however far
    into this window it later ran.
    """
    if not start:
        return False
    try:
        stamp = datetime.strptime(start, DPD_STAMP)
    except ValueError:
        return False
    return span[0] <= stamp <= span[1]


def _series(rows: List[dict]) -> Optional[dict]:
    """Collapse one device's rows for ONE event type into a single record.

    Appearances fall back to 1 when DPD reports none: `retry` counts alarms
    raised inside a bucket, and a continuous alarm is never re-raised, so a
    week of unbroken failure sums to zero. Reporting "0 appearances, 7 days"
    would read as no alarm at all.
    """
    total = 0
    appearances = 0
    first = None
    last = None
    dropped = 0
    for r in rows:
        end = r.get("end") or ""
        if end.startswith(GARBAGE_END_PREFIX):
            dropped += 1
            continue
        start = r.get("start") or ""
        total += r.get("duration") or 0
        appearances += r.get("retry") or 0
        if start and (first is None or start < first):
            first = start
        # An open row (no end) is bounded by its start: the alarm is known to
        # have existed then, and inventing start+duration would put the last
        # seen time in the future for a still-running alarm.
        bound = end or start
        if bound and (last is None or bound > last):
            last = bound
    if first is None:
        return None
    if appearances == 0 and total > 0:
        appearances = 1
    return {
        "first": first,
        "last": last,
        "duration": total,
        "appearances": appearances,
        "dropped": dropped,
    }


def aggregate(per_device: List[dict]) -> List[dict]:
    """Group per-device series into one row per event type, newest pain first.

    Each group carries its objects so the UI can expand a type into the
    enterprises it happened at without a second request.
    """
    groups: Dict[str, dict] = {}
    for entry in per_device:
        g = groups.setdefault(entry["type"], {
            "type": entry["type"],
            "code": entry.get("code"),
            "name": event_names().get(entry["type"], entry["type"]),
            "translated": entry["type"] in event_names(),
            "first": entry["first"],
            "last": entry["last"],
            "duration": 0,
            "appearances": 0,
            "objects": [],
        })
        g["duration"] += entry["duration"]
        g["appearances"] += entry["appearances"]
        if entry["first"] < g["first"]:
            g["first"] = entry["first"]
        if entry["last"] and (not g["last"] or entry["last"] > g["last"]):
            g["last"] = entry["last"]
        g["objects"].append(entry)

    out = list(groups.values())
    for g in out:
        g["devices"] = len(g["objects"])
        g["objects"].sort(key=lambda o: -o["duration"])
    # Longest total outage first: that is the question the report is opened
    # with. Appearances break ties so a frequent short alarm still surfaces.
    out.sort(key=lambda g: (-g["duration"], -g["appearances"]))
    return out


async def collect(
    branch_id: int,
    date_from,
    date_to,
    session,
    *,
    kind: str = "accidents",
    events_cb: Optional[Callable[[dict], None]] = None,
) -> dict:
    """Poll one branch's enterprise devices for alarms (or interventions).

    Only devices that are BOTH ours and present in DPD's "who had events" list
    are asked for details. On live data that is ~130 of ~2050 devices, so the
    intersection is what makes the report affordable rather than an N+1 sweep.
    """
    def emit(event: dict) -> None:
        if events_cb is not None:
            events_cb(event)

    # DPD's `to` covers the whole day, so the request window must too:
    # a corrector installed at 10:00 on the last day would otherwise fall
    # outside the range and be dropped from the report entirely.
    range_to = date_to.replace(hour=23, minute=59, second=59)
    assignments = await get_devices_for_branch_db(
        branch_id, session, range_from=date_from, range_to=range_to,
    )
    # One corrector can serve two metering points, so the same quadruple may
    # appear twice; poll it once and report both objects.
    ours: Dict[tuple, List[dict]] = defaultdict(list)
    for a in assignments:
        ours[(a["serNum"], a["mfDev"], a["typeDev"], a["chNum"])].append(a)

    client = await DPDClient.for_branch(branch_id, session)
    try:
        emit({"type": "status", "phase": "list"})
        devices = await client.get_event_devices(
            kind, date_from, date_to,
            page_cb=lambda done, total: emit(
                {"type": "progress", "phase": "list", "done": done, "total": total}
            ),
        )

        matched = []
        for row in devices:
            try:
                key = (int(row["serNum"]), int(row["mfDev"]),
                       int(row["typeDev"]), int(row["chNum"]))
            except (KeyError, TypeError, ValueError):
                continue
            if key in ours:
                matched.append((row, ours[key]))

        emit({
            "type": "status", "phase": "polling",
            "candidates": len(devices), "matched": len(matched),
            "ours": len(assignments),
        })

        per_device: List[dict] = []
        dropped = 0
        done = 0
        sem = asyncio.Semaphore(MAX_CONCURRENCY)

        async def one(row, owners):
            nonlocal done, dropped
            async with sem:
                try:
                    rows = await client.get_device_events(
                        kind, row["id"], date_from, date_to,
                    )
                except Exception as e:
                    # A single unreachable device must not lose the whole
                    # report; it is reported as an error event and skipped.
                    logger.warning("DPD %s failed for device %s: %s", kind, row.get("id"), e)
                    emit({"type": "device_error", "serNum": row.get("serNum"), "detail": str(e)})
                    rows = []
                # The device is fetched ONCE over the whole range, then each
                # owner keeps only the part of it that fell inside its own
                # installation window. A corrector replaced mid-period would
                # otherwise lend the enterprise alarms from the days it was
                # somewhere else — and its replacement would do the same in
                # the other direction.
                for owner in owners:
                    span = device_history.clip(
                        owner["win_from"], owner["win_to"], date_from, range_to,
                    )
                    if span is None:
                        continue
                    mine = [r for r in rows if _within(r.get("start"), span)]
                    by_type: Dict[str, List[dict]] = defaultdict(list)
                    for r in mine:
                        by_type[r.get("type") or "?"].append(r)
                    for etype, group in by_type.items():
                        s = _series(group)
                        if s is None:
                            continue
                        dropped += s.pop("dropped")
                        per_device.append({
                            **s,
                            "type": etype,
                            "code": group[0].get("code"),
                            "enterprise_id": owner["enterprise_id"],
                            "enterprise_name": owner["enterprise_name"],
                            "line_id": owner["line_id"],
                            "serNum": owner["serNum"],
                            "chNum": owner["chNum"],
                        })
                done += 1
                emit({"type": "progress", "phase": "polling",
                      "done": done, "total": len(matched)})

        await asyncio.gather(*(one(row, owners) for row, owners in matched))

        groups = aggregate(per_device)
        return {
            "kind": kind,
            "groups": groups,
            "stats": {
                "ours": len(assignments),
                "candidates": len(devices),
                "matched": len(matched),
                "with_events": len({(e["serNum"], e["chNum"]) for e in per_device}),
                "dropped_rows": dropped,
                "untranslated": sorted({g["type"] for g in groups if not g["translated"]}),
            },
        }
    finally:
        await client.close()
