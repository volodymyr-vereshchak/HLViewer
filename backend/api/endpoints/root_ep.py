import asyncio
import json
from typing import Awaitable, Callable

import sqlalchemy as sa
from fastapi import APIRouter, Depends, BackgroundTasks, status, HTTPException
from pydantic import BaseModel

from backend.api.endpoints.auth_ep import get_current_user
from backend.db.engine import async_session_factory
from backend.db.preload_db.preload_db import preload_db
from backend.hl_engine.main import update_hostlibs, update_direct
from backend.hl_engine.hostlib_updater import HostlibUpdater
from utils.logger import logger_setup


class DirectUpdateBody(BaseModel):
    lumg_id: int
    path: str


# How often the running worker writes a heartbeat (and flushes per-LUMG progress).
HEARTBEAT_SEC = 1.5
# If the heartbeat is older than this, the job is treated as crashed: a new update
# may take it over, and /status reports it as an error instead of a stuck "running".
# Generous so that a legitimately long update is never mistaken for a dead one.
STALE_SECONDS = 300


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

    async def _acquire(self, lumg_id: int | None = None) -> bool:
        """Atomically claim the job. Returns True if claimed, False if one is
        already running (and not stale). Atomic at the DB level, so two workers
        can never both win."""
        async with async_session_factory() as session:
            result = await session.execute(
                sa.text(
                    """
                    UPDATE update_job
                    SET status = 'running',
                        started_at = now(),
                        updated_at = now(),
                        finished_at = NULL,
                        error = NULL,
                        lumg_id = :lumg_id,
                        progress = '{}'::jsonb
                    WHERE id = 1
                      AND (status <> 'running'
                           OR updated_at < now() - make_interval(secs => :stale))
                    RETURNING id
                    """
                ),
                {"lumg_id": lumg_id, "stale": STALE_SECONDS},
            )
            acquired = result.scalar() is not None
            await session.commit()
            return acquired

    async def _heartbeat(self, progress: dict) -> None:
        """Flush progress + bump the heartbeat. No-op if the job is no longer
        running (e.g. it was reset), so it can't resurrect a finished job."""
        async with async_session_factory() as session:
            await session.execute(
                sa.text(
                    """
                    UPDATE update_job
                    SET progress = CAST(:progress AS jsonb), updated_at = now()
                    WHERE id = 1 AND status = 'running'
                    """
                ),
                {"progress": json.dumps(progress)},
            )
            await session.commit()

    async def _finalize(self, status_: str, error: str | None, progress: dict) -> None:
        async with async_session_factory() as session:
            await session.execute(
                sa.text(
                    """
                    UPDATE update_job
                    SET status = :status,
                        error = :error,
                        progress = CAST(:progress AS jsonb),
                        finished_at = now(),
                        updated_at = now()
                    WHERE id = 1
                    """
                ),
                {"status": status_, "error": error, "progress": json.dumps(progress)},
            )
            await session.commit()

    async def _read(self) -> dict:
        """Read job state for the API. Maps the DB row to the shape the frontend
        expects (`lumgs` = per-LUMG progress) and downgrades a stale 'running'
        to an error so the UI never gets stuck."""
        async with async_session_factory() as session:
            row = (
                await session.execute(
                    sa.text(
                        """
                        SELECT status, started_at, finished_at, error, lumg_id, progress,
                               (status = 'running'
                                AND updated_at < now() - make_interval(secs => :stale)) AS is_stale
                        FROM update_job WHERE id = 1
                        """
                    ),
                    {"stale": STALE_SECONDS},
                )
            ).mappings().first()

        if row is None:
            return {"status": "idle", "started_at": None, "finished_at": None,
                    "error": None, "lumg_id": None, "lumgs": {}}

        if row["is_stale"]:
            return {
                "status": "error",
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "error": "Оновлення перервано (процес зупинився)",
                "lumg_id": row["lumg_id"],
                "lumgs": row["progress"] or {},
            }

        return {
            "status": row["status"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "error": row["error"],
            "lumg_id": row["lumg_id"],
            "lumgs": row["progress"] or {},
        }

    async def _run_job(self, work: Callable[[object, dict], Awaitable[None]]) -> None:
        """Run `work(session, progress)` with a heartbeat that flushes progress
        to the DB and records the terminal state. Lock is already acquired."""
        progress: dict = {}
        stop = asyncio.Event()

        async def heartbeat():
            while not stop.is_set():
                try:
                    await self._heartbeat(dict(progress))
                except Exception:
                    self.logger.warning("Heartbeat persist failed", exc_info=True)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_SEC)
                except asyncio.TimeoutError:
                    pass

        hb = asyncio.create_task(heartbeat())
        result_status, error = "done", None
        try:
            async with async_session_factory() as session:
                await work(session, progress)
        except Exception as e:
            result_status, error = "error", str(e) or e.__class__.__name__
            self.logger.error(f"Update job failed: {e}", exc_info=True)
        finally:
            stop.set()
            try:
                await hb
            except Exception:
                pass
            try:
                await self._finalize(result_status, error, dict(progress))
            except Exception:
                self.logger.error("Failed to persist final job state", exc_info=True)

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

    async def reset_update_status(self):
        """Force the job back to idle — instant manual recovery if it ever hangs."""
        async with async_session_factory() as session:
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
