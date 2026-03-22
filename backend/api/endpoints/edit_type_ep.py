from typing import Optional

from fastapi import APIRouter, HTTPException, status
from sqlmodel import select

from backend.db.dao.custom_exceptions import DatabaseIntegrityError
from backend.db.dao.edit_type_dao import EditTypeDao
from backend.db.engine import async_session_factory
from backend.db.models.edit_type_model import EditType, EditTypeCreate, EditTypeList, EditTypeUpdate

router = APIRouter(prefix="/edit-types", tags=["edit_type"])


@router.get("/", response_model=list[EditTypeList])
async def get_edit_types(calc_type_id: Optional[int] = None):
    async with async_session_factory() as session:
        query = select(EditType)
        if calc_type_id is not None:
            query = query.where(EditType.gas_volume_calc_type_id == calc_type_id)
        result = await session.execute(query.order_by(EditType.gas_volume_calc_type_id, EditType.edit_type_id))
        return result.scalars().all()


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
