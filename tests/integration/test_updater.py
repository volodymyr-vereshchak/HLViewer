"""Updater plumbing: the DB-backed update_job lock (single row, id=1) and the
HostlibUpdater notification messages (Telegram/e-mail text only — nothing is
sent). The /update_data/ endpoints are exercised with update_hostlibs mocked.
"""

from datetime import datetime, timedelta

import pandas as pd
import pytest_asyncio
import sqlalchemy as sa

from backend.db.engine import async_session_factory
from backend.db.models import UpdateJob
from backend.hl_engine import update_job_lock
from backend.hl_engine.hostlib_updater import HostlibUpdater


@pytest_asyncio.fixture
async def update_job_row(clean_db):
    """The single update_job row (id=1). In production it is seeded by the
    Alembic migration; the test schema is built from metadata, so insert it."""
    async with async_session_factory() as session:
        session.add(UpdateJob(id=1, status="idle"))
        await session.commit()


async def _mark_stale():
    """Age the heartbeat past STALE_SECONDS."""
    async with async_session_factory() as session:
        await session.execute(
            sa.text(
                "UPDATE update_job SET updated_at = now() - make_interval("
                "secs => :age) WHERE id = 1"
            ),
            {"age": update_job_lock.STALE_SECONDS + 60},
        )
        await session.commit()


class TestUpdateJobLock:
    async def test_acquire_and_duplicate_guard(self, update_job_row):
        assert await update_job_lock.acquire() is not None
        # a second worker must NOT win while the job is running and fresh
        assert await update_job_lock.acquire() is None

    async def test_stale_job_can_be_taken_over(self, update_job_row):
        assert await update_job_lock.acquire() is not None
        await _mark_stale()
        assert await update_job_lock.acquire() is not None

    async def test_finalize_does_not_close_a_takeover(self, update_job_row):
        """The run that was replaced must not close the run that replaced it.

        A hung process whose lock was taken over used to write its own status
        over the new run — after which acquire() succeeds again and two hostlib
        updates go at once, which is what this lock exists to prevent. Status
        alone cannot tell the two apart: both of them are 'running'.
        """
        token_a = await update_job_lock.acquire()
        assert token_a is not None
        await _mark_stale()
        token_b = await update_job_lock.acquire()               # takes over
        assert token_b is not None and token_b != token_a

        # A wakes up and finishes, still holding its own token.
        await update_job_lock.finalize("done", None, {}, token_a)

        state = await update_job_lock.read()
        assert state["status"] == "running", "B's run was closed by A"
        assert await update_job_lock.acquire() is None, "the lock was released"

    async def test_heartbeat_of_a_replaced_run_does_not_hold_the_lock(
        self, update_job_row
    ):
        """A's heartbeat must not keep B's lock warm — or refresh a lock that
        is no longer A's, hiding B's own staleness."""
        token_a = await update_job_lock.acquire()
        await _mark_stale()
        token_b = await update_job_lock.acquire()
        await _mark_stale()

        await update_job_lock.heartbeat({"files": 1}, token_a)

        # Still stale from B's point of view, so B's lock can be taken over.
        assert await update_job_lock.acquire() is not None
        assert token_b is not None

    async def test_finalize_closes_its_own_run(self, update_job_row):
        token = await update_job_lock.acquire()
        assert token is not None
        await update_job_lock.finalize("done", None, {"files": 3}, token)

        state = await update_job_lock.read()
        assert state["status"] == "done"
        assert await update_job_lock.acquire() is not None

    async def test_read_idle(self, update_job_row):
        state = await update_job_lock.read()
        assert state["status"] == "idle"
        assert state["lumgs"] == {}

    async def test_read_stale_running_reports_error(self, update_job_row):
        await update_job_lock.acquire()
        await _mark_stale()
        state = await update_job_lock.read()
        assert state["status"] == "error"
        assert "перервано" in state["error"]

    async def test_read_without_row_defaults_to_idle(self, clean_db):
        state = await update_job_lock.read()
        assert state["status"] == "idle"

    async def test_run_job_success_persists_progress(self, update_job_row):
        await update_job_lock.acquire()

        async def work(session, progress):
            progress["7"] = {"status": "done", "counts": {"hourly": 3}}

        await update_job_lock.run_job(work)
        state = await update_job_lock.read()
        assert state["status"] == "done"
        assert state["error"] is None
        assert state["lumgs"] == {"7": {"status": "done", "counts": {"hourly": 3}}}

    async def test_run_job_failure_records_error(self, update_job_row):
        await update_job_lock.acquire()

        async def work(session, progress):
            raise RuntimeError("диск недоступний")

        await update_job_lock.run_job(work)
        state = await update_job_lock.read()
        assert state["status"] == "error"
        assert "диск недоступний" in state["error"]

    async def test_heartbeat_noop_when_not_running(self, update_job_row):
        # finished job must not be resurrected by a late heartbeat
        await update_job_lock.acquire()
        await update_job_lock.finalize("done", None, {})
        await update_job_lock.heartbeat({"9": {"status": "running"}})
        state = await update_job_lock.read()
        assert state["status"] == "done"
        assert state["lumgs"] == {}

    async def test_run_guarded_update_skips_when_busy(self, update_job_row):
        await update_job_lock.acquire()
        ran = []

        async def work(session, progress):
            ran.append(True)

        assert await update_job_lock.run_guarded_update(work) is False
        assert ran == []


