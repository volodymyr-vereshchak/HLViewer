"""Endpoint that receives browser-side error reports and writes them to
logs/frontend.log (see backend/logging_config.py). Unauthenticated on purpose —
errors can happen before login — but every field is length-capped so the log
cannot be flooded by a malicious client.
"""

from typing import Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel

from backend.logging_config import get_frontend_logger

router = APIRouter(tags=["logs"])

_frontend_logger = get_frontend_logger()

_MAX_MESSAGE = 2000
_MAX_STACK = 8000
_MAX_UA = 300
_MAX_URL = 500


class ClientLog(BaseModel):
    level: Optional[str] = "error"
    message: str
    stack: Optional[str] = None
    url: Optional[str] = None
    userAgent: Optional[str] = None


@router.post("/client-log")
async def client_log(entry: ClientLog, request: Request):
    ip = request.client.host if request.client else "?"
    message = (entry.message or "")[:_MAX_MESSAGE]
    stack = (entry.stack or "")[:_MAX_STACK]
    url = (entry.url or "")[:_MAX_URL]
    user_agent = (entry.userAgent or "")[:_MAX_UA]
    level = (entry.level or "error").lower()

    line = "CLIENT [%s] %s | url=%s ua=%s%s" % (
        ip,
        message,
        url,
        user_agent,
        ("\n" + stack) if stack else "",
    )

    if level in ("error", "fatal"):
        _frontend_logger.error(line)
    elif level in ("warn", "warning"):
        _frontend_logger.warning(line)
    else:
        _frontend_logger.info(line)

    return {"ok": True}


logs_router = router
