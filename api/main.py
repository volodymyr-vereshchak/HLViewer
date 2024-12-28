from fastapi import FastAPI
from api.endpoints import day_archive_ep, lumg_ep, root_ep

tags_metadata = [
    {
        "name": "lumg",
        "description": "Operations with lumgs.",
    },
]

app = FastAPI(openapi_tags=tags_metadata)

app.include_router(day_archive_ep.router)
app.include_router(lumg_ep.lumg_router)
app.include_router(root_ep.root_router)
