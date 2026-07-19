"""
Enterprise Volumes API Endpoint

Provides endpoints for fetching enterprise volume data from DPD API,
aggregating by line_id and date, and CRUD management of enterprise records.
"""

import asyncio
import json
import logging
import pandas as pd
from typing import List, Optional
from fastapi import APIRouter, Depends, Query, status, HTTPException, UploadFile, File
from backend.api.endpoints.auth_ep import get_branch_filter
from backend.api.endpoints.enterprise_virtual_ep import resolve_virtual_devices
from fastapi.responses import StreamingResponse

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from backend.db.engine import get_session
from backend.db.dao.dpd_archive_dao import DpdArchiveDao
from backend.db.dao.enterprise_dao import EnterpriseDao
from backend.services import dpd_archive_refresh
from backend.db.models.enterprise_model import EnterpriseRead, EnterpriseCreate, EnterpriseUpdate
from backend.db.models.enterprise_models import (
    EnterpriseVolumeResponse,
    EnterpriseVolumeError,
    EnterpriseMapping
)
from backend.services import enterprise_excel
from backend.services.enterprise_mappings import get_devices_for_lines_db, load_mappings
from backend.services.enterprise_volume_service import (
    aggregate_volumes,
    fetch_dpd_volumes,
    parse_date_range,
)

logger = logging.getLogger(__name__)

# Strong refs to detached poll-cleanup tasks (bare create_task results may be
# garbage-collected before completion).
_poll_reapers: set = set()


async def _reap_poll(task: "asyncio.Task") -> None:
    """Await a (usually cancelled) poll task until it fully unwinds, so its
    transaction rolls back and the branch advisory lock is released.

    Runs DETACHED from the stream generator: awaiting the task inside the
    dying generator's finally can itself be re-cancelled mid-rollback, leaving
    the lock on a leaked connection (prod incident 2026-07-11: one cancelled
    stream wedged its branch for ~50 minutes, queueing every later request)."""
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("Enterprise poll cleanup failed")


