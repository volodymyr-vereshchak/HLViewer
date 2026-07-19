"""
DPD lines API: CRUD with device history, init (full reload) and archives.

A DPD line's data comes from the DPD API through its device (corrector)
history — see backend/services/dpd_line_refresh.py. Mutations are
admin-only via the global auth_guard middleware; reads are branch-scoped
through get_branch_filter. Archive endpoints (/daily_dpd/, /hourly_dpd/)
return rows in the same shape as /daily_virtual/ // /hourly_virtual/ so
the frontend trends consume all three line kinds identically.
"""

import asyncio
import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from backend.api.endpoints.auth_ep import get_branch_filter, get_current_user
from backend.db.engine import get_session
from backend.db.dao.dpd_line_dao import DpdLineArchiveDao, DpdLineDao
from backend.db.models.device_catalog_model import CorectorType
from backend.db.models.dpd_line_model import (
    DpdLine,
    DpdLineCreate,
    DpdLineDevice,
    DpdLineDeviceIn,
    DpdLineDeviceRead,
    DpdLineList,
)
from backend.services import dpd_line_refresh

logger = logging.getLogger(__name__)

# Strong refs to detached init tasks (bare create_task results may be
# garbage-collected before completion).
_init_tasks: set = set()


def _normalize_devices(devices: List[DpdLineDeviceIn]) -> List[DpdLineDeviceIn]:
    """Force installed_from to hour precision and reject duplicates."""
    seen = set()
    for dev in devices:
        dev.installed_from = dev.installed_from.replace(
            minute=0, second=0, microsecond=0
        )
        if dev.installed_from in seen:
            raise HTTPException(
                status_code=400,
                detail="Два прилади не можуть мати однакову дату встановлення",
            )
        seen.add(dev.installed_from)
    return devices


async def _check_corector_types(
    devices: List[DpdLineDeviceIn], session: AsyncSession
) -> None:
    ct_ids = {d.corector_type_id for d in devices}
    if not ct_ids:
        return
    found = set((await session.execute(
        select(CorectorType.id).where(CorectorType.id.in_(ct_ids))
    )).scalars())
    missing = ct_ids - found
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Невідомий тип коректора: {sorted(missing)}",
        )


async def _line_to_list(line: DpdLine, session: AsyncSession) -> DpdLineList:
    """Build the read model: devices resolved through the catalog, with the
    derived window end (next device's installed_from)."""
    resolved = await DpdLineDao(session).get_devices_resolved(line.id)
    devices = []
    for i, dev in enumerate(resolved):
        bound_to = (
            resolved[i + 1]["installed_from"] if i + 1 < len(resolved) else None
        )
        devices.append(DpdLineDeviceRead(
            id=dev["id"],
            ser_num=dev["serNum"],
            corector_type_id=dev["corector_type_id"],
            ch_num=dev["chNum"],
            installed_from=dev["installed_from"],
            mf_dev=dev["mfDev"],
            type_dev=dev["typeDev"],
            model_name=dev["model_name"],
            manufacturer_short_name=dev["manufacturer_short_name"],
            bound_to=bound_to,
        ))
    return DpdLineList(
        **line.model_dump(exclude={"devices"}),
        devices=devices,
    )


