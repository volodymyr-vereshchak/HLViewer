"""Gas routes: CRUD plus the «Звірка ФХП» report.

Writes need no role check of their own — the middleware in `api/main.py` makes
every POST/PATCH/DELETE admin-only. Reads are scoped by branch, and a route in
a branch the viewer cannot see answers 404 rather than 403, so route ids of
other branches cannot be discovered by probing.
"""

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete as sa_delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from backend.api.endpoints.auth_ep import get_branch_filter, get_current_user
from backend.db.dao.fhp_dao import FhpDao
from backend.db.engine import get_session
from backend.db.models.fhp_report_models import RouteFhpReport
from backend.db.models.gas_route_model import (
    FreeLine,
    GasRoute,
    GasRouteMember,
    GasRouteMemberOut,
    GasRouteRead,
    GasRouteWrite,
)
from backend.db.models.gas_volume_calc_model import GasVolumeCalc
from backend.db.models.line_model import Line
from backend.db.models.lumg_model import Lumg
from backend.services import fhp_route_service
from backend.services.fhp_route_service import (
    DEFAULT_STALE_AFTER_HOURS,
    FHP_PARAMS,
    RouteReportError,
    build_route_report,
)


class GasRouteRouter:
    def __init__(self):
        self.router = APIRouter(dependencies=[Depends(get_current_user)])

        self.router.add_api_route(
            path="/gas_routes/",
            tags=["gas_routes"],
            endpoint=self.get_all,
            methods=["GET"],
            response_model=List[GasRouteRead],
            summary="Список маршрутів",
        )
        self.router.add_api_route(
            path="/gas_routes/free_lines/",
            tags=["gas_routes"],
            endpoint=self.free_lines,
            methods=["GET"],
            response_model=List[FreeLine],
            summary="Лінії філії, не зайняті іншим маршрутом",
        )
        self.router.add_api_route(
            path="/gas_routes/{route_id}",
            tags=["gas_routes"],
            endpoint=self.get_one,
            methods=["GET"],
            response_model=GasRouteRead,
            summary="Маршрут",
        )
        self.router.add_api_route(
            path="/gas_routes/",
            tags=["gas_routes"],
            endpoint=self.create,
            methods=["POST"],
            response_model=GasRouteRead,
            status_code=status.HTTP_201_CREATED,
            summary="Створити маршрут",
        )
        self.router.add_api_route(
            path="/gas_routes/{route_id}",
            tags=["gas_routes"],
            endpoint=self.update,
            methods=["PATCH"],
            response_model=GasRouteRead,
            summary="Змінити маршрут",
        )
        self.router.add_api_route(
            path="/gas_routes/{route_id}",
            tags=["gas_routes"],
            endpoint=self.delete,
            methods=["DELETE"],
            status_code=status.HTTP_204_NO_CONTENT,
            summary="Видалити маршрут",
        )
        self.router.add_api_route(
            path="/gas_routes/{route_id}/data_until",
            tags=["gas_routes"],
            endpoint=self.data_until,
            methods=["GET"],
            summary="Момент останніх даних по лініях маршруту",
        )
        self.router.add_api_route(
            path="/gas_routes/{route_id}/fhp_report",
            tags=["gas_routes"],
            endpoint=self.fhp_report,
            methods=["GET"],
            response_model=RouteFhpReport,
            summary="Звірка ФХП по маршруту",
        )

    # ── CRUD ─────────────────────────────────────────────────────────────────

    async def get_all(
        self,
        branch_id: Optional[int] = None,
        active: Optional[bool] = None,
        allowed_branches: Optional[list[int]] = Depends(get_branch_filter),
        session: AsyncSession = Depends(get_session),
    ) -> List[GasRouteRead]:
        branch_ids = [branch_id] if branch_id is not None else None
        if allowed_branches is not None:
            branch_ids = (
                [b for b in branch_ids if b in allowed_branches]
                if branch_ids is not None else allowed_branches
            )
            if not branch_ids:
                return []

        stmt = select(GasRoute)
        if branch_ids is not None:
            stmt = stmt.where(GasRoute.branch_id.in_(branch_ids))
        if active is not None:
            stmt = stmt.where(GasRoute.active == active)
        # Explicit order: without it an UPDATE moves the touched row to the end
        # of the heap and the admin list reshuffles on every switch toggle.
        stmt = stmt.order_by(GasRoute.branch_id, GasRoute.number, GasRoute.id)

        routes = (await session.execute(stmt)).scalars().all()
        return [await _to_read(r, session) for r in routes]

    async def get_one(
        self,
        route_id: int,
        allowed_branches: Optional[list[int]] = Depends(get_branch_filter),
        session: AsyncSession = Depends(get_session),
    ) -> GasRouteRead:
        route = await _get_visible(route_id, allowed_branches, session)
        return await _to_read(route, session)

    async def create(
        self, data: GasRouteWrite, session: AsyncSession = Depends(get_session)
    ) -> GasRouteRead:
        _check_number(data)
        await _check_members(data, session, route_id=None)
        route = GasRoute(**data.model_dump(exclude={"members"}))
        session.add(route)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            raise _duplicate_number(data.number)
        await _replace_members(route.id, data, session)
        await session.commit()
        await session.refresh(route)
        return await _to_read(route, session)

    async def update(
        self,
        route_id: int,
        data: GasRouteWrite,
        session: AsyncSession = Depends(get_session),
    ) -> GasRouteRead:
        route = await session.get(GasRoute, route_id)
        if route is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Маршрут не знайдено")
        _check_number(data)
        await _check_members(data, session, route_id=route_id)

        for key, value in data.model_dump(exclude={"members"}).items():
            setattr(route, key, value)
        await session.execute(
            sa_delete(GasRouteMember).where(GasRouteMember.route_id == route_id)
        )
        await _replace_members(route_id, data, session)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            raise _duplicate_number(data.number)
        await session.refresh(route)
        return await _to_read(route, session)

    async def delete(
        self, route_id: int, session: AsyncSession = Depends(get_session)
    ) -> None:
        route = await session.get(GasRoute, route_id)
        if route is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Маршрут не знайдено")
        await session.delete(route)
        await session.commit()

    async def free_lines(
        self,
        branch_id: int,
        route_id: Optional[int] = None,
        allowed_branches: Optional[list[int]] = Depends(get_branch_filter),
        session: AsyncSession = Depends(get_session),
    ) -> List[FreeLine]:
        """Lines of the branch that no OTHER route holds.

        `route_id` keeps the edited route's own members in the list, so the
        picker can show what is already selected.
        """
        if allowed_branches is not None and branch_id not in allowed_branches:
            return []

        taken = select(GasRouteMember.line_id)
        if route_id is not None:
            taken = taken.where(GasRouteMember.route_id != route_id)

        stmt = (
            select(Line.id, Line.name, GasVolumeCalc.name)
            .join(GasVolumeCalc, GasVolumeCalc.id == Line.gas_volume_calc_id)
            .join(Lumg, Lumg.id == GasVolumeCalc.lumg_id)
            .where(Lumg.branch_id == branch_id)
            .where(Line.id.not_in(taken))
            .order_by(GasVolumeCalc.name, Line.name)
        )
        rows = (await session.execute(stmt)).all()
        return [
            FreeLine(id=lid, name=name or f"Лінія {lid}", calc_name=calc)
            for lid, name, calc in rows
        ]

    # ── Report ───────────────────────────────────────────────────────────────

    async def data_until(
        self,
        route_id: int,
        allowed_branches: Optional[list[int]] = Depends(get_branch_filter),
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        """How far the archive reaches for this route — the same moment the
        overview screen shows. The report cannot go past it, so the screen
        offers it before a period is chosen."""
        await _get_visible(route_id, allowed_branches, session)
        members = await fhp_route_service.load_members(session, route_id)
        stamp = await FhpDao(session=session).last_data_period(
            [m.line_id for m in members]
        )
        return {"data_until": stamp.isoformat() if stamp else None}

    async def fhp_report(
        self,
        route_id: int,
        date_from: str = Query(..., description="YYYY-MM-DD"),
        date_to: str = Query(..., description="YYYY-MM-DD"),
        params: Optional[list[str]] = Query(None),
        granularity: str = Query("hourly", pattern="^(hourly|daily)$"),
        tolerance_mode: str = Query("abs", pattern="^(abs|pct)$"),
        tol_density: Optional[float] = None,
        tol_co2: Optional[float] = None,
        tol_n2: Optional[float] = None,
        stale_after_hours: int = Query(DEFAULT_STALE_AFTER_HOURS, ge=1),
        allowed_branches: Optional[list[int]] = Depends(get_branch_filter),
        session: AsyncSession = Depends(get_session),
    ) -> RouteFhpReport:
        route = await _get_visible(route_id, allowed_branches, session)
        try:
            start = _parse_date(date_from)
            end = _parse_date(date_to)
        except ValueError:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "Невірний формат дати, очікується YYYY-MM-DD"
            )
        if start > end:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "Початок періоду пізніше за кінець"
            )

        tolerances = {
            "density": tol_density, "co2": tol_co2, "n2": tol_n2,
        }
        try:
            return await build_route_report(
                session,
                route,
                start,
                end,
                params or list(FHP_PARAMS),
                granularity=granularity,
                tolerances={k: v for k, v in tolerances.items() if v is not None},
                tolerance_mode=tolerance_mode,
                stale_after_hours=stale_after_hours,
            )
        except RouteReportError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))