class EnterpriseRouter:
    """Router for enterprise volume endpoints."""

    def __init__(self):
        self.router = APIRouter()
        self.router.add_api_route(
            path="/enterprise/volumes/",
            tags=["enterprise"],
            endpoint=self.get_enterprise_volumes,
            methods=["GET"],
            response_model=List[EnterpriseVolumeResponse],
            status_code=status.HTTP_200_OK,
            summary="Get enterprise volume data",
            description=(
                "Fetches volume data for enterprise calculators from DPD API, "
                "aggregated by line_id and date. Returns empty array if no "
                "mappings exist for specified lines."
            ),
            responses={
                200: {
                    "description": "Successfully retrieved enterprise volumes",
                    "model": List[EnterpriseVolumeResponse]
                },
                400: {
                    "description": "Invalid request parameters",
                    "model": EnterpriseVolumeError
                },
                500: {
                    "description": "Server error (e.g., mappings file not found)",
                    "model": EnterpriseVolumeError
                },
                503: {
                    "description": "DPD API unavailable",
                    "model": EnterpriseVolumeError
                }
            }
        )
        self.router.add_api_route(
            path="/enterprise/mappings/",
            tags=["enterprise"],
            endpoint=self.get_all_enterprises_excel,
            methods=["GET"],
            response_model=List[EnterpriseMapping],
            status_code=status.HTTP_200_OK,
            summary="Get all enterprise mappings (Excel source)",
            description=(
                "Returns list of all enterprises from Excel mappings with their device "
                "information and active status. Legacy endpoint."
            ),
        )
        self.router.add_api_route(
            path="/enterprise/volumes/stream",
            tags=["enterprise"],
            endpoint=self.stream_enterprise_volumes,
            methods=["GET"],
            summary="Enterprise volumes with NDJSON progress stream",
            description=(
                "Same data as /enterprise/volumes/ (or /enterprise/volumes_virtual/ "
                "with virtual=true), but the response is an application/x-ndjson "
                "stream: one JSON event per line — status/progress/ping while the "
                "DPD poll runs, then a final result (or error) event."
            ),
        )
        self.router.add_api_route(
            path="/enterprise/cache/",
            tags=["enterprise"],
            endpoint=self.clear_dpd_archive,
            methods=["DELETE"],
            status_code=status.HTTP_200_OK,
            summary="Wipe the DPD archive",
            description=(
                "Deletes both archive tables and the coverage registry; the "
                "next scheduler run (or manual refresh) reloads the window "
                "from scratch. Admin-only (the auth middleware requires the "
                "admin role for any DELETE)."
            ),
        )
        self.router.add_api_route(
            path="/enterprise/archive/refresh",
            tags=["enterprise"],
            endpoint=self.trigger_archive_refresh,
            methods=["POST"],
            status_code=status.HTTP_202_ACCEPTED,
            summary="Manually refresh the DPD archive now",
            description=(
                "Starts the same job the scheduler runs at DPD_REFRESH_TIMES: "
                "re-poll the last DPD_ARCHIVE_WINDOW_DAYS for all enterprises. "
                "409 when a refresh is already running. Admin-only (POST)."
            ),
        )
        self.router.add_api_route(
            path="/enterprise/archive/refresh/status",
            tags=["enterprise"],
            endpoint=self.archive_refresh_status,
            methods=["GET"],
            summary="DPD archive refresh status",
        )


    async def stream_enterprise_volumes(
        self,
        line_id: Optional[List[int]] = Query(default=None, description="Line IDs (physical, or mixed with virtual when virtual=true)"),
        from_date: str = Query(..., description="Start date (YYYY-MM-DD)"),
        to_date: str = Query(..., description="End date (YYYY-MM-DD)"),
        period_type: str = Query(
            default="daily",
            pattern="^(daily|hourly)$",
            description="Data granularity: 'daily' or 'hourly'"
        ),
        virtual: bool = Query(default=False, description="volumes_virtual semantics: resolve virtual lines, report missing volumes as 0"),
        include_devices: bool = Query(default=True, description="Set false to strip per-device breakdowns from the result (reports that only need line totals get a ~100x smaller payload)"),
        serNum: Optional[int] = Query(None, description="Optional: Filter by device serial number"),
        mfDev: Optional[int] = Query(None, description="Optional: Manufacturer code"),
        typeDev: Optional[int] = Query(None, description="Optional: Device type code"),
        chNum: Optional[int] = Query(None, description="Optional: Filter by device channel number"),
        live: bool = Query(default=False, description=(
            "Re-poll the DPD API for the whole range first (fresh data); the "
            "archive serves as fallback when the API is unreachable"
        )),
        session: AsyncSession = Depends(get_session),
    ) -> StreamingResponse:
        logger.info(
            f"Streaming enterprise volumes for lines {line_id} "
            f"(virtual={virtual}, live={live}), period {from_date} to {to_date}, "
            f"granularity: {period_type}"
        )
        try:
            date_from, date_to = parse_date_range(from_date, to_date)
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid date format: {e}. Use YYYY-MM-DD format."
            )

        # Devices are resolved before the stream starts so validation and DB
        # problems still surface as ordinary HTTP errors, not 200 + error event.
        line_remap = None
        none_volume_as_zero = False
        try:
            if virtual:
                if not line_id:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="line_id is required with virtual=true"
                    )
                devices, line_remap = await resolve_virtual_devices(line_id, session)
                none_volume_as_zero = True
            elif line_id:
                devices = await get_devices_for_lines_db(line_id, session)
                if serNum is not None and chNum is not None:
                    devices = [d for d in devices if d["serNum"] == serNum and d["chNum"] == chNum]
            elif None not in (serNum, mfDev, typeDev, chNum):
                ent = await EnterpriseDao(session).get_by_device(serNum, mfDev, typeDev, chNum)
                devices = [] if not ent else [{
                    "id": ent.id,
                    "line_id": ent.line_id if ent.line_id is not None else ent.dpd_line_id,
                    "branch_id": ent.branch_id,
                    "serNum": ent.ser_num,
                    "mfDev": ent.mf_dev,
                    "typeDev": ent.type_dev,
                    "chNum": ent.ch_num,
                    "enterprise_name": ent.enterprise_name,
                }]
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Provide line_id or serNum+chNum"
                )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error loading enterprise mappings from DB: {e}")
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

        return StreamingResponse(
            self._volume_events(
                devices, date_from, date_to, period_type,
                line_remap, none_volume_as_zero, include_devices, live,
            ),
            media_type="application/x-ndjson",
            # nginx honors this per-response: events reach the browser as they
            # happen instead of being buffered — no nginx config change needed.
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
        )

    @staticmethod
    async def _volume_events(
        devices, date_from, date_to, period_type,
        line_remap, none_volume_as_zero, include_devices=True, live=False,
    ):
        """NDJSON event generator around fetch_dpd_volumes.

        The poll pushes events through a non-blocking callback: progress goes
        into a single latest-value slot (only the newest counter matters, so a
        slow client merely skips intermediate values and can never slow the
        poll down), rare status events go into a queue. This generator drains
        both at <=10 events/s and pings on silent stretches (waiting for the
        branch advisory lock) so proxies don't drop the connection."""
        # Deferred import: backend.api.main imports this module at startup.
        from backend.api.main import _sanitize_nan

        def dump(event) -> str:
            return json.dumps(
                _sanitize_nan(event), ensure_ascii=False,
                allow_nan=False, separators=(",", ":"),
            ) + "\n"

        if not devices:
            yield dump({"type": "result", "data": []})
            return

        queue: asyncio.Queue = asyncio.Queue()
        latest = {"progress": None, "dirty": False}

        def events_cb(event):
            if event.get("type") == "progress":
                latest["progress"] = event
                latest["dirty"] = True
            else:
                queue.put_nowait(event)

        async def run():
            volumes = await fetch_dpd_volumes(
                devices, date_from, date_to, period_type,
                events_cb=events_cb, live=live,
            )
            return aggregate_volumes(
                volumes, devices, period_type,
                line_remap=line_remap,
                none_volume_as_zero=none_volume_as_zero,
            )

        task = asyncio.create_task(run())
        silent = 0.0
        try:
            while True:
                # Status events first (they are emitted before the progress
                # counter they precede logically), then the coalesced progress.
                try:
                    event = queue.get_nowait()
                except asyncio.QueueEmpty:
                    event = None
                if event is not None:
                    silent = 0.0
                    yield dump(event)
                    continue
                if latest["dirty"]:
                    latest["dirty"] = False
                    silent = 0.0
                    yield dump(latest["progress"])
                    continue
                if task.done():
                    break
                await asyncio.sleep(0.1)
                silent += 0.1
                if silent >= 15.0:
                    silent = 0.0
                    yield dump({"type": "ping"})
            # Flush whatever arrived between the last drain and task completion.
            while True:
                try:
                    yield dump(queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            if latest["dirty"]:
                yield dump(latest["progress"])
            result = await task
            if not include_devices:
                for r in result:
                    r.devices = []
            # Serialize with the pydantic core (jsonable_encoder walked ~118k
            # nested device objects in pure Python — minutes on prod) and off
            # the event loop, so a big result cannot stall the whole worker.
            yield await asyncio.to_thread(
                lambda: dump({
                    "type": "result",
                    "data": [r.model_dump(mode="json") for r in result],
                })
            )
        except asyncio.CancelledError:
            # Client went away; the poll is reaped in the finally below.
            logger.info("Enterprise volume stream cancelled by client")
            raise
        except Exception as e:
            # The response is already streaming with status 200, so failures
            # can only be reported in-band.
            logger.exception("Enterprise volume stream failed")
            yield dump({"type": "error", "detail": str(e)})
        finally:
            task.cancel()
            # Never await the unwind here: this generator may be dying under
            # cancellation and the await would be interrupted mid-rollback,
            # leaking the branch advisory lock. A detached reaper waits it out.
            reaper = asyncio.create_task(_reap_poll(task))
            _poll_reapers.add(reaper)
            reaper.add_done_callback(_poll_reapers.discard)

    async def clear_dpd_archive(
        self,
        session: AsyncSession = Depends(get_session),
    ) -> dict:
        await DpdArchiveDao(session).clear_all()
        await session.commit()
        logger.info("DPD archive wiped (daily, hourly, coverage)")
        return {"cleared": True}

    async def trigger_archive_refresh(self) -> dict:
        if not await dpd_archive_refresh.acquire():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Оновлення архіву вже виконується",
            )
        # Lock is held; run detached (same lifecycle as the scheduler run).
        asyncio.create_task(dpd_archive_refresh.execute_locked())
        return {"started": True}

    async def archive_refresh_status(self) -> dict:
        return await dpd_archive_refresh.read_status()

    async def get_enterprise_volumes(
        self,
        line_id: Optional[List[int]] = Query(default=None, description="Line IDs to fetch volumes for (optional if serNum+chNum provided)"),
        from_date: str = Query(..., description="Start date (YYYY-MM-DD)"),
        to_date: str = Query(..., description="End date (YYYY-MM-DD)"),
        period_type: str = Query(
            default="daily",
            pattern="^(daily|hourly)$",
            description="Data granularity: 'daily' or 'hourly'"
        ),
        serNum: Optional[int] = Query(None, description="Optional: Filter by device serial number"),
        mfDev: Optional[int] = Query(None, description="Optional: Manufacturer code"),
        typeDev: Optional[int] = Query(None, description="Optional: Device type code"),
        chNum: Optional[int] = Query(None, description="Optional: Filter by device channel number"),
        include_devices: bool = Query(default=True, description="Set false to strip per-device breakdowns (line totals only)"),
        live: bool = Query(default=False, description=(
            "Re-poll the DPD API for the whole range first (fresh data); the "
            "archive serves as fallback when the API is unreachable"
        )),
        session: AsyncSession = Depends(get_session),
    ) -> List[EnterpriseVolumeResponse]:
        logger.info(
            f"Fetching enterprise volumes for lines {line_id}, "
            f"period {from_date} to {to_date}, granularity: {period_type}, "
            f"live={live}"
        )

        try:
            date_from, date_to = parse_date_range(from_date, to_date)
        except ValueError as e:
            logger.error(f"Invalid date parameters: {e}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid date format: {e}. Use YYYY-MM-DD format."
            )

        if not line_id and (serNum is None or chNum is None or mfDev is None or typeDev is None):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Provide line_id or serNum+chNum"
            )

        try:
            if line_id:
                devices = await get_devices_for_lines_db(line_id, session)
                if serNum is not None and chNum is not None:
                    devices = [d for d in devices if d["serNum"] == serNum and d["chNum"] == chNum]
            else:
                # No line_id — lookup by full device identity
                ent = await EnterpriseDao(session).get_by_device(serNum, mfDev, typeDev, chNum)
                if not ent:
                    logger.warning(f"No enterprise found: serNum={serNum} mfDev={mfDev} typeDev={typeDev} chNum={chNum}")
                    return []
                devices = [{
                    "id": ent.id,
                    "line_id": ent.line_id if ent.line_id is not None else ent.dpd_line_id,
                    "branch_id": ent.branch_id,
                    "serNum": ent.ser_num,
                    "mfDev": ent.mf_dev,
                    "typeDev": ent.type_dev,
                    "chNum": ent.ch_num,
                    "enterprise_name": ent.enterprise_name,
                }]
        except Exception as e:
            logger.error(f"Error loading enterprise mappings from DB: {e}")
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

        if not devices:
            logger.info(f"No enterprise mappings found for lines {line_id}")
            return []

        try:
            volumes_data = await fetch_dpd_volumes(
                devices, date_from, date_to, period_type, live=live
            )
        except LookupError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        except Exception as e:
            logger.error(f"DPD API error: {e}")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"DPD API unavailable: {e}"
            )

        if not volumes_data:
            logger.warning("No volume data returned from DPD API")
            return []

        result = aggregate_volumes(volumes_data, devices, period_type)
        if not include_devices:
            for r in result:
                r.devices = []
        logger.info(
            f"Returning {len(result)} aggregated enterprise volume records "
            f"for {len(devices)} devices"
        )
        return result

    async def get_all_enterprises_excel(self) -> List[EnterpriseMapping]:
        """Legacy endpoint: read from Excel."""
        logger.info("Fetching all enterprise mappings (Excel)")

        try:
            df = load_mappings()
        except FileNotFoundError as e:
            logger.error(f"Enterprise mappings file not found: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=str(e)
            )
        except Exception as e:
            logger.error(f"Error loading enterprise mappings: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Error loading enterprise mappings: {e}"
            )

        if df is None or df.empty:
            logger.warning("No enterprise mappings available")
            return []

        result = []
        for _, row in df.iterrows():
            line_id_val = None if pd.isna(row["line_id"]) else int(row["line_id"])
            result.append(
                EnterpriseMapping(
                    line_id=line_id_val,
                    serNum=int(row["serNum"]),
                    mfDev=int(row["mfDev"]),
                    typeDev=int(row["typeDev"]),
                    chNum=int(row["chNum"]),
                    enterprise_name=str(row["enterprise_name"]),
                    active=bool(row["active"]),
                    enabled=bool(row["enabled"])
                )
            )

        result.sort(key=lambda x: (x.line_id is None, x.line_id or 0, x.enterprise_name))
        logger.info(f"Returning {len(result)} enterprise mappings")
        return result


