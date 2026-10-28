"""«Версія програми»: the admin side of the application update.

The update itself is done by the updater container (see updater/): this
process cannot replace the container it runs in. These routes are the only
way to give it orders — the admin check happens here, where the account is
re-read on every request, and the updater trusts whoever holds UPDATER_TOKEN.

Off unless UPDATER_ENABLED=true: the Windows server runs without Docker and
is updated by hand, and there the tab simply does not exist.
"""
import os
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from backend.api.endpoints.auth_ep import require_admin
from backend.db.models.app_user_model import AppUser

router = APIRouter(prefix="/system-update", tags=["system-update"])

REPO_ROOT = Path(__file__).resolve().parents[3]
# A check fetches two repositories; nginx gives this prefix more than this.
CHECK_TIMEOUT = 150.0
DEFAULT_TIMEOUT = 20.0


def _enabled() -> bool:
    return os.getenv("UPDATER_ENABLED", "false").strip().lower() in ("1", "true", "yes")


def _read_head(repo: Path) -> str | None:
    """The commit this process was started from. Read once at import, so it
    stays what is RUNNING even after the checkout moves under it."""
    git = repo / ".git"
    try:
        head = (git / "HEAD").read_text().strip()
        if not head.startswith("ref: "):
            return head
        ref = head[5:]
        if (git / ref).exists():
            return (git / ref).read_text().strip()
        for line in (git / "packed-refs").read_text().splitlines():
            if line.endswith(f" {ref}"):
                return line.split()[0]
    except OSError:
        pass
    return None


RUNNING_COMMIT = _read_head(REPO_ROOT)


async def _call(method: str, path: str, json: dict | None = None,
                timeout: float = DEFAULT_TIMEOUT):
    if not _enabled():
        raise HTTPException(404, "Оновлення з адмінки на цьому сервері вимкнено")
    url = os.getenv("UPDATER_URL", "http://updater:8090").rstrip("/") + path
    headers = {"X-Updater-Token": os.getenv("UPDATER_TOKEN", "")}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(method, url, json=json, headers=headers)
    except httpx.TimeoutException as exc:
        raise HTTPException(504, "Updater не відповів вчасно") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            503, "Updater недоступний — перевірте контейнер hlviewer_updater"
        ) from exc
    try:
        body = resp.json()
    except ValueError:
        body = {"detail": resp.text[:500]}
    if resp.status_code >= 400:
        return JSONResponse(status_code=resp.status_code, content=body)
    return body


@router.get("/enabled")
async def enabled(_: AppUser = Depends(require_admin)):
    return {"enabled": _enabled()}


@router.get("/status")
async def status(_: AppUser = Depends(require_admin)):
    body = await _call("GET", "/status")
    if isinstance(body, dict):
        body["running_backend"] = RUNNING_COMMIT
    return body


@router.post("/check")
async def check(_: AppUser = Depends(require_admin)):
    return await _call("POST", "/check", timeout=CHECK_TIMEOUT)


class StartBody(BaseModel):
    target: dict[str, str]


@router.post("/start")
async def start(body: StartBody, user: AppUser = Depends(require_admin)):
    return await _call("POST", "/start", json={
        "target": body.target,
        "started_by": user.display_name or user.username,
    })


@router.get("/config")
async def get_config(_: AppUser = Depends(require_admin)):
    return await _call("GET", "/config")


class RepoBody(BaseModel):
    url: str
    branch: str


class ConfigBody(BaseModel):
    repos: dict[str, RepoBody]
    token: str | None = None


@router.put("/config")
async def put_config(body: ConfigBody, _: AppUser = Depends(require_admin)):
    return await _call("PUT", "/config", json=body.model_dump())


@router.get("/history")
async def history(_: AppUser = Depends(require_admin)):
    return await _call("GET", "/history")


@router.get("/jobs/{job_id}")
async def job(job_id: str, _: AppUser = Depends(require_admin)):
    if not job_id.replace("-", "").isdigit():
        raise HTTPException(404, "Немає такого оновлення")
    return await _call("GET", f"/jobs/{job_id}")