# ── helpers ───────────────────────────────────────────────────────────────────


def _parse_date(value: str):
    return datetime.strptime(value.split(" ")[0].split("T")[0], "%Y-%m-%d").date()


def _check_number(data: GasRouteWrite) -> None:
    data.number = (data.number or "").strip()
    if not data.number:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Вкажіть номер маршруту")


def _duplicate_number(number: str) -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT, f"Маршрут № {number} уже існує в цій філії"
    )


async def _check_members(
    data: GasRouteWrite, session: AsyncSession, route_id: Optional[int]
) -> None:
    line_ids = [m.line_id for m in data.members]
    if len(set(line_ids)) != len(line_ids):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Одна й та сама лінія вказана двічі"
        )
    if not line_ids:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Додайте лінії до маршруту"
        )
    # A route exists to compare its lines against a reference, so a route
    # without one has nothing to say. The reference line is not necessarily
    # the one with a stream chromatograph — it is whichever line's ФХП is
    # taken as correct for this route.
    if not any(m.is_reference for m in data.members):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Позначте хоча б одну лінію як еталонну — без еталона немає з чим "
            "порівнювати",
        )

    # Every member must belong to the route's own branch: the route is the unit
    # access is granted on, and a line from elsewhere would be readable through it.
    rows = (await session.execute(
        select(Line.id, Line.name, Lumg.branch_id)
        .join(GasVolumeCalc, GasVolumeCalc.id == Line.gas_volume_calc_id)
        .join(Lumg, Lumg.id == GasVolumeCalc.lumg_id)
        .where(Line.id.in_(line_ids))
    )).all()
    known = {lid: (name, branch) for lid, name, branch in rows}
    for line_id in line_ids:
        if line_id not in known:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"Лінію {line_id} не знайдено"
            )
        name, branch = known[line_id]
        if branch != data.branch_id:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Лінія «{name}» не належить обраній філії",
            )

    clash = select(GasRouteMember.line_id, GasRoute.number).join(
        GasRoute, GasRoute.id == GasRouteMember.route_id
    ).where(GasRouteMember.line_id.in_(line_ids))
    if route_id is not None:
        clash = clash.where(GasRouteMember.route_id != route_id)
    taken = (await session.execute(clash)).all()
    if taken:
        line_id, other = taken[0]
        name = known[line_id][0]
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Лінія «{name}» уже входить до маршруту № {other}",
        )