# Create router instance (class-based routes)
enterprise_router = EnterpriseRouter().router

# ─── DB CRUD routes ───────────────────────────────────────────────────────────

_crud_router = APIRouter(tags=["enterprise"])


@_crud_router.get(
    "/enterprise-mappings/",
    response_model=List[EnterpriseRead],
    summary="List all enterprises (DB)",
)
async def list_enterprises(
    branch_ids: list[int] | None = Depends(get_branch_filter),
    session: AsyncSession = Depends(get_session),
):
    from sqlalchemy import asc
    from backend.db.models.enterprise_model import Enterprise
    from backend.db.models.device_catalog_model import CorectorType, Manufacturer

    stmt = (
        select(
            Enterprise,
            CorectorType.type_dev,
            CorectorType.model_name,
            Manufacturer.mf_dev,
            Manufacturer.short_name,
        )
        .outerjoin(CorectorType, Enterprise.corector_type_id == CorectorType.id)
        .outerjoin(Manufacturer, CorectorType.manufacturer_id == Manufacturer.id)
        .order_by(
            asc(Enterprise.line_id.is_(None)),
            asc(Enterprise.line_id),
            asc(Enterprise.enterprise_name),
        )
    )
    if branch_ids is not None:
        stmt = stmt.where(Enterprise.branch_id.in_(branch_ids))

    rows = (await session.execute(stmt)).all()
    result = []
    for ent, ct_type_dev, ct_model, mfr_mf_dev, mfr_short in rows:
        linked = ent.corector_type_id is not None
        data = ent.model_dump()
        # Surface EFFECTIVE device codes (catalog when linked, else legacy).
        data["mf_dev"] = mfr_mf_dev if linked else ent.mf_dev
        data["type_dev"] = ct_type_dev if linked else ent.type_dev
        data["model_name"] = ct_model
        data["manufacturer_short_name"] = mfr_short
        result.append(EnterpriseRead(**data))
    return result


