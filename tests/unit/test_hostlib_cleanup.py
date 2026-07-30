"""Orphan temp-dir sweep and the poller's retry bookkeeping.

Both guard the same incident: the API's startup hook used to sweep
hostlibs/__temp_*__ in all 8 uvicorn workers, deleting the directory the
scheduler was extracting into, and the poller then recorded that path as
processed anyway — so the batch was lost until the next file arrived."""

import os
import time

import pytest

from backend.hl_engine import scheduler_runner
from backend.hl_engine.main import _cleanup_orphan_temp_dirs
from backend.hl_engine.update_job_lock import STALE_SECONDS


def make_dir(root, name, age_sec: float = 0.0):
    path = root / "hostlibs" / name
    path.mkdir(parents=True)
    (path / "file.dat").write_bytes(b"x")
    if age_sec:
        old = time.time() - age_sec
        os.utime(path, (old, old))
    return path


class TestCleanupOrphanTempDirs:
    def test_removes_dirs_older_than_the_lock_window(self, tmp_path, monkeypatch):
        orphan = make_dir(tmp_path, "__temp_dead__", age_sec=STALE_SECONDS + 60)
        monkeypatch.chdir(tmp_path)

        _cleanup_orphan_temp_dirs()

        assert not orphan.exists()

    def test_keeps_a_dir_that_may_still_be_in_use(self, tmp_path, monkeypatch):
        live = make_dir(tmp_path, "__temp_live__")
        monkeypatch.chdir(tmp_path)

        _cleanup_orphan_temp_dirs()

        assert (live / "file.dat").exists()

    def test_leaves_real_data_dirs_alone(self, tmp_path, monkeypatch):
        data = make_dir(tmp_path, "ZP", age_sec=STALE_SECONDS + 60)
        monkeypatch.chdir(tmp_path)

        _cleanup_orphan_temp_dirs()

        assert (data / "file.dat").exists()


@pytest.fixture
def poller(monkeypatch):
    """poll_once with a single path whose file arrived long enough ago to be
    settled, and with the DB lock always granted."""
    scheduler_runner._last_sig.clear()
    sig = frozenset({("new.zip", 1.0, 10)})
    monkeypatch.setattr(
        scheduler_runner, "_scan",
        lambda: _async({"/data/ZP": (sig, time.time() - 600)}),
    )

    async def run_guarded(work, lumg_id=None):
        await work(None, {})
        return True

    monkeypatch.setattr(scheduler_runner, "run_guarded_update", run_guarded)
    return sig


def _async(value):
    async def coro():
        return value
    return coro()


class TestPollOnceRetries:
    async def test_successful_path_is_not_reprocessed(self, poller, monkeypatch):
        async def update(session, progress):
            return set()

        monkeypatch.setattr(scheduler_runner, "update_hostlibs", update)
        await scheduler_runner.poll_once()

        assert scheduler_runner._last_sig == {"/data/ZP": poller}

    async def test_failed_path_stays_pending(self, poller, monkeypatch):
        async def update(session, progress):
            return {"/data/ZP"}

        monkeypatch.setattr(scheduler_runner, "update_hostlibs", update)
        await scheduler_runner.poll_once()

        # Nothing recorded → the very next tick sees the same signature as new
        # and runs the path again instead of skipping it forever.
        assert scheduler_runner._last_sig == {}
