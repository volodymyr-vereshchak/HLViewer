from fastapi import FastAPI
from api.endpoints import (
    day_archive_ep,
    lumg_ep,
    root_ep,
    hour_archive_ep,
    gas_volume_calc_type_ep,
    gas_volume_calc_ep,
)


tags_metadata = [
    {
        "name": "lumg",
        "description": "Operations with lumgs.",
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
]

app = FastAPI(openapi_tags=tags_metadata)

app.include_router(day_archive_ep.daily_router)
app.include_router(hour_archive_ep.hourly_router)
app.include_router(lumg_ep.lumg_router)
app.include_router(root_ep.root_router)
app.include_router(gas_volume_calc_type_ep.gvct_router)
app.include_router(gas_volume_calc_ep.gas_volume_calc_router)
