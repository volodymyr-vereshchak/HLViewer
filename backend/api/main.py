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
    gas_route_ep,
    hourly_virtual_ep,
    daily_virtual_ep,
    dpd_line_ep,
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
from backend.api.endpoints.auth_ep import (
    hash_password,
    _maybe_refresh_token,
    decode_jwt,
    resolve_session_user,
    session_ended_marker,
    COOKIE_NAME,
    COOKIE_SECURE,
    SESSION_ENDED_HEADER,
)
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
        "name": "dpd_lines",
        "description": (
            "Operations with DPD lines: lines fed from the DPD API with a "
            "device (corrector) history."
        ),
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
    # No temp-dir sweep here. This runs in every uvicorn worker, none of which
    # holds the update lock, so a restart while the scheduler was extracting
    # meant 8 processes deleting its live temp dir. update_hostlibs sweeps at
    # the start of every run instead — under the lock, where it is safe.
    await _seed_admin()
    await _seed_default_user()
    yield
    logger.info("HLViewer backend shutting down")


class StreamAwareGZipMiddleware(GZipMiddleware):
    """GZip everything except the NDJSON progress stream: gzip accumulates the
    tiny event lines in its compression buffer, so the browser would receive
    every progress event in one burst at the end instead of live."""

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].endswith("/enterprise/volumes/stream"):
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


# run FastApi
app = FastAPI(openapi_tags=tags_metadata, lifespan=lifespan, default_response_class=NaNSafeJSONResponse)
app.add_middleware(StreamAwareGZipMiddleware, minimum_size=1000)


@app.middleware("http")
async def sliding_session_middleware(request: Request, call_next):
    """Sliding session: re-issue the auth cookie on any request whose token is
    past the refresh threshold, so an actively-used tab never expires mid-use.
    Idle tabs still expire normally (and the frontend re-auths via /auth/me)."""
    response = await call_next(request)
    token = request.cookies.get(COOKIE_NAME)
    # 401 means the guard (or /auth/me) just refused this token — a rights change
    # under it, a deactivated account, an expired session. Re-issuing the cookie
    # here would hand back a fresh copy of the very token that was refused, and
    # on /auth/me it would race the delete_cookie that ends the session.
    if token and response.status_code != 401:
        refreshed = _maybe_refresh_token(token)
        if refreshed:
            new_token, max_age = refreshed
            response.set_cookie(
                key=COOKIE_NAME,
                value=new_token,
                httponly=True,
                secure=COOKIE_SECURE,
                max_age=max_age,
                samesite="lax",
            )
    return response


# ── Central authorization guard (default-deny) ────────────────────────────────
# Every endpoint requires a valid session EXCEPT the public allowlist below.
# Mutations (POST/PUT/PATCH/DELETE) and sensitive config/credential endpoints
# require the "admin" role; authenticated viewers keep read access (data reads
# are still branch-filtered by the existing dependencies). This closes the
# systemic missing-authorization gap centrally and fails closed: any new route
# is protected by default unless explicitly added to the public allowlist.

# Reachable without authentication: login flow, frontend bootstrap, client logs.
_PUBLIC_PATHS = frozenset({
    "/auth/login",
    "/auth/logout",
    "/auth/me",
    "/config",
    "/client-log",
})
# Path markers for admin-only endpoints that are GET (credentials, data paths,
# branch config tooling) and would otherwise pass the read-only check.
_ADMIN_PATH_MARKERS = (
    "dpd-credential",
    "data-path",
    "config-debug",
    "config-preview",
    "config-mappings",
    # The branch bundle carries the DPD password in clear text, and a GET would
    # otherwise pass the read-only check.
    "config-export",
    "scan-eis",
)
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    path = request.url.path
    method = request.method
    # CORS preflight and public endpoints bypass the guard.
    if method == "OPTIONS" or path in _PUBLIC_PATHS:
        return await call_next(request)

    claims = decode_jwt(request.cookies.get(COOKIE_NAME))
    if claims is None:
        return NaNSafeJSONResponse(status_code=401, content={"detail": "Not authenticated"})

    # The role in the token is a snapshot taken at login and a remember-me token
    # lives 30 days, so it cannot be the last word on what the account may do:
    # a demoted admin would keep write access here for weeks, and a promoted
    # viewer would be shown the admin screens and refused on every save. The
    # account is re-read per request; a token from before the change is dead.
    user, reason = await resolve_session_user(claims)
    if user is None:
        marker = session_ended_marker(reason)
        return NaNSafeJSONResponse(
            status_code=401,
            content={"detail": reason},
            headers={SESSION_ENDED_HEADER: marker} if marker else None,
        )

    needs_admin = method in _WRITE_METHODS or any(m in path for m in _ADMIN_PATH_MARKERS)
    if needs_admin and user.role != "admin":
        return NaNSafeJSONResponse(status_code=403, content={"detail": "Admin privileges required"})

    request.state.claims = claims
    return await call_next(request)


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
    # allow_headers covers the request side; a response header is invisible to
    # JS cross-origin unless it is listed here. Same-origin behind nginx this
    # changes nothing, but the dev server talks to the API on another port.
    expose_headers=[SESSION_ENDED_HEADER],
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
app.include_router(gas_route_ep.gas_route_router)
app.include_router(dpd_line_ep.dpd_line_router)
app.include_router(hourly_virtual_ep.hourly_virtual_router)
app.include_router(daily_virtual_ep.daily_virtual_router)
app.include_router(grmu_branch_ep.grmu_branch_router)
app.include_router(auth_ep.auth_router)
app.include_router(device_catalog_ep.router)
app.include_router(sys_type_ep.sys_type_router)
app.include_router(edit_type_ep.edit_type_router)
app.include_router(config_ep.router)
app.include_router(logs_ep.router)