class DpdLineRouter:
    """Router for DPD line endpoints."""

    def __init__(self):
        self.router = APIRouter(dependencies=[Depends(get_current_user)])

        self.router.add_api_route(
            path="/dpd_lines/",
            tags=["dpd_lines"],
            endpoint=self.get_all,
            methods=["GET"],
            response_model=List[DpdLineList],
            status_code=status.HTTP_200_OK,
            summary="List DPD lines with device history",
        )
        self.router.add_api_route(
            path="/dpd_lines/",
            tags=["dpd_lines"],
            endpoint=self.create,
            methods=["POST"],
            response_model=DpdLineList,
            status_code=status.HTTP_201_CREATED,
            summary="Create DPD line",
        )
        self.router.add_api_route(
            path="/dpd_lines/{dl_id}",
            tags=["dpd_lines"],
            endpoint=self.update,
            methods=["PATCH"],
            response_model=DpdLineList,
            status_code=status.HTTP_200_OK,
            summary="Update DPD line (replaces the device history)",
        )
        self.router.add_api_route(
            path="/dpd_lines/{dl_id}",
            tags=["dpd_lines"],
            endpoint=self.delete_line,
            methods=["DELETE"],
            status_code=status.HTTP_204_NO_CONTENT,
            summary="Delete DPD line (cascades devices, archives, job)",
        )
        self.router.add_api_route(
            path="/dpd_lines/{dl_id}/init",
            tags=["dpd_lines"],
            endpoint=self.init_line,
            methods=["POST"],
            status_code=status.HTTP_202_ACCEPTED,
            summary="Initialize the line's archives from the DPD API",
            description=(
                "Wipes the line's daily+hourly archives and re-polls every "
                "device's full validity window from DPD. 409 when a job for "
                "this line is already running. Admin-only (POST)."
            ),
        )
        self.router.add_api_route(
            path="/dpd_lines/{dl_id}/init/status",
            tags=["dpd_lines"],
            endpoint=self.init_status,
            methods=["GET"],
            summary="Init/update job status of the line",
        )
        self.router.add_api_route(
            path="/daily_dpd/",
            tags=["dpd_lines"],
            endpoint=self.get_daily_archive,
            methods=["GET"],
            response_model=List[dict],
            status_code=status.HTTP_200_OK,
            summary="Daily archives of DPD lines",
        )
        self.router.add_api_route(
            path="/hourly_dpd/",
            tags=["dpd_lines"],
            endpoint=self.get_hourly_archive,
            methods=["GET"],
            response_model=List[dict],
            status_code=status.HTTP_200_OK,
            summary="Hourly archives of DPD lines",
        )

    # ── CRUD ──────────────────────────────────────────────────────────────────

    async def get_all(
        self,
        branch_id: Optional[int] = None,
        include_in_trends: Optional[bool] = None,
        active: Optional[bool] = None,
        allowed_branches: Optional[list[int]] = Depends(get_branch_filter),
        session: AsyncSession = Depends(get_session),
    ) -> List[DpdLineList]:
        branch_ids = None
        if branch_id is not None:
            branch_ids = [branch_id]
        if allowed_branches is not None:
            branch_ids = (
                [b for b in branch_ids if b in allowed_branches]
                if branch_ids is not None else allowed_branches
            )
        lines = await DpdLineDao(session).get_all(
            branch_ids=branch_ids,
            include_in_trends=include_in_trends,
            active=active,
        )
        return [await _line_to_list(line, session) for line in lines]

    async def create(
        self, data: DpdLineCreate, session: AsyncSession = Depends(get_session)
    ) -> DpdLineList:
        devices = _normalize_devices(data.devices)
        await _check_corector_types(devices, session)
        line = DpdLine(**data.model_dump(exclude={"devices"}))
        session.add(line)
        await session.flush()
        for dev in devices:
            session.add(DpdLineDevice(dpd_line_id=line.id, **dev.model_dump()))
        await session.commit()
        await session.refresh(line)
        return await _line_to_list(line, session)

    async def update(
        self,
        dl_id: int,
        data: DpdLineCreate,
        session: AsyncSession = Depends(get_session),
    ) -> DpdLineList:
        line = await session.get(DpdLine, dl_id)
        if not line:
            raise HTTPException(status_code=404, detail="DPD line not found")
        devices = _normalize_devices(data.devices)
        await _check_corector_types(devices, session)
        for k, v in data.model_dump(exclude={"devices"}).items():
            setattr(line, k, v)
        await session.execute(
            sa_delete(DpdLineDevice).where(DpdLineDevice.dpd_line_id == dl_id)
        )
        for dev in devices:
            session.add(DpdLineDevice(dpd_line_id=dl_id, **dev.model_dump()))
        await session.commit()
        await session.refresh(line)
        return await _line_to_list(line, session)

    async def delete_line(
        self, dl_id: int, session: AsyncSession = Depends(get_session)
    ):
        line = await session.get(DpdLine, dl_id)
        if not line:
            raise HTTPException(status_code=404, detail="DPD line not found")
        await session.delete(line)
        await session.commit()

    # ── init / status ─────────────────────────────────────────────────────────

    async def init_line(
        self, dl_id: int, session: AsyncSession = Depends(get_session)
    ) -> dict:
        line = await session.get(DpdLine, dl_id)
        if not line:
            raise HTTPException(status_code=404, detail="DPD line not found")
        devices = await DpdLineDao(session).get_devices_resolved(dl_id)
        if not devices:
            raise HTTPException(
                status_code=400,
                detail="У лінії немає жодного приладу — додайте прилад перед "
                       "ініціалізацією",
            )
        if not await dpd_line_refresh.acquire(dl_id, "init"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Оновлення цієї лінії вже виконується",
            )
        # Lock is held; run detached (same lifecycle as the scheduler run).
        task = asyncio.create_task(dpd_line_refresh.init_line_locked(dl_id))
        _init_tasks.add(task)
        task.add_done_callback(_init_tasks.discard)
        return {"started": True}

    async def init_status(self, dl_id: int) -> dict:
        return await dpd_line_refresh.read_status(dl_id)

    # ── archives ──────────────────────────────────────────────────────────────

    async def _get_archive(
        self,
        period_type: str,
        max_days: int,
        from_date: datetime,
        to_date: datetime,
        line_id: Optional[List[int]],
        allowed_branches: Optional[list[int]],
        session: AsyncSession,
    ) -> List[dict]:
        if not from_date or not to_date:
            raise HTTPException(
                status_code=400, detail="from_date and to_date are required"
            )
        if (to_date - from_date).days > max_days:
            raise HTTPException(
                status_code=400, detail=f"Date range exceeds {max_days} days"
            )
        if not line_id:
            return []
        # Scope the requested IDs to DPD lines the user may see.
        stmt = select(DpdLine.id).where(DpdLine.id.in_(line_id))
        if allowed_branches is not None:
            stmt = stmt.where(DpdLine.branch_id.in_(allowed_branches))
        visible_ids = list((await session.execute(stmt)).scalars())
        rows = await DpdLineArchiveDao(session).load_range(
            period_type, visible_ids, from_date, to_date
        )
        return [
            {
                "line_id": r["dpd_line_id"],
                # Daily 'day' is a date — serialize as midnight datetime for
                # consistency with the physical/virtual archive endpoints.
                "period": (
                    datetime.combine(r["stamp"], datetime.min.time())
                    if period_type == "daily" else r["stamp"]
                ),
                "volume": r["volume"],
                "w_volume_dp": None,
                "pressure": r["pressure"],
                "temperature": r["temperature"],
                "density": None,
            }
            for r in rows
        ]

    async def get_daily_archive(
        self,
        from_date: datetime = Query(None, description="Start date/time"),
        to_date: datetime = Query(None, description="End date/time"),
        line_id: List[int] = Query(None, description="DPD line IDs"),
        allowed_branches: Optional[list[int]] = Depends(get_branch_filter),
        session: AsyncSession = Depends(get_session),
    ) -> List[dict]:
        return await self._get_archive(
            "daily", 400, from_date, to_date, line_id, allowed_branches, session
        )

    async def get_hourly_archive(
        self,
        from_date: datetime = Query(None, description="Start date/time"),
        to_date: datetime = Query(None, description="End date/time"),
        line_id: List[int] = Query(None, description="DPD line IDs"),
        allowed_branches: Optional[list[int]] = Depends(get_branch_filter),
        session: AsyncSession = Depends(get_session),
    ) -> List[dict]:
        return await self._get_archive(
            "hourly", 90, from_date, to_date, line_id, allowed_branches, session
        )


dpd_line_router = DpdLineRouter().router
