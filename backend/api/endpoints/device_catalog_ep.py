"""
CRUD endpoints for device manufacturers and corrector model types.
"""
from typing import List

from fastapi import APIRouter, HTTPException, status
from backend.db.dao.custom_exceptions import DatabaseIntegrityError

from backend.db.engine import async_session_factory
from backend.db.dao.device_catalog_dao import ManufacturerDao, CorectorTypeDao
from backend.db.models.device_catalog_model import (
    ManufacturerRead, ManufacturerCreate, ManufacturerUpdate,
    CorectorTypeRead, CorectorTypeCreate, CorectorTypeUpdate,
)

router = APIRouter(prefix="/device-catalog", tags=["device_catalog"])


def _conflict_msg(e: DatabaseIntegrityError) -> str:
    msg = str(e)
    if 'uq_manufacturer_short_name' in msg:
        return "Виробник з такою скороченою назвою вже існує"
    if 'uq_manufacturer_full_name' in msg:
        return "Виробник з такою повною назвою вже існує"
    if 'uq_manufacturer_mf_dev' in msg:
        return "Виробник з таким кодом mf_dev вже існує"
    if 'uq_corector_type_mfr_model' in msg:
        return "Модель з такою назвою вже існує для цього виробника"
    return "Запис з такими даними вже існує"


# ─── Manufacturers ────────────────────────────────────────────────────────────

@router.get("/manufacturers/", response_model=List[ManufacturerRead])
async def list_manufacturers():
    async with async_session_factory() as session:
        return await ManufacturerDao(session).get_all()


@router.post("/manufacturers/", response_model=ManufacturerRead, status_code=201)
async def create_manufacturer(data: ManufacturerCreate):
    try:
        async with async_session_factory() as session:
            return await ManufacturerDao(session).create(data)
    except DatabaseIntegrityError as e:
        raise HTTPException(status_code=409, detail=_conflict_msg(e))


@router.patch("/manufacturers/{item_id}", response_model=ManufacturerRead)
async def update_manufacturer(item_id: int, data: ManufacturerUpdate):
    try:
        async with async_session_factory() as session:
            item = await ManufacturerDao(session).update(item_id, data)
    except DatabaseIntegrityError as e:
        raise HTTPException(status_code=409, detail=_conflict_msg(e))
    if not item:
        raise HTTPException(status_code=404, detail="Manufacturer not found")
    return item


@router.delete("/manufacturers/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_manufacturer(item_id: int):
    async with async_session_factory() as session:
        ok = await ManufacturerDao(session).delete(item_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Manufacturer not found")


# ─── Corrector types ──────────────────────────────────────────────────────────

@router.get("/corector-types/", response_model=List[CorectorTypeRead])
async def list_corector_types(manufacturer_id: int | None = None):
    async with async_session_factory() as session:
        dao = CorectorTypeDao(session)
        if manufacturer_id is not None:
            return await dao.get_by_manufacturer(manufacturer_id)
        return await dao.get_all()


@router.post("/corector-types/", response_model=CorectorTypeRead, status_code=201)
async def create_corector_type(data: CorectorTypeCreate):
    try:
        async with async_session_factory() as session:
            return await CorectorTypeDao(session).create(data)
    except DatabaseIntegrityError as e:
        raise HTTPException(status_code=409, detail=_conflict_msg(e))


@router.patch("/corector-types/{item_id}", response_model=CorectorTypeRead)
async def update_corector_type(item_id: int, data: CorectorTypeUpdate):
    try:
        async with async_session_factory() as session:
            item = await CorectorTypeDao(session).update(item_id, data)
    except DatabaseIntegrityError as e:
        raise HTTPException(status_code=409, detail=_conflict_msg(e))
    if not item:
        raise HTTPException(status_code=404, detail="Corector type not found")
    return item


@router.delete("/corector-types/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_corector_type(item_id: int):
    async with async_session_factory() as session:
        ok = await CorectorTypeDao(session).delete(item_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Corector type not found")
