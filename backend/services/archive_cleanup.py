"""Removing archive rows a metering point should never have had.

Rare, deliberate, and destructive — so it is counted before it is done, and
it is bounded twice over.

Bounded by the point's own history, first. The archive is keyed by corrector,
and a corrector moves: taken off one point and installed at another, its rows
belong to whoever it was measuring at the time. Deleting "the archive of this
enterprise" by device alone would take another enterprise's gas with it, so
every device is clipped to the window it actually stood there.

Bounded by dates, second, because the reason to do this at all is a stretch of
readings that is wrong — a unit read the wrong way round, a corrector polled
under somebody else's serial. The rest of the history is not in question.

What comes back afterwards is worth knowing before pressing anything. The
nightly refresh re-reads the last thirty days for every device, so rows
deleted inside that window return by themselves on the next run. Older ones
stay gone: a manual poll starts at the newest record it can see and works
forward, so nothing goes back for them unless somebody asks for that range.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services import commercial_day

logger = logging.getLogger(__name__)

#: The daily archive is stored by date, the hourly one by moment.
_TABLES = (("dpd_hourly_archive", "stamp"), ("dpd_daily_archive", "day"))

#: A range of dates means COMMERCIAL days, which do not start at midnight.
#:
#: Gas day D runs from D 07:00 to D+1 07:00, so clearing "the 5th to the 9th"
#: has to take the hours from the 5th at 07:00 up to — but not including — the
#: 10th at 07:00. Cleared by calendar midnight instead, every gas day would
#: lose its first seven hours and keep somebody else's last seven, and the
#: daily row for that day would no longer match the hours under it.


async def windows(session: AsyncSession, enterprise_id: int) -> List[Dict]:
    """Which corrector stood at this point, and when.

    `removed_at` of None is open-ended — it is still there.
    """
    rows = (await session.execute(
        text(
            """
            SELECT ed.device_id, d.ser_num, ed.installed_from, ed.removed_at
              FROM enterprise_device ed
              JOIN dpd_device d ON d.id = ed.device_id
             WHERE ed.enterprise_id = :id
             ORDER BY ed.installed_from
            """
        ),
        {"id": enterprise_id},
    )).mappings().all()
    return [dict(r) for r in rows]


def _clip(window: Dict, since: datetime, until: datetime):
    """The window intersected with the range asked for, or None if they miss."""
    start = max(window["installed_from"], since)
    end = until if window["removed_at"] is None else min(window["removed_at"], until)
    return None if start >= end else (start, end)


async def _affected(
    session: AsyncSession, enterprise_id: int, since: datetime, until: datetime,
    delete: bool,
) -> Dict:
    spans = []
    for window in await windows(session, enterprise_id):
        clipped = _clip(window, since, until)
        if clipped is not None:
            spans.append((window["device_id"], window["ser_num"], *clipped))

    hour = commercial_day.contract_hour()
    counts = {"hourly": 0, "daily": 0}
    for table, column in _TABLES:
        period = "hourly" if column == "stamp" else "daily"
        for device_id, _ser, start, end in spans:
            if column == "stamp":
                # Half-open: the instant the next gas day opens belongs to it.
                bound, params = "<", {"dev": device_id, "from": start, "to": end}
            else:
                # Daily rows are dated by the gas day they close, so the range
                # is the days themselves, inclusive.
                bound = "<="
                params = {
                    "dev": device_id,
                    "from": commercial_day.day_of(start, hour),
                    "to": commercial_day.day_of(end - timedelta(hours=1), hour),
                }
            verb = "DELETE FROM" if delete else "SELECT count(*) FROM"
            result = await session.execute(
                text(
                    f"{verb} {table} WHERE device_id = :dev "
                    f"AND {column} >= :from AND {column} {bound} :to"
                ),
                params,
            )
            counts[period] += result.rowcount if delete else result.scalar_one()
    return {
        "hourly": counts["hourly"],
        "daily": counts["daily"],
        "devices": [
            {"device_id": d, "ser_num": s,
             "from": start.isoformat(), "to": end.isoformat()}
            for d, s, start, end in spans
        ],
    }


async def preview(
    session: AsyncSession, enterprise_id: int, since: datetime, until: datetime
) -> Dict:
    """How many rows this would remove, and from which correctors."""
    return await _affected(session, enterprise_id, since, until, delete=False)


async def purge(
    session: AsyncSession, enterprise_id: int, since: datetime, until: datetime
) -> Dict:
    """Remove them. The caller owns the transaction."""
    removed = await _affected(session, enterprise_id, since, until, delete=True)
    logger.warning(
        "Archive purge: enterprise %s, %s..%s — %s hourly, %s daily rows",
        enterprise_id, since, until, removed["hourly"], removed["daily"],
    )
    return removed


def day_bounds(since: date, until: date) -> tuple[datetime, datetime]:
    """A pair of dates read as the commercial days they name.

    [since 07:00, until+1 07:00) — the end exclusive, because the hour the
    next gas day opens on is that day's, not this one's.
    """
    return commercial_day.range_window(since, until, commercial_day.contract_hour())

#: Where a point with nothing stored is read from.
#:
#: Not the corrector's install date: that is often unknown, often wrong, and
#: on a point migrated from the old schema it is the epoch — which would ask
#: DPD for twenty-six years and be refused. Early 2024 covers everything this
#: system is expected to answer for.
FIRST_EVER = date(2024, 1, 1)


async def poll_range(session: AsyncSession, enterprise_id: int) -> tuple[date, date]:
    """What to ask DPD for: from where the archive ends, to tomorrow.

    Tomorrow because the day here is a gas day — the hours of the current one
    are filed under a date that has not arrived yet, and ending at today would
    leave them behind on every poll.

    From the newest record rather than a fixed window, so a point polled
    yesterday costs one day and a point nobody has touched since spring costs
    the months it actually missed.
    """
    # The subquery alias is spelled out rather than the obvious «both»:
    # BOTH is a reserved word in Postgres (TRIM(BOTH …)), and an alias that
    # needs quoting is an alias waiting to break.
    newest = (await session.execute(
        text(
            """
            SELECT max(newest) FROM (
                SELECT max(h.stamp)::date AS newest
                  FROM dpd_hourly_archive h
                  JOIN enterprise_device ed ON ed.device_id = h.device_id
                 WHERE ed.enterprise_id = :id
                UNION ALL
                SELECT max(d.day)
                  FROM dpd_daily_archive d
                  JOIN enterprise_device ed ON ed.device_id = d.device_id
                 WHERE ed.enterprise_id = :id
            ) AS newest_of_both
            """
        ),
        {"id": enterprise_id},
    )).scalar_one_or_none()
    tomorrow = date.today() + timedelta(days=1)
    return (newest or FIRST_EVER), tomorrow