async def _replace_members(
    route_id: int, data: GasRouteWrite, session: AsyncSession
) -> None:
    for order, member in enumerate(data.members):
        session.add(GasRouteMember(
            route_id=route_id,
            line_id=member.line_id,
            is_reference=member.is_reference,
            sort_order=order,
        ))
    await session.flush()


async def _get_visible(
    route_id: int, allowed_branches: Optional[list[int]], session: AsyncSession
) -> GasRoute:
    route = await session.get(GasRoute, route_id)
    if route is None or (
        allowed_branches is not None and route.branch_id not in allowed_branches
    ):
        # 404 and not 403: a viewer must not be able to tell an id that exists
        # elsewhere from one that does not exist at all.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Маршрут не знайдено")
    return route


async def _to_read(route: GasRoute, session: AsyncSession) -> GasRouteRead:
    members = await fhp_route_service.load_members(session, route.id)
    return GasRouteRead(
        id=route.id,
        branch_id=route.branch_id,
        number=route.number,
        name=route.name,
        description=route.description,
        active=route.active,
        members=[
            GasRouteMemberOut(
                id=m.member_id,
                line_id=m.line_id,
                line_name=m.line_name,
                is_reference=m.is_reference,
                sort_order=m.sort_order,
            )
            for m in members
        ],
    )


gas_route_router = GasRouteRouter().router
