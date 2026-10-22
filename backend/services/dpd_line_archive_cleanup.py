"""Removing a stretch of a DPD line's archive — one line, or every DPD line
of a branch at once.

A DPD line keeps two archives, daily and hourly, both filled from the DPD API
through the line's device history. They are cleared together over the same
range, for the same reason a hostlib line's five archives are: half of a
period is not a state anybody asks for.

Dates are COMMERCIAL days, as everywhere DPD data is concerned (see
archive_cleanup): day D runs from D 07:00 to D+1 07:00. Hourly rows go from
the first day's 07:00 up to, not including, 07:00 after the last day; daily
rows are taken by their own date, both ends included. Either end may be left
open, and with no dates at all the whole archive goes.

What comes back afterwards: the scheduled update polls each line from its
newest stored period onwards, so a range cleared at the end of the archive is
read again on the next run, and anything cleared before the newest period
stays gone until the line is initialized again.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services import commercial_day
from backend.services.dpd_line_refresh import STALE_SECONDS

logger = logging.getLogger(__name__)

#: kind -> (table, period column, is the column a date rather than a moment).
TABLES: Dict[str, tuple] = {
    "daily": ("dpd_line_daily_archive", "day", True),
    "hourly": ("dpd_line_hourly_archive", "stamp", False),
}

_LINE = "dpd_line_id = :scope"
_BRANCH = "dpd_line_id IN (SELECT id FROM dpd_line WHERE branch_id = :scope)"
_EVERY = "TRUE"


def _bulk(branch_id: Optional[int]) -> tuple:
    """A branch picked means its DPD lines only; none means every DPD line."""
    return (_BRANCH, branch_id) if branch_id is not None else (_EVERY, None)


def valid(since: Optional[date], until: Optional[date]) -> Optional[str]:
    """Why this range cannot be used, or None when it can. No dates at all is
    the whole archive."""
    if since is not None and until is not None and since > until:
        return "Початкова дата пізніша за кінцеву"
    return None


def _window(since: Optional[date], until: Optional[date],
            column: str, by_date: bool) -> tuple:
    hour = commercial_day.contract_hour()
    where, values = [], {}
    if since is not None:
        where.append(f"{column} >= :since")
        values["since"] = since if by_date else commercial_day.day_bounds(since, hour)[0]
    if until is not None:
        if by_date:
            where.append(f"{column} <= :until")
            values["until"] = until
        else:
            where.append(f"{column} < :until")
            values["until"] = commercial_day.day_bounds(until, hour)[1]
    return " AND ".join(where), values


async def _counts(session, scope, scope_id, since, until) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for kind, (table, column, by_date) in TABLES.items():
        where, values = _window(since, until, column, by_date)
        clause = f" AND {where}" if where else ""
        out[kind] = (await session.execute(
            text(f"SELECT count(*) FROM {table} WHERE {scope}{clause}"),
            {"scope": scope_id, **values},
        )).scalar() or 0
    return out


async def _purge(session, scope, scope_id, since, until) -> Dict[str, int]:
    removed: Dict[str, int] = {}
    for kind, (table, column, by_date) in TABLES.items():
        where, values = _window(since, until, column, by_date)
        clause = f" AND {where}" if where else ""
        result = await session.execute(
            text(f"DELETE FROM {table} WHERE {scope}{clause}"),
            {"scope": scope_id, **values},
        )
        removed[kind] = result.rowcount or 0
    return removed


async def _extent(session, scope, scope_id) -> Dict[str, Optional[str]]:
    """First and last commercial day anything is stored on."""
    hour = commercial_day.contract_hour()
    first: Optional[date] = None
    last: Optional[date] = None
    for table, column, by_date in TABLES.values():
        row = (await session.execute(
            text(f"SELECT min({column}), max({column}) FROM {table} "
                 f"WHERE {scope}"),
            {"scope": scope_id},
        )).first()
        for value, keep_smaller in ((row[0], True), (row[1], False)):
            if value is None:
                continue
            day = commercial_day.day_of(value, hour) if isinstance(value, datetime) else value
            if keep_smaller:
                first = day if first is None or day < first else first
            else:
                last = day if last is None or day > last else last
    return {"first": first.isoformat() if first else None,
            "last": last.isoformat() if last else None}


async def busy(session: AsyncSession, scope: str, scope_id: Optional[int]) -> List[str]:
    """Names of the lines whose initialization or update is running now.

    Clearing under a running job would race it: the job writes what it
    fetched after the delete and the range comes straight back, or half of it
    does. A heartbeat older than the job's stale window is a dead process, not
    a running one.
    """
    rows = (await session.execute(
        text(
            f"SELECT l.name FROM dpd_line_job j JOIN dpd_line l ON l.id = j.dpd_line_id "
            f"WHERE j.status = 'running' "
            f"AND j.updated_at >= now() - make_interval(secs => :stale) "
            f"AND {scope.replace('dpd_line_id', 'j.dpd_line_id')} ORDER BY l.name"
        ),
        {"scope": scope_id, "stale": STALE_SECONDS},
    )).scalars().all()
    return list(rows)


async def counts(session, line_id, since, until) -> Dict[str, int]:
    return await _counts(session, _LINE, line_id, since, until)


async def extent(session, line_id) -> Dict[str, Optional[str]]:
    return await _extent(session, _LINE, line_id)


async def purge(session, line_id, since, until) -> Dict[str, int]:
    removed = await _purge(session, _LINE, line_id, since, until)
    logger.warning("Очищено архів лінії ДПД %s за %s..%s: %s", line_id,
                   since or "початку", until or "кінця", removed)
    return removed


async def line_busy(session, line_id) -> List[str]:
    return await busy(session, _LINE, line_id)


async def bulk_line_count(session: AsyncSession, branch_id: Optional[int]) -> int:
    if branch_id is None:
        return (await session.execute(text("SELECT count(*) FROM dpd_line"))).scalar() or 0
    return (await session.execute(
        text("SELECT count(*) FROM dpd_line WHERE branch_id = :b"),
        {"b": branch_id},
    )).scalar() or 0


async def bulk_counts(session, branch_id, since, until) -> Dict[str, int]:
    return await _counts(session, *_bulk(branch_id), since, until)


async def bulk_extent(session, branch_id) -> Dict[str, Optional[str]]:
    return await _extent(session, *_bulk(branch_id))


async def bulk_purge(session, branch_id, since, until) -> Dict[str, int]:
    removed = await _purge(session, *_bulk(branch_id), since, until)
    logger.warning("Очищено архіви всіх ліній ДПД %s за %s..%s: %s",
                   f"філії {branch_id}" if branch_id is not None else "усіх філій",
                   since or "початку", until or "кінця", removed)
    return removed


async def bulk_busy(session, branch_id) -> List[str]:
    return await busy(session, *_bulk(branch_id))