@_crud_router.post(
    "/enterprise-mappings/",
    response_model=EnterpriseRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create enterprise (DB)",
)
async def create_enterprise(data: EnterpriseCreate, session: AsyncSession = Depends(get_session)):
    if data.line_id is not None and data.dpd_line_id is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Вкажіть або фізичну лінію, або ДПД-лінію — не обидві",
        )
    dao = EnterpriseDao(session=session)
    return await dao.create(data)


@_crud_router.patch(
    "/enterprise-mappings/{enterprise_id}",
    response_model=EnterpriseRead,
    summary="Update enterprise (DB)",
)
async def update_enterprise(
    enterprise_id: int, data: EnterpriseUpdate, session: AsyncSession = Depends(get_session)
):
    if data.line_id is not None and data.dpd_line_id is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Вкажіть або фізичну лінію, або ДПД-лінію — не обидві",
        )
    dao = EnterpriseDao(session=session)
    item = await dao.update(enterprise_id, data)
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Enterprise not found")
    return item


@_crud_router.delete(
    "/enterprise-mappings/{enterprise_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete enterprise (DB)",
)
async def delete_enterprise(enterprise_id: int, session: AsyncSession = Depends(get_session)):
    dao = EnterpriseDao(session=session)
    deleted = await dao.delete(enterprise_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Enterprise not found")


# ─── Excel template & upload ─────────────────────────────────────────────────
# Workbook building/parsing lives in backend/services/enterprise_excel.py;
# these endpoints only translate between HTTP and the service.

_XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _xlsx_response(wb, filename: str) -> StreamingResponse:
    return StreamingResponse(
        enterprise_excel.workbook_bytes(wb),
        media_type=_XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@_crud_router.get(
    "/enterprise-mappings/template",
    summary="Download Excel template for enterprise import",
    response_class=StreamingResponse,
)
async def download_template():
    wb = await enterprise_excel.build_template_workbook()
    return _xlsx_response(wb, "enterprise_template.xlsx")


@_crud_router.get(
    "/enterprise-mappings/export",
    summary="Export current enterprises from DB to Excel",
    response_class=StreamingResponse,
)
async def export_enterprises():
    wb = await enterprise_excel.build_export_workbook()
    return _xlsx_response(wb, "enterprise_export.xlsx")


@_crud_router.post(
    "/enterprise-mappings/upload",
    summary="Import enterprises from Excel",
)
async def upload_enterprises(file: UploadFile = File(...), branch_id: Optional[int] = None):
    content = await file.read()
    try:
        records, errors = await enterprise_excel.parse_upload(content, branch_id)
    except enterprise_excel.ExcelParseError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not records:
        return {"imported": 0, "warnings": len(errors), "errors": errors}

    returned_ids = await enterprise_excel.upsert_enterprises(records)
    logger.info(
        f"Upserted {len(returned_ids)} enterprise records from Excel ({len(errors)} warnings)"
    )
    return {
        "imported": len(returned_ids),
        "warnings": len(errors),
        "errors": errors,
    }


# Attach CRUD router to the main enterprise router so main.py needs no change
enterprise_router.include_router(_crud_router)

