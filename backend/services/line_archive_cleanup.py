"""Removing a stretch of one line's archive — all five of them at once —
or the same stretch for every line of a branch.

A line's data lives in five tables, filled by five engines from the same
hostlib files: the daily and hourly volumes, the operator's edits, the events
and alarms, and the settings. They are one archive as far as anybody using the
system is concerned, so they are cleared together: leaving the alarms of a
period whose hours are gone is not a state anybody asked for.

The range is open at either end on purpose. What this is for is a stretch that
was read wrongly — a hostlib that came from the wrong folder, a line that was
pointed at the wrong EIC code — and such a stretch is usually described as
"everything before we noticed" or "everything since the swap". So a start
alone means everything from that day on, an end alone everything up to it,
and no dates at all the whole archive.

Dates are COMMERCIAL days, inclusive at both ends. The daily row dated D holds
the gas that flowed from D 07:00 to D+1 07:00 (checked on prod: «ГРС-2»,
10.09.2026, daily 39 652 = the hours 10.09 07:00 … 11.09 06:00), so the
hourly, edit, alarm and parameter rows are taken over the same instants:
from the first day's 07:00 up to, not including, 07:00 after the last day.
Cut at calendar midnight instead, clearing day D would take the night hours
of D−1 and leave D's own night behind, and the daily row would no longer
match the hours under it.

What is cleared comes back only by reading the hostlib files again — the
update asked for by hand on that path, which ignores the log of what has been
read. Nothing re-reads them by itself.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Dict, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services import commercial_day

logger = logging.getLogger(__name__)

#: kind -> (table, period column, is the column a date rather than a moment).
#: The kinds are what the screen counts and names, in the order it shows them.
TABLES: Dict[str, tuple] = {
    "daily": ("daily_archive", "period", True),
    "hourly": ("hourly_archive", "period", False),
    "edits": ("edit_archive", "period", False),
    "alarms": ("sys_archive", "period", False),
    "params": ("params", "period", False),
}


def _window(since: Optional[date], until: Optional[date], by_date: bool) -> tuple:
    """The SQL condition and its values for one table — gas days, see above."""
    hour = commercial_day.contract_hour()
    where, values = [], {}
    if since is not None:
        where.append("period >= :since")
        values["since"] = since if by_date else commercial_day.day_bounds(since, hour)[0]
    if until is not None:
        if by_date:
            where.append("period <= :until")
            values["until"] = until
        else:
            where.append("period < :until")
            values["until"] = commercial_day.day_bounds(until, hour)[1]
    return " AND ".join(where), values


def valid(since: Optional[date], until: Optional[date]) -> Optional[str]:
    """Why this range cannot be used, or None when it can. No dates at all is
    a range too: the whole archive."""
    if since is not None and until is not None and since > until:
        return "Початкова дата пізніша за кінцеву"
    return None


#: Which lines a call is about: one line, every line of a branch (line →
#: calc → LUMG → branch), or every line there is. The bulk forms are what the
#: lines screen clears under its branch filter: a branch picked means its own
#: lines and never another branch's, «all branches» means all of them.
_LINE = "line_id = :scope"
_BRANCH = (
    "line_id IN (SELECT l.id FROM gas_volume_line l "
    "JOIN gas_volume_calc c ON c.id = l.gas_volume_calc_id "
    "JOIN lumg m ON m.id = c.lumg_id WHERE m.branch_id = :scope)"
)
_EVERY = "TRUE"


def _bulk(branch_id: Optional[int]) -> tuple:
    return (_BRANCH, branch_id) if branch_id is not None else (_EVERY, None)


async def _counts(session: AsyncSession, scope: str, scope_id: Optional[int],
                  since: Optional[date], until: Optional[date]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for kind, (table, _column, by_date) in TABLES.items():
        where, values = _window(since, until, by_date)
        clause = f" AND {where}" if where else ""
        out[kind] = (await session.execute(
            text(f"SELECT count(*) FROM {table} WHERE {scope}{clause}"),
            {"scope": scope_id, **values},
        )).scalar() or 0
    return out


async def _purge(session: AsyncSession, scope: str, scope_id: Optional[int],
                 since: Optional[date], until: Optional[date]) -> Dict[str, int]:
    removed: Dict[str, int] = {}
    for kind, (table, _column, by_date) in TABLES.items():
        where, values = _window(since, until, by_date)
        clause = f" AND {where}" if where else ""
        result = await session.execute(
            text(f"DELETE FROM {table} WHERE {scope}{clause}"),
            {"scope": scope_id, **values},
        )
        removed[kind] = result.rowcount or 0
    return removed


async def _extent(session: AsyncSession, scope: str,
                  scope_id: Optional[int]) -> Dict[str, Optional[str]]:
    first: Optional[date] = None
    last: Optional[date] = None
    for table, column, _by_date in TABLES.values():
        row = (await session.execute(
            text(f"SELECT min({column}), max({column}) FROM {table} "
                 f"WHERE {scope}"),
            {"scope": scope_id},
        )).first()
        for value, keep_smaller in ((row[0], True), (row[1], False)):
            if value is None:
                continue
            day = (commercial_day.day_of(value, commercial_day.contract_hour())
                   if isinstance(value, datetime) else value)
            if keep_smaller:
                first = day if first is None or day < first else first
            else:
                last = day if last is None or day > last else last
    return {"first": first.isoformat() if first else None,
            "last": last.isoformat() if last else None}


async def counts(session: AsyncSession, line_id: int,
                 since: Optional[date], until: Optional[date]) -> Dict[str, int]:
    """How many rows of each kind the range holds. Counts only, nothing is
    touched — the screen shows these before the button that removes them."""
    return await _counts(session, _LINE, line_id, since, until)


async def purge(session: AsyncSession, line_id: int,
                since: Optional[date], until: Optional[date]) -> Dict[str, int]:
    """Remove those rows, returning how many went of each kind."""
    removed = await _purge(session, _LINE, line_id, since, until)
    logger.warning("Очищено архів лінії %s за %s..%s: %s", line_id,
                   since or "початку", until or "кінця", removed)
    return removed


async def extent(session: AsyncSession, line_id: int) -> Dict[str, Optional[str]]:
    """The first and last day this line has anything on, across all five
    tables — what the screen shows so that dates are picked against something
    real instead of against a guess."""
    return await _extent(session, _LINE, line_id)


async def bulk_line_count(session: AsyncSession, branch_id: Optional[int]) -> int:
    """How many lines a bulk clear would reach: the branch's, or all."""
    if branch_id is None:
        return (await session.execute(
            text("SELECT count(*) FROM gas_volume_line"))).scalar() or 0
    return (await session.execute(
        text("SELECT count(*) FROM gas_volume_line l "
             "JOIN gas_volume_calc c ON c.id = l.gas_volume_calc_id "
             "JOIN lumg m ON m.id = c.lumg_id WHERE m.branch_id = :b"),
        {"b": branch_id},
    )).scalar() or 0


async def bulk_counts(session: AsyncSession, branch_id: Optional[int],
                      since: Optional[date], until: Optional[date]) -> Dict[str, int]:
    """`counts`, summed over every line of the branch — or of all branches."""
    return await _counts(session, *_bulk(branch_id), since, until)


async def bulk_purge(session: AsyncSession, branch_id: Optional[int],
                     since: Optional[date], until: Optional[date]) -> Dict[str, int]:
    """`purge` for every line of the branch (or of all branches) at once, in
    one transaction."""
    removed = await _purge(session, *_bulk(branch_id), since, until)
    logger.warning("Очищено архіви всіх ліній %s за %s..%s: %s",
                   f"філії {branch_id}" if branch_id is not None else "усіх філій",
                   since or "початку", until or "кінця", removed)
    return removed


async def bulk_extent(session: AsyncSession,
                      branch_id: Optional[int]) -> Dict[str, Optional[str]]:
    """`extent` across every line of the branch — or of all branches."""
    return await _extent(session, *_bulk(branch_id))
