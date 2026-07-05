import sqlalchemy as sa
from fastapi import APIRouter, Depends, BackgroundTasks, status, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.endpoints.auth_ep import get_current_user
from backend.db.engine import get_session
from backend.db.preload_db.preload_db import preload_db
from backend.hl_engine.main import update_hostlibs, update_direct
from backend.hl_engine.hostlib_updater import HostlibUpdater
from backend.hl_engine import update_job_lock
from utils.logger import logger_setup


class DirectUpdateBody(BaseModel):
    lumg_id: int
    path: str


class RootRouter:
    """Hostlib-update endpoints.

    The update job state lives in the single-row `update_job` table (not in
    process memory) so that all uvicorn workers share one consistent view —
    status polls, the duplicate-update guard, and per-LUMG progress all agree
    regardless of which worker handles a given request.
    """

    def __init__(self):
        self.router = APIRouter(dependencies=[Depends(get_current_user)])
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
            path="/update_data/reset",
            endpoint=self.reset_update_status,
            tags=["root"],
            methods=["POST"],
            status_code=status.HTTP_200_OK,
        )
        self.router.add_api_route(
            path="/update_data/direct",
            endpoint=self.update_data_direct,
            tags=["root"],
            methods=["POST"],
            status_code=status.HTTP_202_ACCEPTED,
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

    # ── Shared job-state helpers (DB-backed) ──────────────────────────────────
    # The actual implementation lives in `backend.hl_engine.update_job_lock` so
    # that the standalone scheduler/poller process shares the exact same lock,
    # heartbeat and progress logic. These are thin delegators kept for the
    # endpoints below.

    async def _acquire(self, lumg_id: int | None = None) -> bool:
        return await update_job_lock.acquire(lumg_id)

    async def _read(self) -> dict:
        return await update_job_lock.read()

    async def _run_job(self, work) -> None:
        await update_job_lock.run_job(work)

    # ── Endpoints ─────────────────────────────────────────────────────────────

    async def update_data(self, background_tasks: BackgroundTasks):
        if not await self._acquire():
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Оновлення вже виконується. Зачекайте завершення.",
            )

        async def work(session, progress):
            await update_hostlibs(session=session, progress=progress)

        background_tasks.add_task(self._run_job, work)
        return await self._read()

    async def update_data_for_lumg(self, lumg_id: int, background_tasks: BackgroundTasks):
        if not await self._acquire(lumg_id=lumg_id):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Оновлення вже виконується. Зачекайте завершення.",
            )

        async def work(session, progress):
            await update_hostlibs(session=session, lumg_id=lumg_id, progress=progress)

        background_tasks.add_task(self._run_job, work)
        return await self._read()

    async def update_data_direct(self, body: DirectUpdateBody, background_tasks: BackgroundTasks):
        if not await self._acquire(lumg_id=body.lumg_id):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Оновлення вже виконується. Зачекайте завершення.",
            )

        async def work(session, progress):
            await update_direct(path=body.path, lumg_id=body.lumg_id, session=session, progress=progress)

        background_tasks.add_task(self._run_job, work)
        return await self._read()

    async def update_status(self):
        return await self._read()

    async def reset_update_status(self, session: AsyncSession = Depends(get_session)):
        """Force the job back to idle — instant manual recovery if it ever hangs."""
        await session.execute(
            sa.text(
                """
                UPDATE update_job
                SET status = 'idle',
                    error = 'Скинуто адміністратором',
                    finished_at = now(),
                    updated_at = now()
                WHERE id = 1
                """
            )
        )
        await session.commit()
        return await self._read()

    async def get_report(self, session: AsyncSession = Depends(get_session)):
        """Get gas volume report for the last 24 hours without updating hostlibs"""
        try:
            updater = HostlibUpdater()
            from backend.db.dao.hourly_archive_dao import HourlyArchiveDao
            from backend.db.models import HourlyArchiveList
            from backend.db.models.line_model import Line
            from datetime import timedelta
            from sqlmodel import select
            import pandas as pd

            # Report covers the lines flagged include_in_report; the flag
            # is_high_pressure switches the Pвх/Pвых label per line.
            report_lines = (await session.execute(
                select(Line).where(Line.include_in_report == True)  # noqa: E712
            )).scalars().all()
            line_flags = {ln.id: ln.is_high_pressure for ln in report_lines}
            if not line_flags:
                return {
                    "message": "Немає ліній з увімкненим прапорцем «у звіт» (include_in_report)",
                    "success": False,
                }

            # Получаем данные за последние 24 часа
            end = await HourlyArchiveDao(session=session).get_last_period()
            if end is None:
                return {"message": "Немає годинних даних для звіту", "success": False}
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
            message = await updater.create_message(df, line_flags)

            return {"message": message, "success": True}

        except Exception as e:
            self.logger.error(f"Error generating report: {e}", exc_info=True)
            return {"message": f"Ошибка при генерации отчета: {str(e)}", "success": False}


root_router = RootRouter().router
