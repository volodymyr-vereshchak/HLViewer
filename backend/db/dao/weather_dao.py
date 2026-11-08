"""Weather points and their daily/hourly rows (backend/db/models/weather_model.py)."""
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Iterable, Optional

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models.weather_model import (
    DateRange,
    WeatherCoverage,
    WeatherDaily,
    WeatherDailyRow,
    WeatherHourly,
    WeatherHourlyRow,
    WeatherLineRef,
    WeatherMissing,
    WeatherPoint,
    WeatherPointLine,
    WeatherPointRead,
)

DAILY_VALUES = ("t_mean", "t_min", "t_max", "precipitation", "wind_max", "radiation")
HOURLY_VALUES = ("temperature", "humidity", "wind_speed", "radiation", "precipitation", "cloud_cover")

# A day counts as covered by the hourly series at 23 hours: the autumn DST day
# has 25 and the spring one 23, so 24 would leave the spring day forever missing.
HOURS_PER_DAY_MIN = 23
# One INSERT ... ON CONFLICT carries this many rows (Postgres caps bind
# parameters at 65535; 8 columns × 5000 stays far below).
UPSERT_CHUNK = 5000


def collapse_days(days: Iterable[date]) -> list[DateRange]:
    """Sorted days → runs of consecutive days."""
    out: list[DateRange] = []
    for d in sorted(set(days)):
        if out and out[-1].last + timedelta(days=1) == d:
            out[-1].last = d
        else:
            out.append(DateRange(first=d, last=d))
    return out


