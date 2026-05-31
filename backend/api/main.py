import json
import logging
import math
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from backend.logging_config import setup_logging

from backend.api.endpoints import device_catalog_ep
from backend.api.endpoints import (
    lumg_ep,
    root_ep,
    hour_archive_ep,
    gas_volume_calc_ep,
    line_ep,
    edit_archive_ep,
    sys_archive_ep,
    param_ep,
    enterprise_ep,
    enterprise_virtual_ep,
    virtual_lines_ep,
    hourly_virtual_ep,
    daily_virtual_ep,
    grmu_branch_ep,
    auth_ep,
)
from backend.api.endpoints import gas_volume_calc_type_ep, day_archive_ep
from backend.api.endpoints import sys_type_ep
from backend.api.endpoints import edit_type_ep
from backend.api.endpoints import config_ep
from backend.api.endpoints import logs_ep
from backend.telegram_notifier.telegram_norifier import TelegramBot
from backend.db.engine import async_session_factory
from backend.db.models.app_user_model import AppUser
from backend.api.endpoints.auth_ep import hash_password
from backend.hl_engine.main import _cleanup_orphan_temp_dirs
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

# Configure file logging (rotating, multiprocess-safe) + console. Runs once per
# worker process at import time. See backend/logging_config.py.
setup_logging()
logger = logging.getLogger(__name__)


def _sanitize_nan(obj):
    """Recursively replace NaN/Inf floats with None for JSON compliance."""
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    if isinstance(obj, dict):
        return {k: _sanitize_nan(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_nan(v) for v in obj]
    return obj


class NaNSafeJSONResponse(JSONResponse):
    def render(self, content) -> bytes:
        return json.dumps(
            _sanitize_nan(content),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")


tags_metadata = [
    {
        "name": "lumg",
        "description": "Operations with lumgs.",
    },
    {
        "name": "Gas volume calcs",
        "description": "Operations with gas volume calcs.",
    },
    {
        "name": "lines",
        "description": "Operations with lines.",
    },
    {
        "name": "root",
        "description": "Operations with all archives.",
    },
    {
        "name": "daily",
        "description": "Operations with all daily archives.",
    },
    {
        "name": "hourly",
        "description": "Operations with all hourly archives.",
    },
    {
        "name": "edit",
        "description": "Operations with all edit archives.",
    },
    {
        "name": "sys",
        "description": "Operations with all sys archives.",
    },
    {
        "name": "param",
        "description": "Operations with all params.",
    },
    {
        "name": "enterprise",
        "description": "Operations with enterprise volume data from DPD API.",
    },
    {
        "name": "virtual_lines",
        "description": "Operations with virtual lines (rings).",
    },
    {
        "name": "hourly_virtual",
        "description": "Operations with hourly archives supporting virtual lines.",
    },
    {
        "name": "daily_virtual",
        "description": "Operations with daily archives supporting virtual lines.",
    },
    {
        "name": "grmu_branch",
        "description": "Operations with GRMU branches (Філіали ГРМУ) and their device mappings.",
    },
]


async def _seed_admin():
    """Create initial admin user from env vars if no users exist."""
    username = os.getenv("ADMIN_USERNAME", "admin")
    password = os.getenv("ADMIN_PASSWORD")
    if not password:
        return
    async with async_session_factory() as session:
        result = await session.execute(select(AppUser).where(AppUser.username == username))
        if result.scalar_one_or_none() is None:
            user = AppUser(
                username=username,
                role="admin",
                active=True,
                password_hash=hash_password(password),
            )
            session.add(user)
            try:
                await session.commit()
                logger.info("Seeded initial admin user: %s", username)
            except IntegrityError:
                # Another worker seeded it first (startup race with --workers > 1)
                await session.rollback()


async def _seed_default_user():
    """Create default viewer user from env vars if not exists. Fixes legacy viewer_all role."""
    username = os.getenv("DEFAULT_USERNAME")
    password = os.getenv("DEFAULT_PASSWORD")
    if not username or not password:
        return
    async with async_session_factory() as session:
        result = await session.execute(select(AppUser).where(AppUser.username == username))
        existing = result.scalar_one_or_none()
        if existing is None:
            user = AppUser(
                username=username,
                display_name=username,
                role="viewer",
                active=True,
                password_hash=hash_password(password),
            )
            session.add(user)
            try:
                await session.commit()
                logger.info("Seeded default viewer user: %s", username)
            except IntegrityError:
                # Another worker seeded it first (startup race with --workers > 1)
                await session.rollback()
        elif existing.role == "viewer_all":
            existing.role = "viewer"
            session.add(existing)
            await session.commit()
            logger.info("Migrated default user role viewer_all → viewer: %s", username)


@asynccontextmanager
async def lifespan(application: FastAPI):
    logger.info("HLViewer backend starting (log dir: %s)", os.getenv("LOG_DIR", "logs"))
    _cleanup_orphan_temp_dirs()
    await _seed_admin()
    await _seed_default_user()
    yield
    logger.info("HLViewer backend shutting down")


# run FastApi
app = FastAPI(openapi_tags=tags_metadata, lifespan=lifespan, default_response_class=NaNSafeJSONResponse)
app.add_middleware(GZipMiddleware, minimum_size=1000)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Log every uncaught exception with a full traceback so it reaches the log
    files, then return a generic 500. FastAPI handles HTTPException and
    validation errors separately, so this only fires for genuine bugs."""
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return NaNSafeJSONResponse(
        status_code=500, content={"detail": "Internal Server Error"}
    )

# Add CORS middleware
_default_cors = ",".join([
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:3001",
    "http://127.0.0.1:3001",
    "http://localhost:8050",
    "http://localhost:8060",
    "http://127.0.0.1:8050",
    "http://grmu-zp-s-metr1:8050",
    "http://10.130.8.141:8050",
])
_cors_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", _default_cors).split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(day_archive_ep.daily_router)
app.include_router(hour_archive_ep.hourly_router)
app.include_router(edit_archive_ep.edit_router)
app.include_router(sys_archive_ep.sys_router)
app.include_router(lumg_ep.lumg_router)
app.include_router(root_ep.root_router)
app.include_router(gas_volume_calc_type_ep.gvct_router)
app.include_router(gas_volume_calc_ep.gas_volume_calc_router)
app.include_router(line_ep.line_router)
app.include_router(param_ep.param_router)
app.include_router(enterprise_ep.enterprise_router)
app.include_router(enterprise_virtual_ep.enterprise_virtual_router)
app.include_router(virtual_lines_ep.virtual_lines_router)
app.include_router(hourly_virtual_ep.hourly_virtual_router)
app.include_router(daily_virtual_ep.daily_virtual_router)
app.include_router(grmu_branch_ep.grmu_branch_router)
app.include_router(auth_ep.auth_router)
app.include_router(device_catalog_ep.router)
app.include_router(sys_type_ep.sys_type_router)
app.include_router(edit_type_ep.edit_type_router)
app.include_router(config_ep.router)
app.include_router(logs_ep.router)
