"""
Analytics → GRS consumption models: upload the 1С reports, train, compare.

Reading follows the user's branch scope; uploading and training are
admin-only. Nothing here runs on a schedule: data comes in when an operator
uploads it and a model changes only when the button is pressed.
"""
import asyncio
import logging
from collections import OrderedDict
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.endpoints.auth_ep import get_branch_filter, get_current_user, require_admin
from backend.db.dao.grs_model_dao import GrsModelDao
from backend.db.engine import get_session
from backend.db.models.app_user_model import AppUser
from backend.db.models.grs_model_model import (
    GrsFit,
    GrsStation,
    GrsStationPoint,
    GrsStationUpdate,
    GrsUpload,
    GrsVolume,
)
from backend.db.models.weather_model import WeatherDaily, WeatherPoint
from backend.services.grs_model import parse, service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/grs-model", tags=["grs_model"], dependencies=[Depends(get_current_user)])

#: An export of a year by object is 1.5 MB; anything far beyond is not one.
MAX_UPLOAD_BYTES = 30 * 1024 * 1024

#: Analyses already computed, by what they were computed from. An analysis
#: takes about a second of CPU and the screen asks for the same one on every
#: visit; the key changes whenever the data, the weather or the model does.
_ANALYSES: "OrderedDict[tuple, dict]" = OrderedDict()
_ANALYSES_KEPT = 8


def _check_branch(branch_id: int, allowed: Optional[list[int]]) -> None:
    if allowed is not None and branch_id not in allowed:
        raise HTTPException(status_code=404, detail="Філію не знайдено")


async def _weather_stamp(session: AsyncSession, branch_id: int) -> tuple:
    return tuple((await session.execute(
        select(func.count(WeatherDaily.id), func.max(WeatherDaily.updated_at), func.max(WeatherDaily.day))
        .join(WeatherPoint, WeatherPoint.id == WeatherDaily.point_id)
        .where(WeatherPoint.branch_id == branch_id)
    )).one())


async def load_inputs(session: AsyncSession, branch_id: int) -> service.Inputs:
    dao = GrsModelDao(session)
    stations = await dao.stations(branch_id)
    points = await dao.weather_points(branch_id)
    by_point: dict[int, list] = {}
    for row in await dao.weather_daily([p.id for p in points]):
        by_point.setdefault(row.point_id, []).append(row)
    return service.Inputs(
        stations=[service.Station(id=s.id, name=s.name, enabled=s.enabled,
                                  weather_point_id=s.weather_point_id) for s in stations],
        volumes=await dao.volumes(branch_id),
        covered=await dao.covered_days(branch_id),
        weather={pid: service.weather_frame(rows) for pid, rows in by_point.items()},
    )


# ── status ─────────────────────────────────────────────────────────────────

