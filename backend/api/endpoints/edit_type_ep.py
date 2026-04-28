from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, or_, cast, String
from sqlmodel import select

from backend.api.endpoints.auth_ep import get_current_user
from backend.db.dao.custom_exceptions import DatabaseIntegrityError
from backend.db.dao.edit_type_dao import EditTypeDao
from backend.db.engine import async_session_factory
from backend.db.models.edit_type_model import EditType, EditTypeCreate, EditTypeList, EditTypeUpdate

router = APIRouter(prefix="/edit-types", tags=["edit_type"], dependencies=[Depends(get_current_user)])


class PagedEditTypes(BaseModel):
    total: int
    items: list[EditTypeList]


@router.get("/", response_model=PagedEditTypes)
async def get_edit_types(
    calc_type_id: Optional[int] = None,
    search: Optional[str] = None,
    skip: int = 0,
    limit: int = 50,
):
    async with async_session_factory() as session:
        base_q = select(EditType)
        if calc_type_id is not None:
            base_q = base_q.where(EditType.gas_volume_calc_type_id == calc_type_id)
        if search:
            base_q = base_q.where(or_(
                EditType.edit_name.ilike(f"%{search}%"),
                cast(EditType.edit_type_id, String).ilike(f"%{search}%"),
            ))

        total = (await session.execute(select(func.count()).select_from(base_q.subquery()))).scalar_one()
        result = await session.execute(
            base_q.order_by(EditType.gas_volume_calc_type_id, EditType.edit_type_id)
            .offset(skip).limit(limit)
        )
        items = result.scalars().all()

    return {"total": total, "items": items}


@router.post("/", response_model=EditTypeList, status_code=status.HTTP_201_CREATED)
async def create_edit_type(body: EditTypeCreate):
    try:
        async with async_session_factory() as session:
            entry = await EditTypeDao(session=session).create_item(body)
    except DatabaseIntegrityError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return entry


@router.patch("/{edit_type_db_id}", response_model=EditTypeList)
async def update_edit_type(edit_type_db_id: int, body: EditTypeUpdate):
    async with async_session_factory() as session:
        entry = await EditTypeDao(session=session).update_by_id(edit_type_db_id, body)
    if not entry:
        raise HTTPException(status_code=404, detail="EditType not found")
    return entry


@router.delete("/{edit_type_db_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_edit_type(edit_type_db_id: int):
    async with async_session_factory() as session:
        deleted = await EditTypeDao(session=session).delete_item(edit_type_db_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="EditType not found")


edit_type_router = router
