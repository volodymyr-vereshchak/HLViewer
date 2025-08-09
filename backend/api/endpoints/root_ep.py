import asyncio

from fastapi import APIRouter, status, HTTPException

from backend.db.engine import DbEngine, async_session_factory
from backend.db.preload_db.preload_db import preload_db
from backend.hl_engine.main import update_hostlibs
from backend.hl_engine.hostlib_updater import HostlibUpdater
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
        self.router.add_api_route(
            path="/get_report/",
            endpoint=self.get_report,
            tags=["root"],
            methods=["GET"],
            status_code=status.HTTP_200_OK,
        )

        self.lock = asyncio.Lock()

    async def update_data(self):
        if self.lock.locked():
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Update is already in progress. Please try again later.",
            )

        async with self.lock:
            async with async_session_factory() as session:
                try:
                    await update_hostlibs(session=session)
                    return {"message": "Updated"}
                except Exception as e:
                    self.logger.error(
                        f"Unexpected error occurred while update_hostlibs: {e}",
                        exc_info=True,
                    )

    async def get_report(self):
        """Get gas volume report for the last 24 hours without updating hostlibs"""
        try:
            updater = HostlibUpdater()
            async with async_session_factory() as session:
                from backend.db.dao.hourly_archive_dao import HourlyArchiveDao
                from backend.db.models import HourlyArchiveList
                from datetime import timedelta
                import pandas as pd
                
                # Получаем данные за последние 24 часа
                end = await HourlyArchiveDao(session=session).get_last_period()
                start = end - timedelta(hours=23)
                result = await HourlyArchiveDao(session=session).get_range(
                    from_date=start, to_date=end
                )
                
                # Конвертируем в DataFrame
                extracted_data = [
                    HourlyArchiveList(**vars(item)).model_dump() for item in result
                ]
                df = pd.DataFrame(extracted_data).sort_values("period")
                
                # Создаем сообщение
                message = await updater.create_message(df)
                
                return {"message": message, "success": True}
                
        except Exception as e:
            self.logger.error(f"Error generating report: {e}", exc_info=True)
            return {"message": f"Ошибка при генерации отчета: {str(e)}", "success": False}


root_router = RootRouter().router