@router.get("/status")
async def status(
    branch_id: int,
    allowed: Optional[list[int]] = Depends(get_branch_filter),
    session: AsyncSession = Depends(get_session),
):
    """What is stored for the branch, what was uploaded, and the model in use."""
    _check_branch(branch_id, allowed)
    dao = GrsModelDao(session)
    stations = await dao.stations(branch_id)
    is_inflow = GrsVolume.category == parse.INFLOW_CATEGORY
    spans = {
        row.station_id: row
        for row in (await session.execute(
            select(
                GrsVolume.station_id,
                func.min(case((is_inflow, GrsVolume.day))).label("inflow_first"),
                func.max(case((is_inflow, GrsVolume.day))).label("inflow_last"),
                func.count(case((is_inflow, GrsVolume.id))).label("inflow_days"),
                func.min(case((~is_inflow, GrsVolume.day))).label("cons_first"),
                func.max(case((~is_inflow, GrsVolume.day))).label("cons_last"),
            )
            .join(GrsStation, GrsStation.id == GrsVolume.station_id)
            .where(GrsStation.branch_id == branch_id)
            .group_by(GrsVolume.station_id)
        )).all()
    }
    covered = await dao.covered_days(branch_id)
    fit = await dao.latest_fit(branch_id)
    count, changed = await dao.data_stamp(branch_id)
    points = await dao.weather_points(branch_id)
    weather = (await session.execute(
        select(func.min(WeatherDaily.day), func.max(WeatherDaily.day),
               func.max(case((WeatherDaily.source == "archive", WeatherDaily.day))))
        .join(WeatherPoint, WeatherPoint.id == WeatherDaily.point_id)
        .where(WeatherPoint.branch_id == branch_id, WeatherDaily.t_mean.is_not(None))
    )).one()

    def span(kind: str) -> Optional[dict]:
        days = covered.get(kind) or []
        return {"first": days[0], "last": days[-1], "days": len(days)} if days else None

    return {
        "stations": [
            {
                "id": s.id, "name": s.name, "enabled": s.enabled,
                "weather_point_id": s.weather_point_id,
                "inflow_first": getattr(spans.get(s.id), "inflow_first", None),
                "inflow_last": getattr(spans.get(s.id), "inflow_last", None),
                "inflow_days": getattr(spans.get(s.id), "inflow_days", 0),
                "consumption_first": getattr(spans.get(s.id), "cons_first", None),
                "consumption_last": getattr(spans.get(s.id), "cons_last", None),
            }
            for s in stations
        ],
        "inflow": span(parse.INFLOW),
        "consumption": span(parse.CONSUMPTION),
        "rows": count,
        "changed_at": changed,
        "weather": {
            "points": [{"id": p.id, "name": p.name} for p in points],
            "first": weather[0], "last": weather[1], "archive_last": weather[2],
        },
        "uploads": await dao.uploads(branch_id),
        "fit": None if fit is None else {
            "id": fit.id, "trained_at": fit.trained_at, "trained_by": fit.trained_by,
            "first_day": fit.first_day, "last_day": fit.last_day,
            # Data uploaded after the training is analysed with the model as
            # it is; it goes into the model only when it is trained again.
            "stale": bool(changed and changed > fit.trained_at),
            **fit.summary,
        },
    }


# ── upload ─────────────────────────────────────────────────────────────────

