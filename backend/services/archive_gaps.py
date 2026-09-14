"""What is missing from a corrector's archive, and what can still be fetched.

Only the modem uses this. DPD is fetched by a plain window — from where the
archive ends to tomorrow — because its API is metered per call and answers by
range: chasing individual holes there would be dozens of paid calls for hours
that one range covers anyway. A corrector on the end of a phone line is the
opposite: one request per record either way, so reading exactly the missing
records is both cheaper and the only way a hole in the middle ever gets
filled.

A poll that re-reads everything is slow and, over a phone line, expensive: one
hour is one request to a ВЕГА, so a month is seven hundred of them. A poll
that reads only "everything after the newest row we hold" is fast but leaves
holes forever — and holes happen: the DPD API is fetched over somebody else's
internet, and a failed afternoon leaves five hours missing in the middle of a
month that otherwise looks complete.

So the question is not "where did we stop" but "what is missing", and it is
asked over a window the source can actually answer for. A corrector's ring
holds sixty-four days; a gap older than that is gone from the meter as well,
and saying so is more use than quietly not reading it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import List, Sequence

HOUR = timedelta(hours=1)


@dataclass(frozen=True)
class Window:
    """A range the source can answer for, inclusive at both ends."""

    start: datetime
    end: datetime

    def hours(self) -> int:
        if self.end < self.start:
            return 0
        return int((self.end - self.start).total_seconds() // 3600) + 1


@dataclass
class Missing:
    """What a poll should ask for, and what it cannot get any more."""

    #: Hours absent from the archive and still inside the source's window.
    hours: List[datetime]
    #: Days absent from the archive and still inside the source's window.
    days: List[date]
    #: Hours we know we are missing but the source no longer holds. Reported
    #: rather than silently dropped: a hole nobody can fill is a fact about
    #: the data, and an operator deciding whether to trust a monthly total
    #: needs it.
    unreachable_hours: int = 0

    @property
    def total(self) -> int:
        return len(self.hours) + len(self.days)


def missing_hours(window: Window, present: Sequence[datetime]) -> List[datetime]:
    """Hours inside the window with nothing stored for them.

    Both ends inclusive, on the hour. The stored stamps are floored to the
    hour first: a reading filed at 09:00:32 is the nine o'clock hour, and
    comparing exact moments would report every hour as missing.
    """
    if window.end < window.start:
        return []
    have = {stamp.replace(minute=0, second=0, microsecond=0) for stamp in present}
    start = window.start.replace(minute=0, second=0, microsecond=0)
    end = window.end.replace(minute=0, second=0, microsecond=0)

    gaps: List[datetime] = []
    at = start
    while at <= end:
        if at not in have:
            gaps.append(at)
        at += HOUR
    return gaps


def missing_days(window: Window, present: Sequence[date]) -> List[date]:
    """Days inside the window with nothing stored for them.

    A day is counted only when the whole of it lies in the window: the day a
    window opens halfway through was never going to be complete, and asking
    for it every poll would make a gap that never closes.
    """
    have = set(present)
    first = window.start.date()
    if window.start.time() != datetime.min.time():
        first = first + timedelta(days=1)
    last = window.end.date() - timedelta(days=1)

    gaps: List[date] = []
    at = first
    while at <= last:
        if at not in have:
            gaps.append(at)
        at += timedelta(days=1)
    return gaps


def as_ranges(moments: Sequence[datetime], step: timedelta = HOUR) -> List[Window]:
    """Consecutive moments folded into ranges.

    Thirty separate holes an hour apart are one range, not thirty requests —
    which matters for the DPD API, where a request is a request whether it
    covers an hour or a week.
    """
    if not moments:
        return []
    ordered = sorted(moments)
    ranges = [Window(ordered[0], ordered[0])]
    for moment in ordered[1:]:
        if moment - ranges[-1].end == step:
            ranges[-1] = Window(ranges[-1].start, moment)
        else:
            ranges.append(Window(moment, moment))
    return ranges


@dataclass(frozen=True)
class DpdWindow:
    """The range a DPD poll asks the API for, and why it starts there."""

    start: date
    end: date
    #: What decided the start: "archive" — where the stored data ends;
    #: "installed" — nothing stored, so from the day the corrector went in.
    reason: str


def dpd_window(
    newest_stored: datetime | date | None,
    installed_from: datetime | date | None,
    today: date,
) -> DpdWindow:
    """From where the archive ends to tomorrow.

    Tomorrow, not today, because the day here is a gas day: it starts at the
    contract hour, so the hours of the current one are filed under a date that
    has not arrived yet. Ending at today would leave them behind on every poll
    and they would only appear the following morning.

    With nothing stored, from the day the corrector was installed — there is no
    earlier data to ask for, and asking anyway is a paid call per range that
    can only come back empty.
    """
    end = today + timedelta(days=1)
    if newest_stored is not None:
        start = newest_stored.date() if isinstance(newest_stored, datetime) else newest_stored
        return DpdWindow(start=start, end=end, reason="archive")

    if installed_from is not None:
        start = (
            installed_from.date() if isinstance(installed_from, datetime)
            else installed_from
        )
        return DpdWindow(start=start, end=end, reason="installed")

    # Neither stored data nor an installation date: one day, so the poll is a
    # question rather than a month of paid calls into the dark.
    return DpdWindow(start=today, end=end, reason="unknown")
