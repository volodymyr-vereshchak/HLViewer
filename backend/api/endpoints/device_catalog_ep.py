"""
CRUD endpoints for device manufacturers and corrector model types.
"""
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from backend.db.dao.custom_exceptions import DatabaseIntegrityError

from backend.db.engine import get_session
from backend.db.dao.device_catalog_dao import ManufacturerDao, CorectorTypeDao
from backend.db.models.device_catalog_model import (
    Manufacturer, CorectorType,
    ManufacturerRead, ManufacturerCreate, ManufacturerUpdate,
    CorectorTypeRead, CorectorTypeCreate, CorectorTypeUpdate,
)
from backend.api.endpoints.auth_ep import get_current_user, require_admin
from backend.db.models.app_user_model import AppUser
from backend.db.preload_db.preload_device_catalog import preload as _preload_catalog
from backend.db.preload_db.export_device_catalog import export as _export_catalog

router = APIRouter(prefix="/device-catalog", tags=["device_catalog"], dependencies=[Depends(get_current_user)])


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
async def list_manufacturers(session: AsyncSession = Depends(get_session)):
    return await ManufacturerDao(session).get_all()


@router.post("/manufacturers/", response_model=ManufacturerRead, status_code=201)
async def create_manufacturer(data: ManufacturerCreate, session: AsyncSession = Depends(get_session)):
    try:
        return await ManufacturerDao(session).create(data)
    except DatabaseIntegrityError as e:
        raise HTTPException(status_code=409, detail=_conflict_msg(e))


@router.patch("/manufacturers/{item_id}", response_model=ManufacturerRead)
async def update_manufacturer(
    item_id: int, data: ManufacturerUpdate, session: AsyncSession = Depends(get_session)
):
    try:
        item = await ManufacturerDao(session).update(item_id, data)
    except DatabaseIntegrityError as e:
        raise HTTPException(status_code=409, detail=_conflict_msg(e))
    if not item:
        raise HTTPException(status_code=404, detail="Manufacturer not found")
    return item


@router.delete("/manufacturers/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_manufacturer(item_id: int, session: AsyncSession = Depends(get_session)):
    ok = await ManufacturerDao(session).delete(item_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Manufacturer not found")


# ─── Corrector types ──────────────────────────────────────────────────────────

@router.get("/corector-types/", response_model=List[CorectorTypeRead])
async def list_corector_types(
    manufacturer_id: int | None = None, session: AsyncSession = Depends(get_session)
):
    dao = CorectorTypeDao(session)
    if manufacturer_id is not None:
        return await dao.get_by_manufacturer(manufacturer_id)
    return await dao.get_all()


@router.post("/corector-types/", response_model=CorectorTypeRead, status_code=201)
async def create_corector_type(data: CorectorTypeCreate, session: AsyncSession = Depends(get_session)):
    try:
        return await CorectorTypeDao(session).create(data)
    except DatabaseIntegrityError as e:
        raise HTTPException(status_code=409, detail=_conflict_msg(e))


@router.patch("/corector-types/{item_id}", response_model=CorectorTypeRead)
async def update_corector_type(
    item_id: int, data: CorectorTypeUpdate, session: AsyncSession = Depends(get_session)
):
    try:
        item = await CorectorTypeDao(session).update(item_id, data)
    except DatabaseIntegrityError as e:
        raise HTTPException(status_code=409, detail=_conflict_msg(e))
    if not item:
        raise HTTPException(status_code=404, detail="Corector type not found")
    return item


@router.delete("/corector-types/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_corector_type(item_id: int, session: AsyncSession = Depends(get_session)):
    ok = await CorectorTypeDao(session).delete(item_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Corector type not found")


@router.post("/export-preload", status_code=status.HTTP_200_OK)
async def export_preload_json(
    user: AppUser = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    manufacturers = (await session.execute(select(Manufacturer))).scalars().all()
    corector_types = (await session.execute(select(CorectorType))).scalars().all()
    await _export_catalog(session)
    return {"ok": True, "exported": {
        "manufacturers": len(manufacturers),
        "corector_types": len(corector_types),
    }}


@router.post("/preload", status_code=status.HTTP_200_OK)
async def preload_device_catalog(force: bool = False, session: AsyncSession = Depends(get_session)):
    if force:
        # Clear existing data before reload
        existing_types = await session.execute(select(CorectorType))
        for ct in existing_types.scalars().all():
            await session.delete(ct)
        existing_mfr = await session.execute(select(Manufacturer))
        for mfr in existing_mfr.scalars().all():
            await session.delete(mfr)
        await session.commit()
    await _preload_catalog(session)
    return {"message": "Каталог передзавантажено" if force else "Каталог оновлено (додано відсутні записи, наявні збережено)"}
