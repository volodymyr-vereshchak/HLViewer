"""
Analytics → weather: points on the map, what is missing, import and read.

Reading follows the user's branch scope; everything that writes is admin-only.
The server never downloads weather itself (production has no internet): the
browser fetches Open-Meteo, or parses a file the user brought, and posts here.
"""
from datetime import date, datetime, time, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.endpoints.auth_ep import get_branch_filter, get_current_user, require_admin
from backend.db.dao.weather_dao import WeatherDao
from backend.db.engine import get_session
from backend.services import weather_refresh
from backend.db.models.weather_model import (
    LINE_KINDS,
    WEATHER_SOURCES,
    WeatherDailyRead,
    WeatherHourlyRead,
    WeatherAutoRead,
    WeatherAutoUpdate,
    WeatherImport,
    WeatherImportResult,
    WeatherLineRef,
    WeatherPlan,
    WeatherPoint,
    WeatherPointCreate,
    WeatherPointRead,
    WeatherPointUpdate,
)

router = APIRouter(prefix="/weather", tags=["weather"], dependencies=[Depends(get_current_user)])

# One read covers at most this span: hourly rows for years of a dozen points
# would be a response nobody can chart anyway.
MAX_READ_DAYS = 3 * 366


def _check_kinds(lines: list[WeatherLineRef]) -> None:
    bad = [ln.kind for ln in lines if ln.kind not in LINE_KINDS]
    if bad:
        raise HTTPException(status_code=422, detail=f"Невідомий тип лінії: {bad[0]}")


def _allowed(branch_id: int, allowed: Optional[list[int]]) -> bool:
    return allowed is None or branch_id in allowed


async def _point_or_404(dao: WeatherDao, point_id: int, allowed: Optional[list[int]]) -> WeatherPoint:
    point = await dao.get_point(point_id)
    if point is None or not _allowed(point.branch_id, allowed):
        raise HTTPException(status_code=404, detail="Точку погоди не знайдено")
    return point


async def _read_one(dao: WeatherDao, point_id: int) -> WeatherPointRead:
    point = await dao.get_point(point_id)
    return next(p for p in await dao.list_points([point.branch_id]) if p.id == point_id)


@router.get("/points", response_model=list[WeatherPointRead])
async def list_points(
    branch_id: Optional[int] = None,
    allowed: Optional[list[int]] = Depends(get_branch_filter),
    session: AsyncSession = Depends(get_session),
):
    if branch_id is not None:
        if not _allowed(branch_id, allowed):
            return []
        scope = [branch_id]
    else:
        scope = allowed
    return await WeatherDao(session).list_points(scope)


@router.post("/points", response_model=WeatherPointRead, status_code=201,
             dependencies=[Depends(require_admin)])
async def create_point(data: WeatherPointCreate, session: AsyncSession = Depends(get_session)):
    _check_kinds(data.lines)
    dao = WeatherDao(session)
    point = WeatherPoint(
        branch_id=data.branch_id, name=data.name.strip(),
        latitude=data.latitude, longitude=data.longitude,
    )
    session.add(point)
    try:
        await session.flush()
        await dao.set_lines(point.id, data.lines)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Точка з такою назвою вже є у цій філії")
    return await _read_one(dao, point.id)


@router.patch("/points/{point_id}", response_model=WeatherPointRead,
              dependencies=[Depends(require_admin)])
async def update_point(
    point_id: int, data: WeatherPointUpdate, session: AsyncSession = Depends(get_session),
):
    dao = WeatherDao(session)
    point = await _point_or_404(dao, point_id, None)
    if data.name is not None:
        point.name = data.name.strip()
    if data.latitude is not None:
        point.latitude = data.latitude
    if data.longitude is not None:
        point.longitude = data.longitude
    point.updated_at = datetime.now()
    try:
        if data.lines is not None:
            _check_kinds(data.lines)
            await dao.set_lines(point_id, data.lines)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Точка з такою назвою вже є у цій філії")
    return await _read_one(dao, point_id)


@router.delete("/points/{point_id}", status_code=status.HTTP_204_NO_CONTENT,
               dependencies=[Depends(require_admin)])
async def delete_point(point_id: int, session: AsyncSession = Depends(get_session)):
    dao = WeatherDao(session)
    point = await _point_or_404(dao, point_id, None)
    await session.delete(point)
    await session.commit()


