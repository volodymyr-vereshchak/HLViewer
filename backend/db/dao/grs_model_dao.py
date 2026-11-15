"""Stations, uploaded volumes and fits of the GRS consumption models."""
from datetime import date, datetime
from typing import Iterable, Optional

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models.grs_model_model import (
    GrsCoveredDay,
    GrsFit,
    GrsStation,
    GrsStationLine,
    GrsUpload,
    GrsVolume,
)
from backend.db.models.weather_model import WeatherDaily, WeatherPoint

# asyncpg takes at most 32767 bind parameters in one statement.
MAX_BIND_PARAMS = 32767
#: Two volumes this close are the same figure: 1С prints two decimals.
SAME_VOLUME = 0.005


class GrsModelDao:
    def __init__(self, session: AsyncSession):
        self.session = session

    # ── stations ────────────────────────────────────────────────────────────

    async def stations(self, branch_id: int) -> list[GrsStation]:
        return list((await self.session.execute(
            select(GrsStation).where(GrsStation.branch_id == branch_id).order_by(GrsStation.name)
        )).scalars())

    async def station(self, station_id: int) -> Optional[GrsStation]:
        return await self.session.get(GrsStation, station_id)

    async def ensure_stations(self, branch_id: int, names: Iterable[str]) -> dict[str, int]:
        """name → id, creating the stations that are new."""
        known = {s.name: s.id for s in await self.stations(branch_id)}
        for name in sorted(set(names) - set(known)):
            station = GrsStation(branch_id=branch_id, name=name)
            self.session.add(station)
            await self.session.flush()
            known[name] = station.id
        return known

    async def station_lines(self, branch_id: int) -> dict[int, list[dict]]:
        """station id → its lines, as {kind, id}."""
        out: dict[int, list[dict]] = {}
        for row in (await self.session.execute(
            select(GrsStationLine).join(GrsStation, GrsStation.id == GrsStationLine.station_id)
            .where(GrsStation.branch_id == branch_id)
            .order_by(GrsStationLine.line_kind, GrsStationLine.line_id)
        )).scalars():
            out.setdefault(row.station_id, []).append({"kind": row.line_kind, "id": row.line_id})
        return out

    async def set_station_lines(self, station_id: int, lines: list[tuple[str, int]]) -> None:
        for row in (await self.session.execute(
            select(GrsStationLine).where(GrsStationLine.station_id == station_id)
        )).scalars():
            await self.session.delete(row)
        await self.session.flush()
        for kind, line_id in sorted(set(lines)):
            self.session.add(GrsStationLine(station_id=station_id, line_kind=kind, line_id=line_id))

    # ── volumes ─────────────────────────────────────────────────────────────

    async def merge_volumes(self, rows: dict[tuple[int, date, str], float]) -> tuple[int, int, int]:
        """Write the figures of one report. Returns (added, updated, unchanged).

        Nothing is ever held twice: the key is (station, day, category). A
        figure that is not there yet is added; one that is there and equal is
        left alone; one that is there and different is replaced — a report
        taken later carries 1С's corrections, and the newer figure is the one
        the balance was closed on.
        """
        if not rows:
            return 0, 0, 0
        station_ids = sorted({key[0] for key in rows})
        first = min(key[1] for key in rows)
        last = max(key[1] for key in rows)
        existing = {
            (r.station_id, r.day, r.category): r.volume
            for r in (await self.session.execute(
                select(GrsVolume.station_id, GrsVolume.day, GrsVolume.category, GrsVolume.volume)
                .where(GrsVolume.station_id.in_(station_ids), GrsVolume.day.between(first, last))
            )).all()
        }
        now = datetime.now()
        added, changed, unchanged = [], [], 0
        for key, volume in rows.items():
            old = existing.get(key)
            row = {"station_id": key[0], "day": key[1], "category": key[2], "volume": volume,
                   "created_at": now, "updated_at": now}
            if old is None:
                added.append(row)
            elif abs(old - volume) > SAME_VOLUME:
                changed.append(row)
            else:
                unchanged += 1

        per_insert = MAX_BIND_PARAMS // 6
        for batch in (added, changed):
            for i in range(0, len(batch), per_insert):
                stmt = insert(GrsVolume).values(batch[i:i + per_insert])
                stmt = stmt.on_conflict_do_update(
                    constraint="uq_grs_model_volume_key",
                    set_={"volume": stmt.excluded.volume, "updated_at": stmt.excluded.updated_at},
                )
                await self.session.execute(stmt)
        return len(added), len(changed), unchanged

    async def cover_days(self, branch_id: int, kind: str, days: Iterable[date]) -> None:
        rows = [{"branch_id": branch_id, "kind": kind, "day": d} for d in set(days)]
        per_insert = MAX_BIND_PARAMS // 3
        for i in range(0, len(rows), per_insert):
            await self.session.execute(
                insert(GrsCoveredDay).values(rows[i:i + per_insert]).on_conflict_do_nothing())

    async def covered_days(self, branch_id: int) -> dict[str, list[date]]:
        out: dict[str, list[date]] = {}
        for kind, day in (await self.session.execute(
            select(GrsCoveredDay.kind, GrsCoveredDay.day)
            .where(GrsCoveredDay.branch_id == branch_id).order_by(GrsCoveredDay.day)
        )).all():
            out.setdefault(kind, []).append(day)
        return out

    async def volumes(self, branch_id: int) -> list[tuple[int, date, str, float]]:
        return [tuple(r) for r in (await self.session.execute(
            select(GrsVolume.station_id, GrsVolume.day, GrsVolume.category, GrsVolume.volume)
            .join(GrsStation, GrsStation.id == GrsVolume.station_id)
            .where(GrsStation.branch_id == branch_id)
        )).all()]

    async def data_stamp(self, branch_id: int) -> tuple[int, Optional[datetime]]:
        """How much is stored and when it last changed — a cheap way to tell
        whether anything was uploaded since a model was trained."""
        count, changed = (await self.session.execute(
            select(func.count(GrsVolume.id), func.max(GrsVolume.updated_at))
            .join(GrsStation, GrsStation.id == GrsVolume.station_id)
            .where(GrsStation.branch_id == branch_id)
        )).one()
        return int(count or 0), changed

    # ── uploads ─────────────────────────────────────────────────────────────

    async def log_upload(self, upload: GrsUpload) -> GrsUpload:
        self.session.add(upload)
        await self.session.flush()
        return upload

    async def uploads(self, branch_id: int, limit: int = 30) -> list[GrsUpload]:
        return list((await self.session.execute(
            select(GrsUpload).where(GrsUpload.branch_id == branch_id)
            .order_by(GrsUpload.uploaded_at.desc()).limit(limit)
        )).scalars())

    # ── fits ────────────────────────────────────────────────────────────────

    async def latest_fit(self, branch_id: int) -> Optional[GrsFit]:
        return (await self.session.execute(
            select(GrsFit).where(GrsFit.branch_id == branch_id)
            .order_by(GrsFit.trained_at.desc()).limit(1)
        )).scalars().first()

    async def save_fit(self, fit: GrsFit, keep: int = 5) -> GrsFit:
        """Store a fit, and drop all but the last few of the branch: a payload
        is a megabyte of JSON and nobody reads the tenth one back."""
        self.session.add(fit)
        await self.session.flush()
        old = list((await self.session.execute(
            select(GrsFit).where(GrsFit.branch_id == fit.branch_id)
            .order_by(GrsFit.trained_at.desc()).offset(keep)
        )).scalars())
        for row in old:
            await self.session.delete(row)
        return fit

    # ── weather ─────────────────────────────────────────────────────────────

    async def weather_points(self, branch_id: int) -> list[WeatherPoint]:
        return list((await self.session.execute(
            select(WeatherPoint).where(WeatherPoint.branch_id == branch_id).order_by(WeatherPoint.name)
        )).scalars())

    async def weather_daily(self, point_ids: list[int]) -> list[WeatherDaily]:
        if not point_ids:
            return []
        return list((await self.session.execute(
            select(WeatherDaily).where(WeatherDaily.point_id.in_(point_ids))
            .order_by(WeatherDaily.day)
        )).scalars())
