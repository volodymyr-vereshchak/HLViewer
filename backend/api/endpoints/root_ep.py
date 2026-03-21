import asyncio
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, status, HTTPException

from backend.db.engine import async_session_factory
from backend.db.preload_db.preload_db import preload_db
from backend.hl_engine.main import update_hostlibs
from backend.hl_engine.hostlib_updater import HostlibUpdater
from utils.logger import logger_setup

# Shared job state — single update at a time
_job: dict = {"status": "idle", "started_at": None, "finished_at": None, "error": None, "lumg_id": None, "lumgs": {}}


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
            path="/update_data/status",
            endpoint=self.update_status,
            tags=["root"],
            methods=["GET"],
            status_code=status.HTTP_200_OK,
        )
        self.router.add_api_route(
            path="/update_data/{lumg_id}",
            endpoint=self.update_data_for_lumg,
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

    async def _run_update_all(self):
        global _job
        _job["started_at"] = datetime.now().isoformat()
        _job["finished_at"] = None
        _job["error"] = None
        _job["lumgs"] = {}
        try:
            async with async_session_factory() as session:
                await update_hostlibs(session=session, progress=_job["lumgs"])
            _job["status"] = "done"
        except Exception as e:
            self.logger.error(f"Background update_hostlibs error: {e}", exc_info=True)
            _job["status"] = "error"
            _job["error"] = str(e)
        finally:
            _job["finished_at"] = datetime.now().isoformat()

    async def update_data(self, background_tasks: BackgroundTasks):
        global _job
        if _job["status"] == "running":
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Update is already in progress. Please try again later.",
            )
        _job["status"] = "running"
        background_tasks.add_task(self._run_update_all)
        return {"message": "Update started", "status": "running", "started_at": datetime.now().isoformat()}

    async def update_status(self):
        return _job

    async def _run_update_lumg(self, lumg_id: int):
        global _job
        _job["started_at"] = datetime.now().isoformat()
        _job["finished_at"] = None
        _job["error"] = None
        _job["lumg_id"] = lumg_id
        _job["lumgs"] = {}
        try:
            async with async_session_factory() as session:
                await update_hostlibs(session=session, lumg_id=lumg_id, progress=_job["lumgs"])
            _job["status"] = "done"
        except Exception as e:
            self.logger.error(f"Background update_hostlibs error for lumg {lumg_id}: {e}", exc_info=True)
            _job["status"] = "error"
            _job["error"] = str(e)
        finally:
            _job["finished_at"] = datetime.now().isoformat()
            _job["lumg_id"] = None

    async def update_data_for_lumg(self, lumg_id: int, background_tasks: BackgroundTasks):
        global _job
        if _job["status"] == "running":
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Update is already in progress. Please try again later.",
            )
        _job["status"] = "running"
        background_tasks.add_task(self._run_update_lumg, lumg_id)
        return {"message": f"Update started for lumg {lumg_id}", "status": "running", "started_at": datetime.now().isoformat()}

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
