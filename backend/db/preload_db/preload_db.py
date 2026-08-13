"""Seed the reference data every installation needs: the device catalog and the
event-type dictionaries. Runs on container start and is also exposed as
POST /preload_data/, so it must stay idempotent and non-destructive."""

import asyncio

from backend.db.engine import async_session_factory
from backend.db.preload_db import preload_device_catalog
from backend.db.preload_db.event_types_json import import_event_types


async def preload_db():
    async with async_session_factory() as session:
        await preload_device_catalog.preload(session)
        await import_event_types(session)


if __name__ == "__main__":
    asyncio.run(preload_db())
