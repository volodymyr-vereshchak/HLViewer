import asyncio
import logging
import os
import re
import zipfile
from concurrent.futures import ThreadPoolExecutor

_EIS_CODE_RE = re.compile(r'^[A-Z0-9]{6,}$')

#: How many archives are read at once when scanning for EIC folders. The cost
#: is the central directory of each file, so on a network share it is latency,
#: not work: eight in flight turns a scan of thousands of archives from a
#: queue into a stream. Beyond that the share, not the scanner, is the limit.
_SCAN_WORKERS = 8


def _folders_in(zip_path: str) -> set:
    """Every directory name inside one archive.

    The names are collected and matched afterwards: an archive holds ~1260
    entries and a handful of distinct folders, and matching each entry's
    segments would run the pattern a thousand times over the same few strings.
    """
    folders = set()
    with zipfile.ZipFile(zip_path, "r") as archive:
        for name in archive.namelist():
            # Everything but the last segment is a directory — and for a
            # directory entry, which ends in "/", the last segment is "".
            for segment in name.split("/")[:-1]:
                if segment:
                    folders.add(segment)
    return folders


def _scan_for_eis(root: str) -> list:
    """The EIC-coded folders under `root`, across every archive there.

    Blocking: reads every zip on the path, so it is called in a thread. It
    used to run on the event loop, where a share holding thousands of
    archives stopped every other request for as long as it took.
    """
    archives = [os.path.join(where, name)
                for where, _dirs, files in os.walk(root)
                for name in files if name.endswith(".zip")]
    folders = set()
    with ThreadPoolExecutor(_SCAN_WORKERS) as pool:
        for found in pool.map(_folders_in, archives):
            folders |= found
    return sorted(name for name in folders if _EIS_CODE_RE.match(name))

from fastapi import APIRouter, Depends, status, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from backend.api.endpoints.auth_ep import get_branch_filter
from backend.db.dao.custom_exceptions import DatabaseIntegrityError
from backend.db.dao.lumg_dao import LumgDao
from backend.db.engine import get_session
from backend.utils.path_utils import resolve_stored_path
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

    async def get_lumgs(
        self,
        branch_ids: list[int] | None = Depends(get_branch_filter),
        session: AsyncSession = Depends(get_session),
    ):
        if branch_ids is None:
            lumgs = await LumgDao(session=session).get_all()
        else:
            result = await session.execute(
                select(Lumg).where(Lumg.branch_id.in_(branch_ids))
            )
            lumgs = result.scalars().all()
        return lumgs

    async def create_lumg(self, lumg: LumgCreate, session: AsyncSession = Depends(get_session)):
        try:
            lumg = await LumgDao(session=session).create_item(lumg)
        except DatabaseIntegrityError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return lumg

    async def update_lumg(self, lumg_id: int, lumg: LumgUpdate, session: AsyncSession = Depends(get_session)):
        lumg_db = await LumgDao(session=session).update_by_id(lumg_id, lumg)
        if not lumg_db:
            raise HTTPException(status_code=404, detail="Lumg not found")
        return lumg_db

    async def delete_lumg(self, lumg_id: int, session: AsyncSession = Depends(get_session)):
        exists = await session.get(Lumg, lumg_id)
        if not exists:
            raise HTTPException(status_code=404, detail="Lumg not found")
        await session.execute(text("DELETE FROM lumg WHERE id = :id"), {"id": lumg_id})
        await session.commit()
        return {"ok": True}

    async def get_data_path(self, lumg_id: int, session: AsyncSession = Depends(get_session)):
        result = await session.execute(
            select(LumgDataPath).where(LumgDataPath.lumg_id == lumg_id)
        )
        data_path = result.scalars().first()
        if not data_path:
            raise HTTPException(status_code=404, detail="Data path not found")
        return data_path

    async def upsert_data_path(
        self, lumg_id: int, body: LumgDataPathUpsert, session: AsyncSession = Depends(get_session)
    ):
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

    async def delete_data_path(self, lumg_id: int, session: AsyncSession = Depends(get_session)):
        result = await session.execute(
            select(LumgDataPath).where(LumgDataPath.lumg_id == lumg_id)
        )
        data_path = result.scalars().first()
        if not data_path:
            raise HTTPException(status_code=404, detail="Data path not found")
        await session.delete(data_path)
        await session.commit()

    async def get_eis_codes(self, lumg_id: int, session: AsyncSession = Depends(get_session)):
        result = await session.execute(
            select(LumgEisCode).where(LumgEisCode.lumg_id == lumg_id)
        )
        return result.scalars().all()

    async def add_eis_code(
        self, lumg_id: int, body: LumgEisCodeCreate, session: AsyncSession = Depends(get_session)
    ):
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

    async def delete_eis_code(
        self, lumg_id: int, eis_code: str, session: AsyncSession = Depends(get_session)
    ):
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

    async def scan_eis(self, lumg_id: int, session: AsyncSession = Depends(get_session)):
        dp_result = await session.execute(
            select(LumgDataPath).where(LumgDataPath.lumg_id == lumg_id)
        )
        data_path = dp_result.scalars().first()
        if not data_path:
            raise HTTPException(status_code=404, detail="Data path not set for this LUMG")
        resolved = resolve_stored_path(data_path.path)
        if not resolved.exists():
            raise HTTPException(status_code=400, detail=f"Path does not exist: {data_path.path}")
        try:
            return await asyncio.to_thread(_scan_for_eis, str(resolved))
        except Exception as e:
            logger.error(f"Error scanning EIS codes for lumg_id={lumg_id}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))


lumg_router = LumgRouter().router
