"""
CRUD endpoints for device manufacturers and corrector model types.
"""
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
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
from backend.db.preload_db.preload_device_catalog import (
    CatalogSync, preload as _preload_catalog,
)
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
    try:
        ok = await ManufacturerDao(session).delete(item_id)
    except DatabaseIntegrityError:
        raise HTTPException(
            status_code=409,
            detail=(
                "Виробника не можна видалити: на його моделі посилаються "
                "прилади. Спершу приберіть або переназначте ці моделі"
            ),
        )
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
    try:
        ok = await CorectorTypeDao(session).delete(item_id)
    except DatabaseIntegrityError:
        # dpd_device and dpd_line_device reference the type with RESTRICT: it
        # is the identity a corrector is polled by, so dropping it would leave
        # devices unaddressable and their archives orphaned.
        raise HTTPException(
            status_code=409,
            detail=(
                "Модель не можна видалити: на неї посилаються прилади "
                "підприємств або ліній ДПД"
            ),
        )
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


def _sync_message(report: CatalogSync) -> str:
    if not report.changed:
        return "Каталог уже збігається з файлом — змін немає"
    parts = []
    if report.added_manufacturers:
        parts.append(f"додано виробників: {report.added_manufacturers}")
    if report.renamed_manufacturers:
        parts.append(f"перейменовано виробників: {report.renamed_manufacturers}")
    if report.added_models:
        parts.append(f"додано моделей: {report.added_models}")
    return "Каталог оновлено — " + ", ".join(parts)


@router.post("/preload", status_code=status.HTTP_200_OK)
async def preload_device_catalog(force: bool = False, session: AsyncSession = Depends(get_session)):
    if force:
        # Clear existing data before reload. The whole block is guarded, not
        # just the commit: deleting the types makes the next SELECT autoflush,
        # so the violation surfaces before any commit is reached.
        try:
            existing_types = await session.execute(select(CorectorType))
            for ct in existing_types.scalars().all():
                await session.delete(ct)
            existing_mfr = await session.execute(select(Manufacturer))
            for mfr in existing_mfr.scalars().all():
                await session.delete(mfr)
            await session.commit()
        except IntegrityError:
            # Models are referenced by dpd_device / dpd_line_device with
            # RESTRICT. Wiping the catalog would orphan real devices, so the
            # DB refuses — say why instead of returning a 500. The normal
            # merge below handles renames anyway (viz. mf_dev), so a wipe is
            # almost never what the admin actually needs.
            await session.rollback()
            raise HTTPException(
                status_code=409,
                detail=(
                    "Каталог не можна перезаписати: на його моделі посилаються "
                    "прилади підприємств або ліній ДПД. Зніміть позначку "
                    "«перезаписати» — злиття оновить назви виробників за кодом "
                    "mf_dev і додасть відсутні моделі"
                ),
            )
    report = await _preload_catalog(session)
    message = "Каталог передзавантажено" if force else _sync_message(report)
    return {
        "message": message,
        "added_manufacturers": report.added_manufacturers,
        "renamed_manufacturers": report.renamed_manufacturers,
        "added_models": report.added_models,
        "warnings": report.warnings,
    }