class TestUpdateEndpoints:
    async def test_update_data_runs_mocked_job(
        self, admin_client, update_job_row, mocker
    ):
        called = mocker.AsyncMock()
        mocker.patch("backend.api.endpoints.root_ep.update_hostlibs", called)

        resp = await admin_client.post("/update_data/")
        assert resp.status_code == 202
        called.assert_awaited_once()

        # background task has completed by the time the transport returns
        status = (await admin_client.get("/update_data/status")).json()
        assert status["status"] == "done"

    async def test_update_data_429_when_running(self, admin_client, update_job_row):
        assert await update_job_lock.acquire() is not None
        resp = await admin_client.post("/update_data/")
        assert resp.status_code == 429

    async def test_reset_forces_idle(self, admin_client, update_job_row):
        await update_job_lock.acquire()
        resp = await admin_client.post("/update_data/reset")
        assert resp.status_code == 200
        assert resp.json()["status"] == "idle"


class TestGetReport:
    async def test_report_for_flagged_lines(self, admin_client, seed_topology):
        from backend.db.models import HourlyArchive, Line

        async with async_session_factory() as session:
            line = await session.get(Line, seed_topology["line1"])
            line.include_in_report = True
            session.add(line)
            for hour in range(24):
                session.add(
                    HourlyArchive(
                        period=datetime(2024, 12, 25, hour),
                        volume=10.0,
                        w_volume_dp=100.0,
                        pressure=5.0,
                        temperature=20.0,
                        density=0.7,
                        line_id=seed_topology["line1"],
                    )
                )
            await session.commit()

        resp = await admin_client.get("/get_report/")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True, body["message"]
        assert "<b>l1</b>" in body["message"]
        assert "240.0" in body["message"]  # 24 × 10 m³

    async def test_report_without_flagged_lines(self, admin_client, seed_topology):
        resp = await admin_client.get("/get_report/")
        body = resp.json()
        assert body["success"] is False
        assert "include_in_report" in body["message"]

    async def test_report_without_data(self, admin_client, seed_topology):
        from backend.db.models import Line

        async with async_session_factory() as session:
            line = await session.get(Line, seed_topology["line1"])
            line.include_in_report = True
            session.add(line)
            await session.commit()

        resp = await admin_client.get("/get_report/")
        body = resp.json()
        assert body["success"] is False


class TestNotificationMessages:
    def _df(self, line_id: int, hours: int = 24, volume: float = 10.0) -> pd.DataFrame:
        start = datetime(2024, 12, 25, 0)
        return pd.DataFrame(
            [
                {
                    "line_id": line_id,
                    "period": start + timedelta(hours=h),
                    "volume": volume,
                    "w_volume_dp": 100.0,
                    "pressure": 5.0,
                }
                for h in range(hours)
            ]
        )

    async def test_telegram_message_low_pressure_line(self, seed_topology):
        line1 = seed_topology["line1"]
        message = await HostlibUpdater.create_message(
            self._df(line1), {line1: False}
        )
        assert "Объем по ГРС за последние 24 часа" in message
        assert "<b>l1</b>" in message  # line name from DB
        assert "240.0" in message  # 24 × 10 m³
        assert "Pвых" in message  # low-pressure line label
        # meter=False → outlet pressure corrected by w_volume_dp/10000
        assert "4.99" in message
        # full 24 hours → no missing-data marker
        assert "🔴" not in message

    async def test_telegram_message_high_pressure_and_gaps(self, seed_topology):
        line1 = seed_topology["line1"]
        message = await HostlibUpdater.create_message(
            self._df(line1, hours=20), {line1: True}
        )
        assert "Pвх" in message  # high-pressure line label
        assert "🔴" in message  # fewer than 24 hourly records

    async def test_email_message_contains_table(self, seed_topology):
        line1 = seed_topology["line1"]
        message = await HostlibUpdater.create_email_message(
            self._df(line1), {line1: False}
        )
        assert "<html>" in message
        assert "l1" in message
        assert "240" in message
