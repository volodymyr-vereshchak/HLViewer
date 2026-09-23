"""Removing a stretch of one line's archive — all five of them at once.

A line's data lives in five tables, filled by five engines from the same
hostlib files: the daily and hourly volumes, the operator's edits, the events
and alarms, and the settings. They are one archive as far as anybody using the
system is concerned, so they are cleared together: leaving the alarms of a
period whose hours are gone is not a state anybody asked for.

The range is open at either end on purpose. What this is for is a stretch that
was read wrongly — a hostlib that came from the wrong folder, a line that was
pointed at the wrong EIC code — and such a stretch is usually described as
"everything before we noticed" or "everything since the swap". Naming both
ends is then the special case, not the rule, and a dialog that demands two
dates makes people type one they do not mean.

Dates are whole calendar days, inclusive at both ends: the day given as the
end is cleared with everything in it. The hourly, edit, alarm and parameter
rows are stamped to the minute, so they run to that day's last moment; the
daily rows are stamped by day and are taken by their own date.

What is cleared comes back only by reading the hostlib files again — the
update asked for by hand on that path, which ignores the log of what has been
read. Nothing re-reads them by itself.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Dict, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

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
    """The SQL condition and its values for one table.

    An end date means the whole of that day. For a table stamped to the minute
    that is "before the next midnight" rather than "at or before midnight",
    which would take the day's first moment only and leave the other 1439.
    """
    where, values = [], {}
    if since is not None:
        where.append("period >= :since")
        values["since"] = since if by_date else datetime.combine(since, time.min)
    if until is not None:
        if by_date:
            where.append("period <= :until")
            values["until"] = until
        else:
            where.append("period < :until")
            values["until"] = datetime.combine(until + timedelta(days=1), time.min)
    return " AND ".join(where), values


def valid(since: Optional[date], until: Optional[date]) -> Optional[str]:
    """Why this range cannot be used, or None when it can.

    Clearing a line's whole archive is not offered here: it is not a range,
    and everything this screen is for has an end or a beginning somebody can
    name. Asking for it by leaving both fields empty would be the easiest
    thing on the screen to do by accident.
    """
    if since is None and until is None:
        return "Вкажіть хоча б одну дату — з якої видаляти або до якої"
    if since is not None and until is not None and since > until:
        return "Початкова дата пізніша за кінцеву"
    return None


async def counts(session: AsyncSession, line_id: int,
                 since: Optional[date], until: Optional[date]) -> Dict[str, int]:
    """How many rows of each kind the range holds. Counts only, nothing is
    touched — the screen shows these before the button that removes them."""
    out: Dict[str, int] = {}
    for kind, (table, _column, by_date) in TABLES.items():
        where, values = _window(since, until, by_date)
        clause = f" AND {where}" if where else ""
        out[kind] = (await session.execute(
            text(f"SELECT count(*) FROM {table} WHERE line_id = :line{clause}"),
            {"line": line_id, **values},
        )).scalar() or 0
    return out


async def purge(session: AsyncSession, line_id: int,
                since: Optional[date], until: Optional[date]) -> Dict[str, int]:
    """Remove those rows, returning how many went of each kind."""
    removed: Dict[str, int] = {}
    for kind, (table, _column, by_date) in TABLES.items():
        where, values = _window(since, until, by_date)
        clause = f" AND {where}" if where else ""
        result = await session.execute(
            text(f"DELETE FROM {table} WHERE line_id = :line{clause}"),
            {"line": line_id, **values},
        )
        removed[kind] = result.rowcount or 0
    logger.warning("Очищено архів лінії %s за %s..%s: %s", line_id,
                   since or "початку", until or "кінця", removed)
    return removed


async def extent(session: AsyncSession, line_id: int) -> Dict[str, Optional[str]]:
    """The first and last day this line has anything on, across all five
    tables — what the screen shows so that dates are picked against something
    real instead of against a guess."""
    first: Optional[date] = None
    last: Optional[date] = None
    for table, column, _by_date in TABLES.values():
        row = (await session.execute(
            text(f"SELECT min({column}), max({column}) FROM {table} "
                 f"WHERE line_id = :line"),
            {"line": line_id},
        )).first()
        for value, keep_smaller in ((row[0], True), (row[1], False)):
            if value is None:
                continue
            day = value.date() if isinstance(value, datetime) else value
            if keep_smaller:
                first = day if first is None or day < first else first
            else:
                last = day if last is None or day > last else last
    return {"first": first.isoformat() if first else None,
            "last": last.isoformat() if last else None}
