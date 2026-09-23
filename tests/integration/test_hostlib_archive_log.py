"""The poller's log of archives it has read.

What it guarantees: every archive is read exactly once whatever it is called,
a failed one comes round again, a half-copied one waits, a corrupt one is
passed over, and a row is forgotten only once its file has left the folder.
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
    calls = {"updates": 0, "fail": False, "read": [], "order": []}

    async def active_paths():
        return [folder]

    async def run_guarded(work, lumg_id=None):
        await work(None, {})
        return True

    async def update(session, progress, archives_by_path=None):
        calls["updates"] += 1
        given = [os.path.basename(a)
                 for a in (archives_by_path or {}).get(folder, [])]
        calls["order"].append(given)
        calls["read"].append(sorted(given))
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

    async def test_every_archive_is_read_whatever_it_is_called(self, poller):
        """The name used to decide: it was taken apart into a source and a
        snapshot time, and only the newest of each source was read. A source
        that renamed its files would have gone unread without a word."""
        folder, calls = poller
        make_zip(folder, "Dnipropetr_2026_09_11_22.zip", age_sec=300)
        make_zip(folder, "Dnipropetr_2026_09_11_23.zip", age_sec=900)
        make_zip(folder, "as-of-today.zip", age_sec=600)

        await scheduler_runner.poll_once()

        assert calls["read"] == [["Dnipropetr_2026_09_11_22.zip",
                                  "Dnipropetr_2026_09_11_23.zip",
                                  "as-of-today.zip"]]
        assert sorted(r.filename for r in await log_rows()) == [
            "Dnipropetr_2026_09_11_22.zip", "Dnipropetr_2026_09_11_23.zip",
            "as-of-today.zip"]

    async def test_the_newest_is_read_first(self, poller):
        """Everything is read sooner or later; the order only keeps today's
        data from waiting behind last month's."""
        folder, calls = poller
        make_zip(folder, "old.zip", age_sec=90000)
        make_zip(folder, "new.zip", age_sec=300)
        await scheduler_runner.poll_once()
        assert calls["order"] == [["new.zip", "old.zip"]]
        assert len(await log_rows()) == 2

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

    async def test_a_broken_archive_is_written_down_and_left_alone(self, poller):
        """It does not open and it is not going to. Written down as broken, it
        is passed over without a word — it used to be looked at every tick and
        announced every time: 456 identical lines over two days, while nothing
        told anybody the share had a bad file on it."""
        folder, calls = poller
        path = os.path.join(folder, "Dnipropetr_2026_01_18_23.zip")
        with open(path, "wb") as f:
            f.write(b"PK half an archive")
        old = time.time() - 600
        os.utime(path, (old, old))

        await scheduler_runner.poll_once()
        await scheduler_runner.poll_once()

        assert calls["updates"] == 0
        rows = await log_rows()
        assert [(r.filename, r.status) for r in rows] == [
            ("Dnipropetr_2026_01_18_23.zip", "broken")]

    async def test_a_broken_archive_is_not_announced_twice(self, poller):
        """It was looked at and complained about every two minutes: 456
        identical lines over two days, while nothing told anybody."""
        folder, calls = poller
        path = os.path.join(folder, "Dnipropetr_2026_01_18_23.zip")
        with open(path, "wb") as f:
            f.write(b"PK\x03\x04 half an archive")
        old = time.time() - 600
        os.utime(path, (old, old))

        for _ in range(5):
            await scheduler_runner.poll_once()

        assert calls["updates"] == 0
        assert len(await log_rows()) == 1

    async def test_a_re_copied_archive_is_tried_again(self, poller):
        """Written down by name, size and time: the copy that replaces it is
        another file, and nothing has to be cleared by hand."""
        folder, calls = poller
        path = os.path.join(folder, "Dnipropetr_2026_09_11_23.zip")
        with open(path, "wb") as f:
            f.write(b"PK half an archive")
        old = time.time() - 600
        os.utime(path, (old, old))
        await scheduler_runner.poll_once()
        assert calls["updates"] == 0

        make_zip(folder, "Dnipropetr_2026_09_11_23.zip")
        await scheduler_runner.poll_once()
        assert calls["updates"] == 1
        # One row per file: the copy that was read replaces what was said
        # about the broken one.
        assert [r.status for r in await log_rows()] == ["ok"]

    async def test_a_row_is_forgotten_once_its_file_has_gone(self, poller):
        """The source moves yesterday's files out of the folder; a month on,
        the row that remembered one is of no use to anybody."""
        folder, calls = poller
        make_zip(folder, "today.zip")        # the folder still has something to say
        async with async_session_factory() as session:
            session.add(HostlibArchiveLog(
                path=folder, filename="moved-to-Arhiv.zip", size=1,
                file_mtime=datetime.now(), status="ok",
                processed_at=datetime.now() - scheduler_runner.FORGET_AFTER
                - timedelta(days=1),
            ))
            await session.commit()
        await scheduler_runner.poll_once()
        assert [r.filename for r in await log_rows()] == ["today.zip"]

    async def test_an_empty_folder_is_not_taken_at_its_word(self, poller):
        """An unmounted share lists as empty, exactly like a folder somebody
        emptied. Believing it would drop the log and read everything again
        when the share came back."""
        folder, calls = poller
        path = make_zip(folder, "Dnipropetr_2026_09_11_23.zip")
        await scheduler_runner.poll_once()

        async with async_session_factory() as session:
            for row in (await session.execute(select(HostlibArchiveLog))).scalars():
                row.processed_at -= scheduler_runner.FORGET_AFTER + timedelta(days=1)
            await session.commit()
        os.remove(path)                      # the share is not there right now

        await scheduler_runner.poll_once()
        assert len(await log_rows()) == 1

    async def test_a_file_that_is_still_there_is_remembered_however_old(self, poller):
        """Forgetting it is exactly what would have it read a second time."""
        folder, calls = poller
        path = make_zip(folder, "Dnipropetr_2026_09_11_23.zip")
        await scheduler_runner.poll_once()
        assert calls["updates"] == 1

        async with async_session_factory() as session:
            for row in (await session.execute(select(HostlibArchiveLog))).scalars():
                row.processed_at -= scheduler_runner.FORGET_AFTER + timedelta(days=1)
            await session.commit()

        await scheduler_runner.poll_once()
        assert calls["updates"] == 1                     # not read again
        assert len(await log_rows()) == 1
        assert os.path.exists(path)
