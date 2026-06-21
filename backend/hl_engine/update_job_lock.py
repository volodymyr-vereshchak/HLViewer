"""Shared helpers for the single-row `update_job` lock.

The hostlib-update job state lives in the `update_job` table (id=1) so that all
uvicorn workers *and* the standalone scheduler process share one consistent view:
who is running, the duplicate-update guard, per-LUMG progress, and the crash
heartbeat all agree regardless of which process handles a given request.

Both the HTTP API (`root_ep.py`) and the file-arrival poller
(`scheduler_runner.py`) acquire the same lock through this module, so a manual
update and a poll-triggered update can never run at the same time.
"""
import asyncio
import json
import logging
from typing import Awaitable, Callable

import sqlalchemy as sa

from backend.db.engine import async_session_factory

logger = logging.getLogger(__name__)

# How often the running worker writes a heartbeat (and flushes per-LUMG progress).
HEARTBEAT_SEC = 1.5
# If the heartbeat is older than this, the job is treated as crashed: a new update
# may take it over, and /status reports it as an error instead of a stuck "running".
# Generous so that a legitimately long update is never mistaken for a dead one.
STALE_SECONDS = 300


async def acquire(lumg_id: int | None = None) -> bool:
    """Atomically claim the job. Returns True if claimed, False if one is already
    running (and not stale). Atomic at the DB level, so two processes can never
    both win."""
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


async def heartbeat(progress: dict) -> None:
    """Flush progress + bump the heartbeat. No-op if the job is no longer running
    (e.g. it was reset), so it can't resurrect a finished job."""
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


async def finalize(status_: str, error: str | None, progress: dict) -> None:
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


async def read() -> dict:
    """Read job state for the API. Maps the DB row to the shape the frontend
    expects (`lumgs` = per-LUMG progress) and downgrades a stale 'running' to an
    error so the UI never gets stuck."""
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


async def run_job(work: Callable[[object, dict], Awaitable[None]]) -> None:
    """Run `work(session, progress)` with a heartbeat that flushes progress to the
    DB and records the terminal state. The lock must already be acquired."""
    progress: dict = {}
    stop = asyncio.Event()

    async def _beat():
        while not stop.is_set():
            try:
                await heartbeat(dict(progress))
            except Exception:
                logger.warning("Heartbeat persist failed", exc_info=True)
            try:
                await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_SEC)
            except asyncio.TimeoutError:
                pass

    hb = asyncio.create_task(_beat())
    result_status, error = "done", None
    try:
        async with async_session_factory() as session:
            await work(session, progress)
    except Exception as e:
        result_status, error = "error", str(e) or e.__class__.__name__
        logger.error(f"Update job failed: {e}", exc_info=True)
    finally:
        stop.set()
        try:
            await hb
        except Exception:
            pass
        try:
            await finalize(result_status, error, dict(progress))
        except Exception:
            logger.error("Failed to persist final job state", exc_info=True)


async def run_guarded_update(
    work: Callable[[object, dict], Awaitable[None]], lumg_id: int | None = None
) -> bool:
    """Claim the lock and run `work` under heartbeat/finalize. Returns False
    immediately (without running) if another update is already in progress."""
    if not await acquire(lumg_id):
        return False
    await run_job(work)
    return True
