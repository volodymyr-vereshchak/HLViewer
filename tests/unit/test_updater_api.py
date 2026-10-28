"""The updater's HTTP surface: who may give orders, who may watch."""
import dataclasses
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from updater import main
from updater.store import Store

TOKEN = "backend-secret"
SHA = "b" * 40


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, token=TOKEN))
    store = Store(tmp_path / "update")
    platform = SimpleNamespace(project_dir=tmp_path, frontend_dir=tmp_path,
                               with_log=lambda log: platform)
    monkeypatch.setattr(main.runtime, "_context", (tmp_path, platform, store))
    monkeypatch.setattr(main.runtime, "job", None)
    # The worker itself is the pipeline's business; here it only must not run.
    monkeypatch.setattr(main, "_run", lambda *a: None)
    monkeypatch.setattr(main.runtime, "forget", lambda: None)
    return TestClient(main.app)


def _start(client, **headers):
    return client.post("/start", json={"target": {"backend": SHA}, "started_by": "admin"},
                       headers=headers)


def test_orders_need_the_backend_token(client):
    assert _start(client).status_code == 401
    assert _start(client, **{"X-Updater-Token": "wrong"}).status_code == 401
    assert client.get("/history").status_code == 401


def test_no_token_configured_refuses_everything(client, monkeypatch):
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, token=""))
    assert _start(client, **{"X-Updater-Token": ""}).status_code == 503


def test_target_must_be_full_commit_ids(client):
    resp = client.post("/start", json={"target": {"backend": "HEAD; rm -rf /"}},
                       headers={"X-Updater-Token": TOKEN})
    assert resp.status_code == 422


def test_one_update_at_a_time(client):
    first = _start(client, **{"X-Updater-Token": TOKEN})
    assert first.status_code == 200
    assert _start(client, **{"X-Updater-Token": TOKEN}).status_code == 409


def test_watch_needs_the_token_of_that_job(client):
    started = _start(client, **{"X-Updater-Token": TOKEN}).json()
    url = f"/updater/watch/{started['job_id']}"
    assert client.get(url).status_code == 404
    assert client.get(url, headers={"X-Watch-Token": "guess"}).status_code == 404
    # The backend token is not a watch token either.
    assert client.get(url, headers={"X-Watch-Token": TOKEN}).status_code == 404
    resp = client.get(url, headers={"X-Watch-Token": started["watch_token"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "running"
    assert "watch_token" not in body
    assert [s["id"] for s in body["steps"]] == ["prepare", "build", "backup", "switch", "verify"]


def test_job_read_by_backend_hides_the_watch_token(client):
    started = _start(client, **{"X-Updater-Token": TOKEN}).json()
    body = client.get(f"/jobs/{started['job_id']}", headers={"X-Updater-Token": TOKEN}).json()
    assert body["id"] == started["job_id"]
    assert "watch_token" not in body


@pytest.mark.parametrize("url", [
    "https://user:tok@github.com/o/r.git",
    "file:///etc",
    "--upload-pack=sh",
    "ssh://github.com/o/r.git",
])
def test_repository_url_is_validated(client, url):
    body = {"repos": {"backend": {"url": url, "branch": "master"},
                      "frontend": {"url": "https://github.com/o/f.git", "branch": "main"}}}
    resp = client.put("/config", json=body, headers={"X-Updater-Token": TOKEN})
    assert resp.status_code == 422


def test_token_is_stored_privately_and_never_returned(client, tmp_path, monkeypatch):
    monkeypatch.setattr(main, "_repos", lambda platform, shell=None: {
        "backend": SimpleNamespace(origin_url=lambda: "", branch=lambda: "master"),
        "frontend": SimpleNamespace(origin_url=lambda: "", branch=lambda: "main"),
    })
    body = {"repos": {"backend": {"url": "https://github.com/o/b.git", "branch": "master"},
                      "frontend": {"url": "https://github.com/o/f.git", "branch": "main"}},
            "token": "ghp_secret"}
    resp = client.put("/config", json=body, headers={"X-Updater-Token": TOKEN})
    assert resp.status_code == 200
    assert resp.json()["token_set"] is True
    assert "ghp_secret" not in resp.text
    stored = Path(tmp_path / "update" / "config.json")
    assert "ghp_secret" in stored.read_text()
    # Saving without a token keeps the one stored; an empty one removes it.
    body.pop("token")
    assert client.put("/config", json=body, headers={"X-Updater-Token": TOKEN}).json()["token_set"] is True
    body["token"] = ""
    assert client.put("/config", json=body, headers={"X-Updater-Token": TOKEN}).json()["token_set"] is False
