import asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from pytz import timezone
from backend.hl_engine.hostlib_updater import HostlibUpdater


async def main():
    scheduler = AsyncIOScheduler()
    trigger = CronTrigger(minute=30, timezone=timezone("Europe/Kyiv"))
    scheduler.add_job(HostlibUpdater().update_and_send_notification, trigger)
    scheduler.start()
    print("Scheduler started. Waiting for jobs...")
    await asyncio.Event().wait()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