class WeatherDao:
    def __init__(self, session: AsyncSession):
        self.session = session

    # ── points ──────────────────────────────────────────────────────────────

    async def get_point(self, point_id: int) -> Optional[WeatherPoint]:
        return await self.session.get(WeatherPoint, point_id)

    async def list_points(self, branch_ids: Optional[list[int]]) -> list[WeatherPointRead]:
        stmt = select(WeatherPoint).order_by(WeatherPoint.branch_id, WeatherPoint.name)
        if branch_ids is not None:
            stmt = stmt.where(WeatherPoint.branch_id.in_(branch_ids))
        points = list((await self.session.execute(stmt)).scalars())
        if not points:
            return []
        ids = [p.id for p in points]

        lines: dict[int, list[WeatherLineRef]] = defaultdict(list)
        for row in await self.session.execute(
            select(WeatherPointLine).where(WeatherPointLine.point_id.in_(ids))
            .order_by(WeatherPointLine.line_id)
        ):
            pl = row[0]
            lines[pl.point_id].append(WeatherLineRef(kind=pl.line_kind, id=pl.line_id))

        daily = {
            r.point_id: WeatherCoverage(first=r.first, last=r.last, count=r.n)
            for r in await self.session.execute(
                select(
                    WeatherDaily.point_id,
                    func.min(WeatherDaily.day).label("first"),
                    func.max(WeatherDaily.day).label("last"),
                    func.count().label("n"),
                ).where(WeatherDaily.point_id.in_(ids)).group_by(WeatherDaily.point_id)
            )
        }
        hourly = {
            r.point_id: WeatherCoverage(first=r.first.date(), last=r.last.date(), count=r.n)
            for r in await self.session.execute(
                select(
                    WeatherHourly.point_id,
                    func.min(WeatherHourly.stamp).label("first"),
                    func.max(WeatherHourly.stamp).label("last"),
                    func.count().label("n"),
                ).where(WeatherHourly.point_id.in_(ids)).group_by(WeatherHourly.point_id)
            )
        }
        return [
            WeatherPointRead(
                id=p.id, branch_id=p.branch_id, name=p.name,
                latitude=p.latitude, longitude=p.longitude,
                lines=lines.get(p.id, []),
                daily=daily.get(p.id, WeatherCoverage()),
                hourly=hourly.get(p.id, WeatherCoverage()),
            )
            for p in points
        ]

    async def set_lines(self, point_id: int, lines: list[WeatherLineRef]) -> None:
        """Replace the point's lines. A line belongs to one point, so taking it
        here takes it away from whichever point had it."""
        ids = list({ln.id for ln in lines})
        await self.session.execute(
            delete(WeatherPointLine).where(
                (WeatherPointLine.point_id == point_id)
                | (WeatherPointLine.line_id.in_(ids))
            )
        )
        seen = set()
        for ln in lines:
            if ln.id in seen:
                continue
            seen.add(ln.id)
            self.session.add(WeatherPointLine(point_id=point_id, line_kind=ln.kind, line_id=ln.id))

    # ── what is missing ─────────────────────────────────────────────────────

    async def archive_range(self, branch_id: int) -> tuple[Optional[date], Optional[date]]:
        """First and last day any consumption archive of the branch covers:
        physical lines' daily and hourly archives and DPD lines' archives.
        Virtual lines are sums of physical ones and add nothing."""
        row = (await self.session.execute(text("""
            WITH phys AS (
                SELECT l.id FROM gas_volume_line l
                JOIN gas_volume_calc c ON c.id = l.gas_volume_calc_id
                JOIN lumg g ON g.id = c.lumg_id
                WHERE g.branch_id = :b
            ), dpd AS (SELECT id FROM dpd_line WHERE branch_id = :b),
            spans AS (
                SELECT min(period) AS f, max(period) AS l
                  FROM daily_archive WHERE line_id IN (SELECT id FROM phys)
                UNION ALL
                SELECT min(period)::date, max(period)::date
                  FROM hourly_archive WHERE line_id IN (SELECT id FROM phys)
                UNION ALL
                SELECT min(day), max(day)
                  FROM dpd_line_daily_archive WHERE dpd_line_id IN (SELECT id FROM dpd)
                UNION ALL
                SELECT min(stamp)::date, max(stamp)::date
                  FROM dpd_line_hourly_archive WHERE dpd_line_id IN (SELECT id FROM dpd)
            )
            SELECT min(f) AS first, max(l) AS last FROM spans
        """), {"b": branch_id})).one()
        return row.first, row.last

    async def missing(self, point_ids: list[int], first: date, last: date) -> list[WeatherMissing]:
        """Days in [first, last] with no daily row, and days with fewer than
        HOURS_PER_DAY_MIN hours of temperature, per point."""
        if not point_ids or first > last:
            return [WeatherMissing(point_id=p) for p in point_ids]
        have_daily: dict[int, set[date]] = defaultdict(set)
        for r in await self.session.execute(
            select(WeatherDaily.point_id, WeatherDaily.day).where(
                WeatherDaily.point_id.in_(point_ids),
                WeatherDaily.day.between(first, last),
                WeatherDaily.t_mean.is_not(None),
            )
        ):
            have_daily[r.point_id].add(r.day)
        have_hourly: dict[int, set[date]] = defaultdict(set)
        day_col = func.date(WeatherHourly.stamp)
        for r in await self.session.execute(
            select(WeatherHourly.point_id, day_col.label("d"))
            .where(
                WeatherHourly.point_id.in_(point_ids),
                WeatherHourly.stamp >= datetime.combine(first, datetime.min.time()),
                WeatherHourly.stamp < datetime.combine(last + timedelta(days=1), datetime.min.time()),
                WeatherHourly.temperature.is_not(None),
            )
            .group_by(WeatherHourly.point_id, day_col)
            .having(func.count() >= HOURS_PER_DAY_MIN)
        ):
            have_hourly[r.point_id].add(r.d)

        all_days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
        return [
            WeatherMissing(
                point_id=p,
                daily=collapse_days(d for d in all_days if d not in have_daily[p]),
                hourly=collapse_days(d for d in all_days if d not in have_hourly[p]),
            )
            for p in point_ids
        ]

    # ── rows ────────────────────────────────────────────────────────────────

    async def _upsert(self, model, key: tuple[str, str], values: tuple[str, ...], rows: list[dict]) -> int:
        if not rows:
            return 0
        for i in range(0, len(rows), UPSERT_CHUNK):
            chunk = rows[i:i + UPSERT_CHUNK]
            stmt = insert(model).values(chunk)
            stmt = stmt.on_conflict_do_update(
                index_elements=list(key), set_={v: stmt.excluded[v] for v in values},
            )
            await self.session.execute(stmt)
        return len(rows)

    async def import_rows(
        self, point_id: int, daily: list[WeatherDailyRow], hourly: list[WeatherHourlyRow],
    ) -> tuple[int, int]:
        """Upsert the rows. Rows with no value at all are dropped (Open-Meteo
        returns nulls for days the reanalysis has not reached yet — storing
        them would mark those days covered). Duplicates of one key keep the
        last: one INSERT cannot touch a row twice, and the autumn DST hour
        comes twice in a local-time series."""
        d_rows: dict[date, dict] = {}
        for r in daily:
            vals = {k: getattr(r, k) for k in DAILY_VALUES}
            if any(v is not None for v in vals.values()):
                d_rows[r.day] = {"point_id": point_id, "day": r.day, **vals}
        h_rows: dict[datetime, dict] = {}
        for r in hourly:
            vals = {k: getattr(r, k) for k in HOURLY_VALUES}
            if any(v is not None for v in vals.values()):
                stamp = r.stamp.replace(tzinfo=None, minute=0, second=0, microsecond=0)
                h_rows[stamp] = {"point_id": point_id, "stamp": stamp, **vals}
        nd = await self._upsert(WeatherDaily, ("point_id", "day"), DAILY_VALUES, list(d_rows.values()))
        nh = await self._upsert(WeatherHourly, ("point_id", "stamp"), HOURLY_VALUES, list(h_rows.values()))
        return nd, nh

    async def daily(self, point_ids: list[int], first: date, last: date) -> list[WeatherDaily]:
        return list((await self.session.execute(
            select(WeatherDaily).where(
                WeatherDaily.point_id.in_(point_ids), WeatherDaily.day.between(first, last),
            ).order_by(WeatherDaily.day, WeatherDaily.point_id)
        )).scalars())

    async def hourly(self, point_ids: list[int], first: datetime, last: datetime) -> list[WeatherHourly]:
        return list((await self.session.execute(
            select(WeatherHourly).where(
                WeatherHourly.point_id.in_(point_ids),
                WeatherHourly.stamp >= first, WeatherHourly.stamp <= last,
            ).order_by(WeatherHourly.stamp, WeatherHourly.point_id)
        )).scalars())
