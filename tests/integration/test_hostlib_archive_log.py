"""The poller's day-long log of archives taken in.

What it guarantees: a new snapshot is taken in once, a failed one comes round
again, a half-copied one waits, and each source in a shared folder is watched
on its own.
"""
import os
import time
import zipfile
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from backend.db.engine import async_session_factory
from backend.db.models.hostlib_archive_log_model import HostlibArchiveLog
from backend.hl_engine import scheduler_runner

pytestmark = pytest.mark.asyncio


def make_zip(folder, name, age_sec=600):
    path = os.path.join(folder, name)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("x.txt", name)
    stamp = time.time() - age_sec
    os.utime(path, (stamp, stamp))
    return path


@pytest.fixture
def poller(tmp_path, monkeypatch):
    """poll_once over one folder, with the update stubbed and recorded."""
    folder = str(tmp_path)
    calls = {"updates": 0, "fail": False}

    async def active_paths():
        return [folder]

    async def run_guarded(work, lumg_id=None):
        await work(None, {})
        return True

    async def update(session, progress):
        calls["updates"] += 1
        return {folder} if calls["fail"] else set()

    async def dpd_lines():
        return None

    monkeypatch.setattr(scheduler_runner, "_active_paths", active_paths)
    monkeypatch.setattr(scheduler_runner, "run_guarded_update", run_guarded)
    monkeypatch.setattr(scheduler_runner, "update_hostlibs", update)
    monkeypatch.setattr(scheduler_runner.dpd_line_refresh, "run_update_all", dpd_lines)
    return folder, calls


async def log_rows():
    async with async_session_factory() as session:
        return (await session.execute(select(HostlibArchiveLog))).scalars().all()


class TestTheLog:
    async def test_a_new_snapshot_is_taken_in_once(self, poller):
        folder, calls = poller
        make_zip(folder, "Dnipropetr_2026_09_11_23.zip")

        await scheduler_runner.poll_once()
        await scheduler_runner.poll_once()           # nothing new this time

        assert calls["updates"] == 1
        rows = await log_rows()
        assert [(r.filename, r.status) for r in rows] == [("Dnipropetr_2026_09_11_23.zip", "ok")]

    async def test_it_survives_a_restart(self, poller):
        """The log is in the database, not in the process: a restart of the
        poller does not read the same snapshot again."""
        folder, calls = poller
        make_zip(folder, "Dnipropetr_2026_09_11_23.zip")
        await scheduler_runner.poll_once()

        # Nothing about what was taken in lives in the module any more — the
        # in-memory signature it used to keep is gone — so a new process has
        # only the log to go by, and the log says done.
        assert not hasattr(scheduler_runner, "_last_sig")
        rows = await log_rows()
        assert [(r.filename, r.status) for r in rows] == [
            ("Dnipropetr_2026_09_11_23.zip", "ok")
        ]
        await scheduler_runner.poll_once()
        assert calls["updates"] == 1

    async def test_the_next_hour_is_taken_in(self, poller):
        folder, calls = poller
        make_zip(folder, "Dnipropetr_2026_09_11_22.zip")
        await scheduler_runner.poll_once()
        make_zip(folder, "Dnipropetr_2026_09_11_23.zip")
        await scheduler_runner.poll_once()
        assert calls["updates"] == 2

    async def test_each_source_is_watched_on_its_own(self, poller):
        """A newer snapshot of one source must not hide a new one of another."""
        folder, calls = poller
        make_zip(folder, "UGV_DNP_2026_09_11_23.zip", age_sec=300)
        await scheduler_runner.poll_once()
        make_zip(folder, "Dnipropetr_2026_09_11_23.zip", age_sec=900)   # older file time
        await scheduler_runner.poll_once()
        assert calls["updates"] == 2

    async def test_the_name_decides_which_is_newest_not_the_file_time(self, poller):
        """A copy that keeps the original time would make hour 23 look older
        than hour 22."""
        folder, calls = poller
        make_zip(folder, "Dnipropetr_2026_09_11_22.zip", age_sec=300)
        make_zip(folder, "Dnipropetr_2026_09_11_23.zip", age_sec=900)
        await scheduler_runner.poll_once()
        rows = await log_rows()
        assert [r.filename for r in rows] == ["Dnipropetr_2026_09_11_23.zip"]

    async def test_a_failed_archive_comes_round_again_but_not_every_tick(self, poller):
        folder, calls = poller
        make_zip(folder, "Dnipropetr_2026_09_11_23.zip")
        calls["fail"] = True
        await scheduler_runner.poll_once()
        await scheduler_runner.poll_once()             # within RETRY_AFTER: waits
        assert calls["updates"] == 1
        assert [r.status for r in await log_rows()] == ["error"]

        async with async_session_factory() as session:
            for row in (await session.execute(select(HostlibArchiveLog))).scalars():
                row.processed_at -= scheduler_runner.RETRY_AFTER + timedelta(minutes=1)
            await session.commit()
        calls["fail"] = False
        await scheduler_runner.poll_once()
        assert calls["updates"] == 2

    async def test_a_file_still_being_copied_waits(self, poller):
        folder, calls = poller
        path = os.path.join(folder, "Dnipropetr_2026_09_11_23.zip")
        with open(path, "wb") as f:
            f.write(b"PK\x03\x04 half an archive")          # no central directory
        old = time.time() - 600
        os.utime(path, (old, old))
        await scheduler_runner.poll_once()
        assert calls["updates"] == 0

        make_zip(folder, "Dnipropetr_2026_09_11_23.zip")    # now complete
        await scheduler_runner.poll_once()
        assert calls["updates"] == 1

    async def test_a_day_old_entry_is_forgotten(self, poller):
        folder, calls = poller
        async with async_session_factory() as session:
            session.add(HostlibArchiveLog(
                path=folder, filename="old.zip", size=1,
                file_mtime=datetime.now(), status="ok",
                processed_at=datetime.now() - timedelta(days=2),
            ))
            await session.commit()
        await scheduler_runner.poll_once()
        assert await log_rows() == []
