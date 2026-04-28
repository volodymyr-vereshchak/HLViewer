from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, or_, cast, String
from sqlmodel import select

from backend.api.endpoints.auth_ep import get_current_user
from backend.db.dao.custom_exceptions import DatabaseIntegrityError
from backend.db.dao.sys_type_dao import SysTypeDao
from backend.db.engine import async_session_factory
from backend.db.models.sys_type_model import SysType, SysTypeCreate, SysTypeList, SysTypeUpdate

router = APIRouter(prefix="/sys-types", tags=["sys_type"], dependencies=[Depends(get_current_user)])


class PagedSysTypes(BaseModel):
    total: int
    items: list[SysTypeList]


@router.get("/", response_model=PagedSysTypes)
async def get_sys_types(
    calc_type_id: Optional[int] = None,
    search: Optional[str] = None,
    skip: int = 0,
    limit: int = 50,
):
    async with async_session_factory() as session:
        base_q = select(SysType)
        if calc_type_id is not None:
            base_q = base_q.where(SysType.gas_volume_calc_type_id == calc_type_id)
        if search:
            base_q = base_q.where(or_(
                SysType.sys_name.ilike(f"%{search}%"),
                cast(SysType.sys_type_id, String).ilike(f"%{search}%"),
            ))

        total = (await session.execute(select(func.count()).select_from(base_q.subquery()))).scalar_one()
        result = await session.execute(
            base_q.order_by(SysType.gas_volume_calc_type_id, SysType.sys_type_id)
            .offset(skip).limit(limit)
        )
        items = result.scalars().all()

    return {"total": total, "items": items}


@router.post("/", response_model=SysTypeList, status_code=status.HTTP_201_CREATED)
async def create_sys_type(body: SysTypeCreate):
    try:
        async with async_session_factory() as session:
            entry = await SysTypeDao(session=session).create_item(body)
    except DatabaseIntegrityError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return entry


@router.patch("/{sys_type_db_id}", response_model=SysTypeList)
async def update_sys_type(sys_type_db_id: int, body: SysTypeUpdate):
    async with async_session_factory() as session:
        entry = await SysTypeDao(session=session).update_by_id(sys_type_db_id, body)
    if not entry:
        raise HTTPException(status_code=404, detail="SysType not found")
    return entry


@router.delete("/{sys_type_db_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_sys_type(sys_type_db_id: int):
    async with async_session_factory() as session:
        deleted = await SysTypeDao(session=session).delete_item(sys_type_db_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="SysType not found")


sys_type_router = router
