from fastapi import APIRouter, status, HTTPException
from sqlmodel import select

from backend.db.dao.custom_exceptions import DatabaseIntegrityError
from backend.db.dao.lumg_dao import LumgDao
from backend.db.engine import DbEngine, async_session_factory
from backend.db.models import LumgCreate, LumgList
from backend.db.models.lumg_model import LumgUpdate, LumgDataPath, LumgDataPathRead, LumgDataPathUpsert


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

    async def get_lumgs(self):
        async with async_session_factory() as session:
            lumgs = await LumgDao(session=session).get_all()
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
            delete_lumg = await LumgDao(session=session).delete_item(lumg_id)
        if not delete_lumg:
            raise HTTPException(status_code=404, detail="Lumg not found")
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


lumg_router = LumgRouter().router