@router.post("/upload")
async def upload(
    branch_id: int,
    files: list[UploadFile] = File(...),
    user: AppUser = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Take in 1С reports. Each file is recognised by its contents, and its
    figures are merged into what is stored: new ones added, changed ones
    replaced, nothing held twice."""
    dao = GrsModelDao(session)
    results = []
    for file in files:
        name = file.filename or "файл"
        content = await file.read()
        if len(content) > MAX_UPLOAD_BYTES:
            results.append({"filename": name, "error": "Файл завеликий для звіту 1С"})
            continue
        try:
            parsed = await asyncio.to_thread(parse.parse, content)
        except parse.ParseError as error:
            results.append({"filename": name, "error": str(error)})
            continue

        # The consumption report names the three stations of the ring apart
        # and the balance names the ring: figures that fall under one station
        # name are one figure.
        totals: dict[tuple[str, date, str], float] = {}
        for day, grs, category, volume in parsed.rows:
            key = (parse.grs_name(grs), day, category)
            totals[key] = totals.get(key, 0.0) + volume
        ids = await dao.ensure_stations(branch_id, {key[0] for key in totals})
        added, updated, unchanged = await dao.merge_volumes(
            {(ids[grs], day, category): volume for (grs, day, category), volume in totals.items()})
        await dao.cover_days(branch_id, parsed.kind, parsed.days)
        entry = await dao.log_upload(GrsUpload(
            branch_id=branch_id, filename=name[:255], kind=parsed.kind, layout=parsed.layout,
            title=parsed.title[:500], first_day=parsed.days[0], last_day=parsed.days[-1],
            days=len(parsed.days), stations=len({key[0] for key in totals}),
            added=added, updated=updated, unchanged=unchanged,
            uploaded_at=datetime.now(), uploaded_by=user.username,
        ))
        await session.commit()
        logger.info("GRS model upload %s: %s, +%d ~%d =%d", name, parsed.kind, added, updated, unchanged)
        results.append({
            "filename": name, "kind": entry.kind, "layout": entry.layout, "title": entry.title,
            "first_day": entry.first_day, "last_day": entry.last_day, "days": entry.days,
            "stations": entry.stations, "added": added, "updated": updated, "unchanged": unchanged,
        })
    return {"files": results}


# ── training ───────────────────────────────────────────────────────────────

@router.post("/train")
async def train(
    branch_id: int,
    user: AppUser = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Train the models of the branch on everything stored now."""
    inputs = await load_inputs(session, branch_id)
    try:
        summary, payload = await asyncio.to_thread(service.train, inputs)
    except service.ModelError as error:
        raise HTTPException(status_code=422, detail=str(error))
    fit = await GrsModelDao(session).save_fit(GrsFit(
        branch_id=branch_id, trained_at=datetime.now(), trained_by=user.username,
        first_day=date.fromisoformat(summary["first_day"]),
        last_day=date.fromisoformat(summary["last_day"]),
        summary=summary, payload=payload,
    ))
    await session.commit()
    logger.info("GRS models trained for branch %s: %d stations", branch_id,
                sum(1 for s in summary["stations"] if s["status"] == "ok"))
    return {"id": fit.id, "trained_at": fit.trained_at, **summary}


# ── analysis ───────────────────────────────────────────────────────────────

@router.get("/analysis")
async def analysis(
    branch_id: int,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    allowed: Optional[list[int]] = Depends(get_branch_filter),
    session: AsyncSession = Depends(get_session),
):
    """Every station against its model over the period: actual, expected,
    deviation, alarms, episodes — and the days ahead by the weather forecast."""
    _check_branch(branch_id, allowed)
    dao = GrsModelDao(session)
    fit = await dao.latest_fit(branch_id)
    if fit is None:
        raise HTTPException(status_code=409, detail="Модель ще не навчено")
    key = (branch_id, fit.id, await dao.data_stamp(branch_id),
           await _weather_stamp(session, branch_id), date_from, date_to,
           tuple((s.id, s.enabled, s.weather_point_id) for s in await dao.stations(branch_id)))
    cached = _ANALYSES.get(key)
    if cached is not None:
        _ANALYSES.move_to_end(key)
        return cached
    inputs = await load_inputs(session, branch_id)
    try:
        result = await asyncio.to_thread(service.analyse, inputs, fit.payload, date_from, date_to)
    except service.ModelError as error:
        raise HTTPException(status_code=422, detail=str(error))
    result["trained_at"] = fit.trained_at
    _ANALYSES[key] = result
    while len(_ANALYSES) > _ANALYSES_KEPT:
        _ANALYSES.popitem(last=False)
    return result


# ── stations ───────────────────────────────────────────────────────────────

@router.post("/stations/weather-points")
async def create_station_points(
    items: list[GrsStationPoint],
    _: AppUser = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """A weather point for each station, named after it, where it is — and
    the station tied to it. The weather points of a branch come from its GRS
    this way, not from lines. A point of that name already in the branch is
    taken as it is, so pressing twice makes nothing twice."""
    dao = GrsModelDao(session)
    out = []
    for item in items:
        station = await dao.station(item.station_id)
        if station is None:
            raise HTTPException(status_code=404, detail="ГРС не знайдено")
        point = (await session.execute(
            select(WeatherPoint).where(WeatherPoint.branch_id == station.branch_id,
                                       WeatherPoint.name == station.name)
        )).scalars().first()
        created = point is None
        if created:
            point = WeatherPoint(branch_id=station.branch_id, name=station.name,
                                 latitude=item.latitude, longitude=item.longitude)
            session.add(point)
            await session.flush()
        station.weather_point_id = point.id
        station.updated_at = datetime.now()
        out.append({"station_id": station.id, "weather_point_id": point.id, "created": created})
    await session.commit()
    return out


@router.patch("/stations/{station_id}")
async def update_station(
    station_id: int,
    data: GrsStationUpdate,
    _: AppUser = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Leave a station out, or say whose weather explains it. Either takes
    effect on the next training."""
    station = await GrsModelDao(session).station(station_id)
    if station is None:
        raise HTTPException(status_code=404, detail="ГРС не знайдено")
    if data.enabled is not None:
        station.enabled = data.enabled
    if data.weather_point_id is not None:
        if data.weather_point_id == 0:
            station.weather_point_id = None
        else:
            point = await session.get(WeatherPoint, data.weather_point_id)
            if point is None or point.branch_id != station.branch_id:
                raise HTTPException(status_code=422, detail="Точка погоди не з цієї філії")
            station.weather_point_id = point.id
    station.updated_at = datetime.now()
    await session.commit()
    return {"id": station.id, "name": station.name, "enabled": station.enabled,
            "weather_point_id": station.weather_point_id}
