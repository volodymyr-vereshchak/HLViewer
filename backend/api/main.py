import asyncio
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from pytz import timezone

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
from backend.hl_engine.hostlib_updater import HostlibUpdater
from backend.telegram_notifier.telegram_norifier import TelegramBot

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


@asynccontextmanager
async def lifespan(application: FastAPI):
    # # Run telegram bot
    # bot = TelegramBot()
    # asyncio.create_task(bot.run())
    scheduler = AsyncIOScheduler()
    trigger = CronTrigger(minute=30, timezone=timezone("Europe/Kyiv"))
    scheduler.add_job(HostlibUpdater().update_and_send_notification, trigger)
    scheduler.start()
    yield
    # await bot.stop_bot()


# run FastApi
app = FastAPI(openapi_tags=tags_metadata, lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1000)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:3001",
        "http://127.0.0.1:3001",
        "http://localhost:8050",
        "http://localhost:8060",
        "http://127.0.0.1:8050",
        "http://grmu-zp-s-metr1:8050",
        "http://10.130.8.141:8050",
    ],
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
