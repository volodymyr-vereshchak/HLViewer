from fastapi import FastAPI
from api.endpoints import day_archive_ep, lumg_ep

app = FastAPI()

app.include_router(day_archive_ep.router)
app.include_router(lumg_ep.router)