@router.get("/plan", response_model=WeatherPlan)
async def plan(
    branch_id: int,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    allowed: Optional[list[int]] = Depends(get_branch_filter),
    session: AsyncSession = Depends(get_session),
):
    """The span the branch's archives cover and, per point, the days in it
    (or in the given range) that still have no weather."""
    if not _allowed(branch_id, allowed):
        raise HTTPException(status_code=404, detail="Філію не знайдено")
    dao = WeatherDao(session)
    first, last = await dao.archive_range(branch_id)
    points = [p.id for p in await dao.list_points([branch_id])]
    lo = date_from or first
    hi = date_to or last
    # Weather for today cannot exist yet; the reanalysis lags a few days more,
    # which the frontend reports rather than hides.
    yesterday = date.today() - timedelta(days=1)
    if hi is not None and hi > yesterday:
        hi = yesterday
    missing = await dao.missing(points, lo, hi) if lo and hi else []
    return WeatherPlan(archive_first=first, archive_last=last, points=missing)


@router.post("/import", response_model=WeatherImportResult, dependencies=[Depends(require_admin)])
async def import_weather(data: WeatherImport, session: AsyncSession = Depends(get_session)):
    dao = WeatherDao(session)
    await _point_or_404(dao, data.point_id, None)
    if data.source not in WEATHER_SOURCES:
        raise HTTPException(status_code=422, detail=f"Невідоме джерело: {data.source}")
    nd, nh = await dao.import_rows(data.point_id, data.daily, data.hourly, source=data.source)
    await session.commit()
    return WeatherImportResult(daily=nd, hourly=nh)


def _check_span(date_from: date, date_to: date) -> None:
    if date_from > date_to:
        raise HTTPException(status_code=422, detail="Початок періоду пізніше кінця")
    if (date_to - date_from).days > MAX_READ_DAYS:
        raise HTTPException(status_code=422, detail="Період задовгий (максимум 3 роки)")


async def _scoped_points(dao: WeatherDao, point_id: list[int], allowed) -> list[int]:
    ids = []
    for pid in point_id:
        point = await dao.get_point(pid)
        if point is not None and _allowed(point.branch_id, allowed):
            ids.append(pid)
    return ids


@router.get("/daily", response_model=list[WeatherDailyRead])
async def daily(
    point_id: list[int] = Query(...),
    date_from: date = Query(...),
    date_to: date = Query(...),
    allowed: Optional[list[int]] = Depends(get_branch_filter),
    session: AsyncSession = Depends(get_session),
):
    _check_span(date_from, date_to)
    dao = WeatherDao(session)
    ids = await _scoped_points(dao, point_id, allowed)
    return await dao.daily(ids, date_from, date_to) if ids else []


@router.get("/hourly", response_model=list[WeatherHourlyRead])
async def hourly(
    point_id: list[int] = Query(...),
    date_from: date = Query(...),
    date_to: date = Query(...),
    allowed: Optional[list[int]] = Depends(get_branch_filter),
    session: AsyncSession = Depends(get_session),
):
    _check_span(date_from, date_to)
    dao = WeatherDao(session)
    ids = await _scoped_points(dao, point_id, allowed)
    if not ids:
        return []
    return await dao.hourly(
        ids, datetime.combine(date_from, time.min), datetime.combine(date_to, time(23, 59, 59)),
    )


# ── automatic update (the server downloads by itself) ───────────────────────

@router.get("/auto", response_model=WeatherAutoRead, dependencies=[Depends(require_admin)])
async def auto_status():
    return await weather_refresh.read_status()


@router.put("/auto", response_model=WeatherAutoRead, dependencies=[Depends(require_admin)])
async def auto_settings(data: WeatherAutoUpdate):
    return await weather_refresh.write_settings(data.auto_enabled, data.refresh_time)


@router.post("/auto/run", response_model=WeatherAutoRead, status_code=202,
             dependencies=[Depends(require_admin)])
async def auto_run_now():
    """Run the update now, on the server, whatever the schedule says."""
    if not await weather_refresh.start_in_background():
        raise HTTPException(status_code=409, detail="Оновлення погоди вже виконується")
    return await weather_refresh.read_status()
