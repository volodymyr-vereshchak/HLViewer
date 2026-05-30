"""
Public runtime config for the frontend.

Returns NON-SECRET project settings the SPA loads at startup (e.g. the
commercial-day contract hour). Intentionally unauthenticated — it is fetched
before login and contains nothing sensitive.
"""

from fastapi import APIRouter, status

from backend.settings import backend_settings

router = APIRouter(tags=["config"])


@router.get("/config", status_code=status.HTTP_200_OK)
async def get_app_config():
    return {
        "contract_hour": backend_settings.get("CONTRACT_HOUR", 7),
    }
