"""Turning ФХП change events into comparable hourly and daily series.

`edit_archive` records moments, not periods: "at 07:25 the density went from
0.7467 to 0.7469". A value therefore holds until the next change — the same
thing the computer itself does — which makes the history a step function.

Everything here is pure: no database, no settings lookups, no clock. That is
deliberate, because the arithmetic is the whole feature and it has to be
testable against numbers worked out by hand.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, Literal, Mapping, Optional, Sequence, TypeVar

from backend.services import commercial_day

# (in force from, value)
Step = tuple[datetime, float]

# Series are keyed either by hour-start datetime or by commercial date.
K = TypeVar("K")

_HOUR = timedelta(hours=1)


def seed_value(
    prior_new_value: Optional[float], first_inside_old_value: Optional[float]
) -> Optional[float]:
    """The value in force when the range opens.

    The last change strictly before the range wins. Failing that, the first
    change INSIDE the range tells us for free what it replaced — its
    `old_value` is exactly the value that had been holding. Only when there is
    neither is the series undefined.
    """
    if prior_new_value is not None:
        return prior_new_value
    return first_inside_old_value


def build_steps(
    rows: Sequence[tuple[datetime, float]],
    seed: Optional[float],
    range_from: datetime,
    seed_at: Optional[datetime] = None,
) -> list[Step]:
    """The step function covering the range.

    `rows` must already be ordered by (period, id): where two rows share an
    instant — which `edit_all_constraint` permits, as it includes the values —
    the last one wins, because it is the one the device settled on.

    `seed_at` is the instant the seed value was actually set, which may be long
    before the range. Keeping it (rather than pinning the seed to `range_from`)
    costs nothing in the hourly walk and is what lets `staleness` see that a
    line has been silent since well before the report started.

    A row that would start before the previous accepted step is dropped. Naive
    stamps go backwards during the autumn DST switch, and a lost change is a
    smaller lie than a negative weight.
    """
    steps: list[Step] = []
    if seed is not None:
        steps.append((min(seed_at or range_from, range_from), seed))

    for stamp, value in rows:
        if stamp < range_from:
            continue
        if steps and stamp < steps[-1][0]:
            continue
        if steps and stamp == steps[-1][0]:
            steps[-1] = (stamp, value)  # same instant: the later row wins
            continue
        steps.append((stamp, value))
    return steps


def hourly_series(
    steps: Sequence[Step], range_from: datetime, range_to: datetime
) -> dict[datetime, float]:
    """Time-weighted mean per clock hour over [range_from, range_to).

    The weight is the number of SECONDS a value was in force inside the hour,
    which is what "changes arrive in the middle of an hour" demands. Hours not
    covered by any step — everything before the seed — are absent from the
    result, not zero and not None. An hour covered only in part (the series
    starts mid-hour because there was no seed) is reported as the mean over the
    part that is known.

    One merge walk over hours and steps: O(hours + changes).
    """
    if not steps:
        return {}

    out: dict[datetime, float] = {}
    hour = _floor_hour(max(range_from, steps[0][0]))
    idx = 0

    while hour < range_to:
        hour_end = min(hour + _HOUR, range_to)
        # The step in force at the start of this hour.
        while idx + 1 < len(steps) and steps[idx + 1][0] <= hour:
            idx += 1

        weighted = 0.0
        seconds = 0
        cursor = max(hour, steps[idx][0])
        walk = idx
        while cursor < hour_end:
            nxt = steps[walk + 1][0] if walk + 1 < len(steps) else None
            segment_end = hour_end if nxt is None or nxt >= hour_end else nxt
            span = int((segment_end - cursor).total_seconds())
            if span > 0:
                weighted += steps[walk][1] * span
                seconds += span
            cursor = segment_end
            if nxt is not None and nxt <= cursor:
                walk += 1
            elif nxt is None:
                break

        if seconds > 0:
            out[hour] = weighted / seconds
        hour += _HOUR

    return out


def daily_series(
    hourly: Mapping[datetime, float],
    days: Sequence[date],
    hour: int,
) -> dict[date, tuple[float, int]]:
    """day → (mean of its hourly values, how many hours were present).

    A PLAIN mean of the hourly means, as specified — not a second time
    weighting of the whole day. The two differ whenever the hours are
    asymmetric, and the test says so explicitly.

    Dividing by the hours actually present is what makes a 23-hour DST day and
    a day whose first hours precede the line's first reading readable rather
    than wrong; `hours_present` lets the UI mark them.
    """
    out: dict[date, tuple[float, int]] = {}
    for day in days:
        values = [
            hourly[h] for h in commercial_day.hours_of_day(day, hour) if h in hourly
        ]
        if values:
            out[day] = (sum(values) / len(values), len(values))
    return out


def reference_series(
    per_line: Mapping[int, Mapping[K, float]], ref_line_ids: Sequence[int]
) -> tuple[dict[K, float], dict[K, int]]:
    """Per period: the mean over the reference lines that HAVE a value there,
    and how many of them backed it.

    The count is part of the answer, not diagnostics: when one of two
    chromatographs falls silent the reference jumps, and a report that hid
    that would blame the jump on the compared line.
    """
    totals: dict[K, float] = {}
    counts: dict[K, int] = {}
    for line_id in ref_line_ids:
        for period, value in per_line.get(line_id, {}).items():
            totals[period] = totals.get(period, 0.0) + value
            counts[period] = counts.get(period, 0) + 1
    return {p: totals[p] / counts[p] for p in totals}, counts


@dataclass(frozen=True)
class Deviation:
    period: object
    value: float
    reference: float
    delta: float
    delta_pct: Optional[float]


def deviations(
    line: Mapping[K, float], ref: Mapping[K, float]
) -> list[Deviation]:
    """One entry per period where BOTH the line and the reference have a value,
    in period order."""
    out: list[Deviation] = []
    for period in sorted(set(line) & set(ref)):  # type: ignore[type-var]
        value = line[period]
        reference = ref[period]
        delta = value - reference
        pct = None if abs(reference) < 1e-9 else delta / reference * 100.0
        out.append(Deviation(period, value, reference, delta, pct))
    return out


@dataclass(frozen=True)
class LineStats:
    n: int
    mean_delta: float
    mean_abs_delta: float
    max_abs_delta: float
    max_abs_delta_at: object
    mean_abs_delta_pct: Optional[float]
    max_abs_delta_pct: Optional[float]
    max_abs_delta_pct_at: object
    out_of_tolerance: int
    out_of_tolerance_share: float


def line_stats(
    devs: Sequence[Deviation],
    tolerance: float,
    mode: Literal["abs", "pct"] = "abs",
) -> Optional[LineStats]:
    if not devs:
        return None

    n = len(devs)
    worst = max(devs, key=lambda d: abs(d.delta))
    pcts = [d for d in devs if d.delta_pct is not None]
    worst_pct = max(pcts, key=lambda d: abs(d.delta_pct)) if pcts else None

    breaches = sum(1 for d in devs if _breaches(d, tolerance, mode))

    return LineStats(
        n=n,
        mean_delta=sum(d.delta for d in devs) / n,
        mean_abs_delta=sum(abs(d.delta) for d in devs) / n,
        max_abs_delta=abs(worst.delta),
        max_abs_delta_at=worst.period,
        mean_abs_delta_pct=(
            sum(abs(d.delta_pct) for d in pcts) / len(pcts) if pcts else None
        ),
        max_abs_delta_pct=abs(worst_pct.delta_pct) if worst_pct else None,
        max_abs_delta_pct_at=worst_pct.period if worst_pct else None,
        out_of_tolerance=breaches,
        out_of_tolerance_share=breaches / n * 100.0,
    )


def _breaches(dev: Deviation, tolerance: float, mode: str) -> bool:
    if mode == "pct":
        return dev.delta_pct is not None and abs(dev.delta_pct) > tolerance
    return abs(dev.delta) > tolerance


def spread_series(
    per_line: Mapping[int, Mapping[K, float]]
) -> dict[K, tuple[float, float, float, int]]:
    """period → (min, max, max - min, how many lines had a value).

    This is what a route with no chromatograph shows: nothing is the reference,
    but the lines still have to agree with each other.
    """
    buckets: dict[K, list[float]] = {}
    for series in per_line.values():
        for period, value in series.items():
            buckets.setdefault(period, []).append(value)
    return {
        period: (min(vs), max(vs), max(vs) - min(vs), len(vs))
        for period, vs in buckets.items()
    }


def staleness(
    steps: Sequence[Step], periods: Iterable[datetime], max_age_hours: int
) -> set[datetime]:
    """The periods whose value in force is older than `max_age_hours`.

    A step function never expires by itself, so a chromatograph that died on
    the 10th keeps "reporting" until the end of the range. We keep holding the
    value — that is genuinely the last thing the device said, and a manually
    entered line legitimately holds for days — but the report has to show that
    it is old.
    """
    if not steps:
        return set()
    limit = timedelta(hours=max_age_hours)
    stale: set[datetime] = set()
    idx = 0
    for period in sorted(periods):
        while idx + 1 < len(steps) and steps[idx + 1][0] <= period:
            idx += 1
        if steps[idx][0] <= period and period - steps[idx][0] > limit:
            stale.add(period)
    return stale


def _floor_hour(stamp: datetime) -> datetime:
    return stamp.replace(minute=0, second=0, microsecond=0)
