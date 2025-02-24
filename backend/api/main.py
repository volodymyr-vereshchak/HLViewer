import asyncio
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI
from backend.api.endpoints import (
    lumg_ep,
    root_ep,
    hour_archive_ep,
    gas_volume_calc_ep,
    line_ep,
    edit_archive_ep,
    sys_archive_ep,
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
]


@asynccontextmanager
async def lifespan(application: FastAPI):
    # Run telegram bot
    bot = TelegramBot()
    asyncio.create_task(bot.run())
    scheduler = AsyncIOScheduler()
    trigger = CronTrigger(hour="*/2", minute=30)
    scheduler.add_job(HostlibUpdater().update_and_send_notification, trigger)
    scheduler.start()
    yield
    await bot.stop_bot()


# run FastApi
app = FastAPI(openapi_tags=tags_metadata, lifespan=lifespan)

app.include_router(day_archive_ep.daily_router)
app.include_router(hour_archive_ep.hourly_router)
app.include_router(edit_archive_ep.edit_router)
app.include_router(sys_archive_ep.sys_router)
app.include_router(lumg_ep.lumg_router)
app.include_router(root_ep.root_router)
app.include_router(gas_volume_calc_type_ep.gvct_router)
app.include_router(gas_volume_calc_ep.gas_volume_calc_router)
app.include_router(line_ep.line_router)
