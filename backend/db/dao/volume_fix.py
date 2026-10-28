"""Days whose totaliser was set back: 0 until the hours can stand in for them.

A corrector replaced or its archive cleared makes the period's volume "new
reading minus old" — minus everything the old one counted (prod, 28.10.2026:
−1 771 792 m³ in one day). Such a period is stored as 0 and marked in
`volume_fix`:

    NULL      the volume is the one that was reported;
    'zeroed'  it was negative and is stored as 0;
    'hours'   (daily only) it was negative, and is the sum of the hourly
              volumes of its commercial day — every hour of it is in the
              archive, the hour of the jump itself counted as 0.

The two archives arrive independently — the refresh polls days then hours,
an on-demand poll and a GSM read one or the other — so the substitution is
done after every write, by whichever of the two comes second: a marked day
written when its hours are there is summed at once, and the hour that
completes a marked day sums it. Only the devices and days the write touched
are looked at, through a partial index holding the marked days alone.
"""
import os
from datetime import date, datetime, timedelta
from typing import Iterable, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.settings import backend_settings

FIX_ZEROED = "zeroed"
FIX_HOURS = "hours"

# Hourly stamps are naive local time. A commercial day counts as complete
# when every hour of it that exists on the clock is stored: 24 on most days,
# 23 on the spring change (its 03:00 never happens). On the autumn change the
# repeated hour shares one stamp, so 24 there too.
TIMEZONE = os.getenv("TZ", "Europe/Kyiv")


def contract_hour() -> int:
    return int(backend_settings.get("CONTRACT_HOUR", 7))


def fix_of(*volumes: Optional[float]) -> Optional[str]:
    """The mark for a row arriving with these volumes."""
    return FIX_ZEROED if any(v is not None and v < 0 for v in volumes) else None


def day_of_hour(stamp: datetime, hour: int) -> date:
    """The commercial day an hourly stamp belongs to."""
    return (stamp - timedelta(hours=hour)).date()


def repair_sql(daily: str, hourly: str, key: str, volume_cols: Sequence[str]) -> str:
    """UPDATE summing the hours into the marked days of the given keys and
    day range. Rows already holding that sum are left alone, so a write that
    changes nothing writes nothing."""
    sums = ", ".join(f"sum(h.{c}) AS {c}" for c in volume_cols)
    # A column the hours carry no value in at all (sum NULL) — dvwrk on a
    # model that reports dvst — keeps what the day had.
    sets = ", ".join(f"{c} = COALESCE(s.{c}, d.{c})" for c in volume_cols)
    changed = " OR ".join(
        f"d.{c} IS DISTINCT FROM COALESCE(s.{c}, d.{c})" for c in volume_cols
    )
    return (
        f"UPDATE {daily} d SET {sets}, volume_fix = '{FIX_HOURS}' "
        f"FROM ("
        f" SELECT a.id, {sums}, count(*) AS n,"
        f"  (SELECT count(*) FROM generate_series("
        f"     a.day + make_interval(hours => :ch),"
        f"     a.day + make_interval(hours => :ch + 23),"
        f"     interval '1 hour') AS g(t)"
        f"   WHERE (g.t AT TIME ZONE :tz) AT TIME ZONE :tz = g.t) AS expected"
        f" FROM {daily} a"
        f" JOIN {hourly} h ON h.{key} = a.{key}"
        f"  AND h.stamp >= a.day + make_interval(hours => :ch)"
        f"  AND h.stamp < a.day + make_interval(hours => :ch + 24)"
        f" WHERE a.volume_fix IS NOT NULL"
        f"  AND a.{key} = ANY(:ids) AND a.day BETWEEN :d0 AND :d1"
        f" GROUP BY a.id, a.day"
        f") s "
        f"WHERE d.id = s.id AND s.n >= s.expected "
        f"AND (d.volume_fix <> '{FIX_HOURS}' OR {changed})"
    )


async def repair_days(
    session: AsyncSession,
    *,
    daily: str,
    hourly: str,
    key: str,
    volume_cols: Sequence[str],
    ids: Iterable[int],
    day_from: date,
    day_to: date,
) -> int:
    """Sum the hours into the marked days of `ids` within [day_from, day_to].
    Returns how many days were (re)computed."""
    ids = sorted(set(ids))
    if not ids:
        return 0
    result = await session.execute(
        text(repair_sql(daily, hourly, key, volume_cols)),
        {"ch": contract_hour(), "tz": TIMEZONE, "ids": ids, "d0": day_from, "d1": day_to},
    )
    return result.rowcount or 0
