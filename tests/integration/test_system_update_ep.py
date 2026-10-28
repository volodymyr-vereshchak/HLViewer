"""/system-update: the backend's door to the updater container.

The updater itself is replaced by an httpx mock transport; what is tested is
the door — off unless enabled, admins only, the token and the admin's name
passed on, the updater's own refusals passed back.
"""
import json

import httpx
import pytest

from tests.conftest import ADMIN_USERNAME

from backend.api.endpoints import system_update_ep

_RealAsyncClient = httpx.AsyncClient


@pytest.fixture
def updater(monkeypatch):
    """A fake updater; records requests, answers from `replies`."""
    seen: list[httpx.Request] = []
    replies: dict[str, tuple[int, dict]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        code, body = replies.get(f"{request.method} {request.url.path}", (200, {"ok": True}))
        return httpx.Response(code, json=body)

    monkeypatch.setenv("UPDATER_ENABLED", "true")
    monkeypatch.setenv("UPDATER_TOKEN", "shared-secret")
    monkeypatch.setattr(
        system_update_ep.httpx, "AsyncClient",
        lambda **kw: _RealAsyncClient(transport=httpx.MockTransport(handler), **kw),
    )
    return seen, replies


async def test_disabled_by_default(admin_client, monkeypatch):
    monkeypatch.delenv("UPDATER_ENABLED", raising=False)
    assert (await admin_client.get("/system-update/enabled")).json() == {"enabled": False}
    assert (await admin_client.get("/system-update/status")).status_code == 404


async def test_viewers_are_refused(viewer_client, updater):
    assert (await viewer_client.get("/system-update/status")).status_code == 403
    assert (await viewer_client.post("/system-update/start", json={"target": {}})).status_code == 403
    assert updater[0] == []


async def test_start_passes_token_and_admin(admin_client, updater):
    seen, replies = updater
    replies["POST /start"] = (200, {"job_id": "20261028-120000", "watch_token": "w"})
    resp = await admin_client.post("/system-update/start", json={"target": {"backend": "b" * 40}})
    assert resp.status_code == 200
    assert resp.json()["watch_token"] == "w"
    request = seen[-1]
    assert request.headers["X-Updater-Token"] == "shared-secret"
    assert json.loads(request.content) == {"target": {"backend": "b" * 40}, "started_by": ADMIN_USERNAME}


async def test_status_adds_the_running_commit(admin_client, updater):
    body = (await admin_client.get("/system-update/status")).json()
    assert "running_backend" in body


async def test_updater_refusal_is_passed_back(admin_client, updater):
    _, replies = updater
    replies["POST /start"] = (409, {"detail": "Оновлення вже виконується"})
    resp = await admin_client.post("/system-update/start", json={"target": {"backend": "b" * 40}})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Оновлення вже виконується"


async def test_updater_down_is_503(admin_client, monkeypatch):
    monkeypatch.setenv("UPDATER_ENABLED", "true")
    monkeypatch.setenv("UPDATER_URL", "http://127.0.0.1:9")
    resp = await admin_client.get("/system-update/status")
    assert resp.status_code == 503
    assert "hlviewer_updater" in resp.json()["detail"]
