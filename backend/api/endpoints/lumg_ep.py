import logging
import os
import re
import zipfile

_EIS_CODE_RE = re.compile(r'^[A-Z0-9]{6,}$')

from fastapi import APIRouter, Depends, status, HTTPException
from sqlalchemy import text
from sqlmodel import select

from backend.api.endpoints.auth_ep import get_branch_filter
from backend.db.dao.custom_exceptions import DatabaseIntegrityError
from backend.db.dao.lumg_dao import LumgDao
from backend.db.engine import DbEngine, async_session_factory
from backend.db.models import LumgCreate, LumgList
from backend.db.models.lumg_model import (
    Lumg, LumgUpdate, LumgDataPath, LumgDataPathRead, LumgDataPathUpsert,
    LumgEisCode, LumgEisCodeRead, LumgEisCodeCreate,
)
from utils.files_utils import UnzipUtils

logger = logging.getLogger(__name__)


class LumgRouter:
    def __init__(self):
        self.router = APIRouter()
        self.router.add_api_route(
            path="/lumgs/",
            tags=["lumg"],
            endpoint=self.get_lumgs,
            methods=["GET"],
            response_model=list[LumgList],
            status_code=status.HTTP_200_OK,
        )
        self.router.add_api_route(
            path="/lumgs/",
            tags=["lumg"],
            endpoint=self.create_lumg,
            methods=["POST"],
            response_model=LumgCreate,
            status_code=status.HTTP_201_CREATED,
        )
        self.router.add_api_route(
            path="/lumgs/{lumg_id}",
            tags=["lumg"],
            endpoint=self.update_lumg,
            methods=["PATCH"],
            response_model=LumgList,
            status_code=status.HTTP_202_ACCEPTED,
        )
        self.router.add_api_route(
            path="/lumgs/{lumg_id}",
            tags=["lumg"],
            endpoint=self.delete_lumg,
            methods=["DELETE"],
            status_code=status.HTTP_204_NO_CONTENT,
        )
        self.router.add_api_route(
            path="/lumgs/{lumg_id}/data-path",
            tags=["lumg"],
            endpoint=self.get_data_path,
            methods=["GET"],
            response_model=LumgDataPathRead,
            status_code=status.HTTP_200_OK,
        )
        self.router.add_api_route(
            path="/lumgs/{lumg_id}/data-path",
            tags=["lumg"],
            endpoint=self.upsert_data_path,
            methods=["PUT"],
            response_model=LumgDataPathRead,
            status_code=status.HTTP_200_OK,
        )
        self.router.add_api_route(
            path="/lumgs/{lumg_id}/data-path",
            tags=["lumg"],
            endpoint=self.delete_data_path,
            methods=["DELETE"],
            status_code=status.HTTP_204_NO_CONTENT,
        )
        self.router.add_api_route(
            path="/lumgs/{lumg_id}/eis-codes",
            tags=["lumg"],
            endpoint=self.get_eis_codes,
            methods=["GET"],
            response_model=list[LumgEisCodeRead],
            status_code=status.HTTP_200_OK,
        )
        self.router.add_api_route(
            path="/lumgs/{lumg_id}/eis-codes",
            tags=["lumg"],
            endpoint=self.add_eis_code,
            methods=["POST"],
            response_model=LumgEisCodeRead,
            status_code=status.HTTP_201_CREATED,
        )
        self.router.add_api_route(
            path="/lumgs/{lumg_id}/eis-codes/{eis_code}",
            tags=["lumg"],
            endpoint=self.delete_eis_code,
            methods=["DELETE"],
            status_code=status.HTTP_204_NO_CONTENT,
        )
        self.router.add_api_route(
            path="/lumgs/{lumg_id}/scan-eis",
            tags=["lumg"],
            endpoint=self.scan_eis,
            methods=["GET"],
            status_code=status.HTTP_200_OK,
        )

    async def get_lumgs(self, branch_ids: list[int] | None = Depends(get_branch_filter)):
        async with async_session_factory() as session:
            if branch_ids is None:
                lumgs = await LumgDao(session=session).get_all()
            else:
                result = await session.execute(
                    select(Lumg).where(Lumg.branch_id.in_(branch_ids))
                )
                lumgs = result.scalars().all()
        return lumgs

    async def create_lumg(self, lumg: LumgCreate):
        try:
            async with async_session_factory() as session:
                lumg = await LumgDao(session=session).create_item(lumg)
        except DatabaseIntegrityError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return lumg

    async def update_lumg(self, lumg_id: int, lumg: LumgUpdate):
        async with async_session_factory() as session:
            lumg_db = await LumgDao(session=session).update_by_id(lumg_id, lumg)
        if not lumg_db:
            raise HTTPException(status_code=404, detail="Lumg not found")
        return lumg_db

    async def delete_lumg(self, lumg_id: int):
        async with async_session_factory() as session:
            exists = await session.get(Lumg, lumg_id)
            if not exists:
                raise HTTPException(status_code=404, detail="Lumg not found")
            await session.execute(text("DELETE FROM lumg WHERE id = :id"), {"id": lumg_id})
            await session.commit()
        return {"ok": True}

    async def get_data_path(self, lumg_id: int):
        async with async_session_factory() as session:
            result = await session.execute(
                select(LumgDataPath).where(LumgDataPath.lumg_id == lumg_id)
            )
            data_path = result.scalars().first()
        if not data_path:
            raise HTTPException(status_code=404, detail="Data path not found")
        return data_path

    async def upsert_data_path(self, lumg_id: int, body: LumgDataPathUpsert):
        async with async_session_factory() as session:
            result = await session.execute(
                select(LumgDataPath).where(LumgDataPath.lumg_id == lumg_id)
            )
            data_path = result.scalars().first()
            if data_path:
                data_path.path = body.path
                data_path.active = body.active
            else:
                data_path = LumgDataPath(lumg_id=lumg_id, path=body.path, active=body.active)
                session.add(data_path)
            await session.commit()
            await session.refresh(data_path)
        return data_path

    async def delete_data_path(self, lumg_id: int):
        async with async_session_factory() as session:
            result = await session.execute(
                select(LumgDataPath).where(LumgDataPath.lumg_id == lumg_id)
            )
            data_path = result.scalars().first()
            if not data_path:
                raise HTTPException(status_code=404, detail="Data path not found")
            await session.delete(data_path)
            await session.commit()

    async def get_eis_codes(self, lumg_id: int):
        async with async_session_factory() as session:
            result = await session.execute(
                select(LumgEisCode).where(LumgEisCode.lumg_id == lumg_id)
            )
            return result.scalars().all()

    async def add_eis_code(self, lumg_id: int, body: LumgEisCodeCreate):
        async with async_session_factory() as session:
            # Check for duplicate
            existing = await session.execute(
                select(LumgEisCode).where(LumgEisCode.eis_code == body.eis_code)
            )
            if existing.scalars().first():
                raise HTTPException(status_code=409, detail=f"EIS code '{body.eis_code}' already assigned")
            entry = LumgEisCode(lumg_id=lumg_id, eis_code=body.eis_code)
            session.add(entry)
            await session.commit()
            await session.refresh(entry)
        return entry

    async def delete_eis_code(self, lumg_id: int, eis_code: str):
        async with async_session_factory() as session:
            result = await session.execute(
                select(LumgEisCode).where(
                    LumgEisCode.lumg_id == lumg_id,
                    LumgEisCode.eis_code == eis_code,
                )
            )
            entry = result.scalars().first()
            if not entry:
                raise HTTPException(status_code=404, detail="EIS code not found")
            await session.delete(entry)
            await session.commit()

    async def scan_eis(self, lumg_id: int):
        async with async_session_factory() as session:
            dp_result = await session.execute(
                select(LumgDataPath).where(LumgDataPath.lumg_id == lumg_id)
            )
            data_path = dp_result.scalars().first()
        if not data_path:
            raise HTTPException(status_code=404, detail="Data path not set for this LUMG")
        if not os.path.exists(data_path.path):
            raise HTTPException(status_code=400, detail=f"Path does not exist: {data_path.path}")
        try:
            folder_names = set()
            for root, _, files in os.walk(data_path.path):
                for file in files:
                    if not file.endswith(".zip"):
                        continue
                    zip_path = os.path.join(root, file)
                    with zipfile.ZipFile(zip_path, "r") as zf:
                        for name in zf.namelist():
                            parts = [p for p in name.split("/") if p]
                            # Check all directory segments (all but last for files,
                            # all for directory entries ending with '/')
                            segments = parts if name.endswith("/") else parts[:-1]
                            for seg in segments:
                                if _EIS_CODE_RE.match(seg):
                                    folder_names.add(seg)
            return sorted(folder_names)
        except Exception as e:
            logger.error(f"Error scanning EIS codes for lumg_id={lumg_id}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))


lumg_router = LumgRouter().router
