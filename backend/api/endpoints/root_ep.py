import asyncio

from fastapi import APIRouter, status

from backend.db.engine import DbEngine, async_session_factory
from backend.db.preload_db.preload_db import preload_db
from backend.hl_engine.main import update_hostlibs
from utils.logger import logger_setup


class RootRouter:
    def __init__(self):
        self.router = APIRouter()
        self.logger = logger_setup("backend")
        self.router.add_api_route(
            path="/update_data/",
            endpoint=self.update_data,
            tags=["root"],
            methods=["POST"],
            status_code=status.HTTP_202_ACCEPTED,
        )
        self.router.add_api_route(
            path="/preload_data/",
            endpoint=preload_db,
            tags=["root"],
            methods=["POST"],
            status_code=status.HTTP_201_CREATED,
        )

        self.lock = asyncio.Lock()

    async def update_data(self):
        if self.lock.locked():
            return {"message": "Update is already in progress. Please try again later."}

        async with self.lock:
            async with async_session_factory() as session:
                try:
                    await update_hostlibs(session=session)
                except Exception as e:
                    self.logger.error(
                        f"Unexpected error occurred while update_hostlibs: {e}",
                        exc_info=True,
                    )


root_router = RootRouter().router
