"""Device-history windows, shared by enterprises and DPD lines.

Both keep a corrector history and both have to answer the same question: at
the moment a record was measured, which device was in force? The answer is a
window per history entry, derived from the ordered entries and never stored —
so correcting a date can never leave stale attributions behind.

Enterprise entries may carry an explicit `removed_at`; DPD-line entries never
do. That is the whole difference: a corrector taken off a metering point on
the 5th and replaced on the 10th is, in between, already measuring somebody
else's gas, so those five days must belong to nobody. Chaining alone (a
device runs until the next is installed) cannot express that.
"""

from datetime import datetime, timedelta
from typing import Any, Optional, Sequence

from backend.settings import backend_settings

# (entry, win_from, win_to) — win_to is EXCLUSIVE, None means still in force.
Window = tuple[Any, datetime, Optional[datetime]]


def _installed_from(entry: Any) -> datetime:
    if isinstance(entry, dict):
        return entry["installed_from"]
    return entry.installed_from


def _removed_at(entry: Any) -> Optional[datetime]:
    if isinstance(entry, dict):
        return entry.get("removed_at")
    return getattr(entry, "removed_at", None)


def resolve_windows(entries: Sequence[Any]) -> list[Window]:
    """Ordered windows for one point's (or line's) history.

    `win_to` is the entry's own `removed_at` when set, otherwise the next
    entry's `installed_from`, otherwise None. A `removed_at` earlier than the
    next install leaves a gap that belongs to no device — that is deliberate,
    not an error to smooth over.

    Entries may be dicts or ORM objects; only `installed_from` and the
    optional `removed_at` are read.
    """
    ordered = sorted(entries, key=_installed_from)
    windows: list[Window] = []
    for i, entry in enumerate(ordered):
        next_from = _installed_from(ordered[i + 1]) if i + 1 < len(ordered) else None
        removed = _removed_at(entry)
        if removed is not None and (next_from is None or removed < next_from):
            win_to = removed
        else:
            win_to = next_from
        windows.append((entry, _installed_from(entry), win_to))
    return windows


def attribution_stamp(stamp: datetime, period_type: str) -> datetime:
    """The moment a record is attributed at for window filtering.

    Hourly records use their exact stamp. A daily record for date d covers the
    commercial day d CONTRACT_HOUR → d+1 CONTRACT_HOUR and is attributed to
    the device whose window covers the commercial-day start.
    """
    if period_type == "hourly":
        return stamp
    contract_hour = backend_settings.get("CONTRACT_HOUR", 7)
    return stamp + timedelta(hours=contract_hour)


def covers(win_from: datetime, win_to: Optional[datetime], at: datetime) -> bool:
    """Is `at` inside [win_from, win_to)?"""
    return at >= win_from and (win_to is None or at < win_to)


def clip(
    win_from: datetime,
    win_to: Optional[datetime],
    range_from: datetime,
    range_to: datetime,
) -> Optional[tuple[datetime, datetime]]:
    """Intersection of a window with an INCLUSIVE [range_from, range_to]
    request, or None when they do not overlap.

    `win_to` is exclusive, so a window ending exactly at range_from
    contributes nothing.
    """
    start = max(win_from, range_from)
    end = range_to if win_to is None else min(range_to, win_to - timedelta(seconds=1))
    if start > end:
        return None
    return start, end


class HistoryError(ValueError):
    """A history that cannot be true: two devices in force at the same time,
    or one device in force at two places."""


def _overlaps(
    from_a: datetime, to_a: Optional[datetime],
    from_b: datetime, to_b: Optional[datetime],
) -> bool:
    """Do two half-open windows share any moment?"""
    if to_a is not None and from_b >= to_a:
        return False
    if to_b is not None and from_a >= to_b:
        return False
    return True


def validate_point(entries: Sequence[Any]) -> None:
    """One point's history: no two devices in force at once.

    A metering point measures through one corrector at a time. Overlapping
    entries would double its volume for the overlap.
    """
    # Two entries at the same moment chain into a zero-length window, so the
    # overlap check below cannot see them — and the caller would get a raw
    # unique-violation from uq_enterprise_device_from instead of an answer.
    seen: set[datetime] = set()
    for entry in entries:
        stamp = _installed_from(entry)
        if stamp in seen:
            raise HistoryError(
                f"Два прилади не можуть мати однакову дату встановлення: "
                f"{stamp:%d.%m.%Y %H:%M}"
            )
        seen.add(stamp)

    windows = resolve_windows(entries)
    for i, (_, from_a, to_a) in enumerate(windows):
        for _, from_b, to_b in windows[i + 1:]:
            if _overlaps(from_a, to_a, from_b, to_b):
                raise HistoryError(
                    f"Періоди приладів перетинаються: з {from_a:%d.%m.%Y %H:%M} "
                    f"і з {from_b:%d.%m.%Y %H:%M}"
                )


def find_device_clashes(
    windows_by_point: dict[Any, list[Window]], device_of
) -> list[tuple[Any, Any, Any]]:
    """(device, point_a, point_b) for a device in force at two points at once.

    Windows must already be resolved WITHIN each point — chaining across
    points would be meaningless, since one point's next install says nothing
    about when the device left another.

    This is the guard that matters most for moved correctors: without it the
    same gas is counted at both points for the overlap.
    """
    placed: dict[Any, list[tuple[Any, datetime, Optional[datetime]]]] = {}
    for point, windows in windows_by_point.items():
        for entry, win_from, win_to in windows:
            placed.setdefault(device_of(entry), []).append((point, win_from, win_to))

    clashes: list[tuple[Any, Any, Any]] = []
    for device, spans in placed.items():
        for i, (point_a, from_a, to_a) in enumerate(spans):
            for point_b, from_b, to_b in spans[i + 1:]:
                if point_a != point_b and _overlaps(from_a, to_a, from_b, to_b):
                    clashes.append((device, point_a, point_b))
    return clashes
