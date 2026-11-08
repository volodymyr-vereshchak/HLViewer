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
# Bind parameters one INSERT may carry. asyncpg refuses more than 32767 —
# half of what Postgres itself allows — and an hourly row binds 8 of them, so
# the old fixed 5000 rows (40000 parameters) failed the first time the server
# imported a year of hours in one call. Rows per INSERT follow from this.
MAX_BIND_PARAMS = 32767
# An archive day this old that Open-Meteo still returns empty is not "not
# published yet" — the reanalysis lags days, not weeks. Such a day is stored
# as an empty archive row so it stops counting as missing and is not asked
# for again on every run.
ARCHIVE_FINAL_DAYS = 10


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
                ).where(
                    WeatherDaily.point_id.in_(ids),
                    WeatherDaily.source == "archive",
                    WeatherDaily.t_mean.is_not(None),
                ).group_by(WeatherDaily.point_id)
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
                ).where(
                    WeatherHourly.point_id.in_(ids),
                    WeatherHourly.source == "archive",
                    WeatherHourly.temperature.is_not(None),
                ).group_by(WeatherHourly.point_id)
            )
        }
        forecast = {
            r.point_id: (r.until, r.at)
            for r in await self.session.execute(
                select(
                    WeatherDaily.point_id,
                    func.max(WeatherDaily.day).label("until"),
                    func.max(WeatherDaily.updated_at).label("at"),
                ).where(
                    WeatherDaily.point_id.in_(ids), WeatherDaily.source == "forecast",
                ).group_by(WeatherDaily.point_id)
            )
        }
        return [
            WeatherPointRead(
                id=p.id, branch_id=p.branch_id, name=p.name,
                latitude=p.latitude, longitude=p.longitude,
                lines=lines.get(p.id, []),
                daily=daily.get(p.id, WeatherCoverage()),
                hourly=hourly.get(p.id, WeatherCoverage()),
                forecast_until=forecast.get(p.id, (None, None))[0],
                forecast_updated_at=forecast.get(p.id, (None, None))[1],
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
        """Days in [first, last] with no archive row, and days with fewer than
        HOURS_PER_DAY_MIN archive hours, per point. A forecast does not cover
        a day — the archive still has to replace it. An empty archive row
        does: it records that the archive has nothing there (see
        ARCHIVE_FINAL_DAYS)."""
        if not point_ids or first > last:
            return [WeatherMissing(point_id=p) for p in point_ids]
        have_daily: dict[int, set[date]] = defaultdict(set)
        for r in await self.session.execute(
            select(WeatherDaily.point_id, WeatherDaily.day).where(
                WeatherDaily.point_id.in_(point_ids),
                WeatherDaily.day.between(first, last),
                WeatherDaily.source == "archive",
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
                WeatherHourly.source == "archive",
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

    async def _upsert(self, model, key: tuple[str, str], values: tuple[str, ...],
                      rows: list[dict], source: str, keep_existing: bool = False) -> int:
        """Rows written (inserted or updated). History overwrites anything; a
        forecast overwrites only a forecast, so a day the archive already has
        is left alone. `keep_existing` inserts only — for the empty archive
        markers, which must never blank out values already stored."""
        if not rows:
            return 0
        written = 0
        per_insert = MAX_BIND_PARAMS // len(rows[0])
        for i in range(0, len(rows), per_insert):
            stmt = insert(model).values(rows[i:i + per_insert])
            if keep_existing:
                stmt = stmt.on_conflict_do_nothing(index_elements=list(key))
            else:
                stmt = stmt.on_conflict_do_update(
                    index_elements=list(key),
                    set_={c: stmt.excluded[c] for c in (*values, "source", "updated_at")},
                    where=(model.source == "forecast") if source == "forecast" else None,
                )
            written += len((await self.session.execute(stmt.returning(model.id))).all())
        return written

    async def import_rows(
        self, point_id: int, daily: list[WeatherDailyRow], hourly: list[WeatherHourlyRow],
        source: str = "archive", today: Optional[date] = None,
    ) -> tuple[int, int]:
        """Upsert the rows; returns how many were written.

        A row with no value at all is dropped while it may still fill in
        (Open-Meteo returns nulls for days the reanalysis has not reached, and
        a forecast with nothing in it says nothing). An ARCHIVE day older
        than ARCHIVE_FINAL_DAYS that is still empty is kept, as a marker that
        the archive has nothing there. Duplicates of one key keep the last:
        one INSERT cannot touch a row twice, and the autumn DST hour comes
        twice in a local-time series."""
        now = datetime.now()
        final_before = (today or date.today()) - timedelta(days=ARCHIVE_FINAL_DAYS)

        def split(items, key_of, values):
            full: dict = {}
            empty: dict = {}
            for r, k, day in items:
                vals = {c: getattr(r, c) for c in values}
                row = {"point_id": point_id, **k, **vals, "source": source,
                       "created_at": now, "updated_at": now}
                if any(v is not None for v in vals.values()):
                    full[key_of(k)] = row
                elif source == "archive" and day < final_before:
                    empty[key_of(k)] = row
            return list(full.values()), [v for kk, v in empty.items() if kk not in full]

        d_full, d_empty = split(
            ((r, {"day": r.day}, r.day) for r in daily), lambda k: k["day"], DAILY_VALUES,
        )
        h_items = []
        for r in hourly:
            stamp = r.stamp.replace(tzinfo=None, minute=0, second=0, microsecond=0)
            h_items.append((r, {"stamp": stamp}, stamp.date()))
        h_full, h_empty = split(h_items, lambda k: k["stamp"], HOURLY_VALUES)

        nd = await self._upsert(WeatherDaily, ("point_id", "day"), DAILY_VALUES, d_full, source)
        nh = await self._upsert(WeatherHourly, ("point_id", "stamp"), HOURLY_VALUES, h_full, source)
        await self._upsert(WeatherDaily, ("point_id", "day"), DAILY_VALUES, d_empty, source, keep_existing=True)
        await self._upsert(WeatherHourly, ("point_id", "stamp"), HOURLY_VALUES, h_empty, source, keep_existing=True)
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
