"""The agent side of the poll: one whole session, end to end.

Step 3 of docs/plans/gsm-polling.md. Seven calls, authenticated by a key rather
than a cookie, and none of them is the server waiting for anything.

What is worth pinning is not the happy path — it is what the protocol refuses.
A poll is unattended, on a machine nobody is watching, so every one of these
would otherwise become a wrong number quietly written into a gas archive:

  * a reply from a serial that is not the one asked for;
  * a second agent dialling a device somebody is already on;
  * an agent writing to a device it was never given;
  * a failed attempt counted as a poll, which would leave the meter unread
    until somebody noticed by hand.
"""
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from backend.db.engine import async_session_factory
from backend.db.models.device_catalog_model import CorectorType, Manufacturer
from backend.db.models.enterprise_model import (
    DpdDevice, Enterprise, EnterpriseDevice,
)
from backend.db.models.grmu_branch_model import GrmuBranch
from backend.db.models.polling_model import PollDevice, PollLog

MARCH = datetime(2026, 3, 10, 7)


@pytest_asyncio.fixture
async def fleet(seed_users) -> dict:
    """A point with a ВЕГА fitted, and nothing polled yet."""
    async with async_session_factory() as session:
        branch = GrmuBranch(name="Тестова філія")
        session.add(branch)
        await session.flush()
        mfr = Manufacturer(short_name="Радміртех", full_name="Радміртех", mf_dev=1)
        session.add(mfr)
        await session.flush()
        ct = CorectorType(
            manufacturer_id=mfr.id, model_name="ВЕГА-1.01", type_dev=5,
            protocol_id=1054,
        )
        session.add(ct)
        await session.flush()
        point = Enterprise(
            enterprise_name="Завод А", branch_id=branch.id,
            active=True, enabled=True,
        )
        device = DpdDevice(ser_num=555001, ch_num=0, corector_type_id=ct.id)
        session.add(point)
        session.add(device)
        await session.flush()
        session.add(EnterpriseDevice(
            enterprise_id=point.id, device_id=device.id, installed_from=MARCH,
        ))
        await session.commit()
        return {"device_id": device.id, "enterprise_id": point.id}


async def make_agent(client, name: str = "АРМ Іваненко") -> dict:
    resp = await client.post("/polling/agents", json={"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def make_card(client, **body) -> dict:
    resp = await client.post("/polling/devices", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def key(agent: dict) -> dict:
    return {"X-Agent-Key": agent["key"]}


async def archive_rows(period: str) -> list:
    table = "dpd_hourly_archive" if period == "hourly" else "dpd_daily_archive"
    async with async_session_factory() as session:
        return (await session.execute(
            text(f"SELECT * FROM {table} ORDER BY device_id")
        )).mappings().all()


@pytest.mark.asyncio
class TestAuthentication:
    async def test_no_key_is_refused(self, anon_client):
        assert (await anon_client.get("/polling/agent/plan")).status_code == 401

    async def test_a_wrong_key_is_refused(self, anon_client):
        resp = await anon_client.get(
            "/polling/agent/plan", headers={"X-Agent-Key": "not-a-key"}
        )
        assert resp.status_code == 401

    async def test_a_switched_off_agent_is_refused(
        self, admin_client, anon_client, fleet
    ):
        # Switching an agent off has to stop it polling, not just hide it from
        # the list — otherwise it keeps dialling customers' meters.
        agent = await make_agent(admin_client)
        await admin_client.put(
            f"/polling/agents/{agent['id']}", json={"active": False}
        )
        resp = await anon_client.get("/polling/agent/plan", headers=key(agent))
        assert resp.status_code == 401

    async def test_a_cookie_is_not_enough(self, admin_client, fleet):
        # The agent routes are exempt from the session middleware, so they must
        # each check the key themselves; an admin session must not pass.
        assert (await admin_client.get("/polling/agent/plan")).status_code == 401


@pytest.mark.asyncio
class TestThePlan:
    async def test_an_agent_with_nothing_assigned_gets_nothing(
        self, admin_client, anon_client, fleet
    ):
        agent = await make_agent(admin_client)
        await make_card(admin_client, dpd_device_id=fleet["device_id"])

        plan = (await anon_client.get(
            "/polling/agent/plan", headers=key(agent)
        )).json()
        # The card exists, but nobody gave it to this agent.
        assert plan["devices"] == []

    async def test_the_agent_picks_its_own_devices(
        self, admin_client, anon_client, fleet
    ):
        agent = await make_agent(admin_client)
        card = await make_card(admin_client, dpd_device_id=fleet["device_id"])

        resp = await anon_client.put(
            "/polling/agent/devices",
            json={"device_ids": [card["id"]]},
            headers=key(agent),
        )
        assert resp.json() == [card["id"]]

        plan = (await anon_client.get(
            "/polling/agent/plan", headers=key(agent)
        )).json()
        assert [d["id"] for d in plan["devices"]] == [card["id"]]

    async def test_it_carries_what_is_needed_to_dial_and_to_check(
        self, admin_client, anon_client, fleet
    ):
        agent = await make_agent(admin_client)
        card = await make_card(
            admin_client, dpd_device_id=fleet["device_id"], phone="+380501234567"
        )
        await anon_client.put(
            "/polling/agent/devices",
            json={"device_ids": [card["id"]]},
            headers=key(agent),
        )

        device = (await anon_client.get(
            "/polling/agent/plan", headers=key(agent)
        )).json()["devices"][0]
        assert device["phone"] == "+380501234567"
        assert device["protocol_id"] == 1054       # from the ВЕГА model
        assert device["device_address"] == 1       # filled in, not asked for
        assert device["ser_num"] == 555001         # what must answer
        assert device["model_name"] == "ВЕГА-1.01"

    async def test_a_device_never_polled_is_due(
        self, admin_client, anon_client, fleet
    ):
        agent = await make_agent(admin_client)
        card = await make_card(admin_client, dpd_device_id=fleet["device_id"])
        await anon_client.put(
            "/polling/agent/devices",
            json={"device_ids": [card["id"]]},
            headers=key(agent),
        )
        device = (await anon_client.get(
            "/polling/agent/plan", headers=key(agent)
        )).json()["devices"][0]
        assert device["due"] is True
        assert device["due_reason"] == "never_polled"

    async def test_the_server_says_what_time_it_is(
        self, admin_client, anon_client, fleet
    ):
        # The agent logs against this rather than its own clock: a workstation
        # an hour fast would otherwise misreport when things happened.
        agent = await make_agent(admin_client)
        plan = (await anon_client.get(
            "/polling/agent/plan", headers=key(agent)
        )).json()
        assert plan["server_time"] is not None

    async def test_asking_for_a_plan_is_a_sign_of_life(
        self, admin_client, anon_client, fleet
    ):
        agent = await make_agent(admin_client)
        await anon_client.get("/polling/agent/plan", headers=key(agent))
        listed = (await admin_client.get("/polling/agents")).json()
        assert listed[0]["last_seen_at"] is not None


@pytest_asyncio.fixture
async def session_ready(admin_client, anon_client, fleet) -> dict:
    """An agent holding one device, ready to poll it."""
    agent = await make_agent(admin_client)
    card = await make_card(admin_client, dpd_device_id=fleet["device_id"])
    await anon_client.put(
        "/polling/agent/devices",
        json={"device_ids": [card["id"]]},
        headers=key(agent),
    )
    resp = await anon_client.post(
        f"/polling/agent/devices/{card['id']}/start", headers=key(agent)
    )
    assert resp.status_code == 200, resp.text
    return {"agent": agent, "card": card, **fleet}


@pytest.mark.asyncio
class TestAWholeSession:
    async def test_data_lands_in_the_archive_marked_as_gsm(
        self, anon_client, session_ready
    ):
        agent, card = session_ready["agent"], session_ready["card"]
        resp = await anon_client.post(
            f"/polling/agent/devices/{card['id']}/data",
            json={
                "ser_num": 555001,
                "period_type": "hourly",
                "rows": [
                    {"stamp": "2026-03-10T07:00:00", "volume": 12.5,
                     "pressure": 101.3, "temperature": 15.0},
                    {"stamp": "2026-03-10T08:00:00", "volume": 13.0},
                ],
            },
            headers=key(agent),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["stored"] == 2

        rows = await archive_rows("hourly")
        assert len(rows) == 2
        # The whole reason the source column exists: the nightly DPD refresh
        # must not overwrite what the modem read.
        assert {r["source"] for r in rows} == {"gsm"}
        assert rows[0]["dvst_alwrk"] == 12.5

    async def test_a_success_moves_last_poll_at(self, anon_client, session_ready):
        agent, card = session_ready["agent"], session_ready["card"]
        await anon_client.post(
            f"/polling/agent/devices/{card['id']}/finish",
            json={"status": "ok", "rows": {"hour": 2}, "duration_ms": 4200},
            headers=key(agent),
        )
        async with async_session_factory() as session:
            stored = await session.get(PollDevice, card["id"])
            assert stored.last_poll_at is not None
            assert stored.last_status == "ok"
            # The claim is released, so the device is free for the next round.
            assert stored.polling_agent_id is None

    async def test_a_failure_leaves_the_device_overdue(
        self, anon_client, session_ready
    ):
        """The rule the whole schedule rests on.

        If a failed attempt counted as a poll, the meter would go unread until
        somebody noticed by hand — and nothing on any screen would say so.
        """
        agent, card = session_ready["agent"], session_ready["card"]
        await anon_client.post(
            f"/polling/agent/devices/{card['id']}/finish",
            json={"status": "error", "error_code": "no_carrier",
                  "error_text": "No Carrier Detect"},
            headers=key(agent),
        )
        async with async_session_factory() as session:
            stored = await session.get(PollDevice, card["id"])
            assert stored.last_poll_at is None
            assert stored.last_attempt_at is not None
            assert stored.last_error_code == "no_carrier"

        plan = (await anon_client.get(
            "/polling/agent/plan", headers=key(agent)
        )).json()
        assert plan["devices"][0]["due"] is True

    async def test_a_manual_request_is_cleared_by_a_successful_poll(
        self, admin_client, anon_client, session_ready
    ):
        agent, card = session_ready["agent"], session_ready["card"]
        await admin_client.post(f"/polling/devices/{card['id']}/poll")
        await anon_client.post(
            f"/polling/agent/devices/{card['id']}/finish",
            json={"status": "ok"},
            headers=key(agent),
        )
        # Left standing, the request would poll the device forever.
        listed = (await admin_client.get("/polling/devices")).json()
        assert listed[0]["manual_requested_at"] is None

    async def test_the_log_is_replaced_each_session(
        self, anon_client, session_ready
    ):
        agent, card = session_ready["agent"], session_ready["card"]
        await anon_client.post(
            f"/polling/agent/devices/{card['id']}/log",
            json={"reset": True, "lines": [{"seq": 1, "message": "дзвоню"}]},
            headers=key(agent),
        )
        resp = await anon_client.post(
            f"/polling/agent/devices/{card['id']}/log",
            json={"lines": [{"seq": 2, "message": "CONNECT 9600"}]},
            headers=key(agent),
        )
        assert resp.json()["last_seq"] == 2

        # A second session resets: the screen answers "what is happening now",
        # and the history of failures lives in poll_attempt, one row each.
        await anon_client.post(
            f"/polling/agent/devices/{card['id']}/log",
            json={"reset": True, "lines": [{"seq": 1, "message": "дзвоню знову"}]},
            headers=key(agent),
        )
        async with async_session_factory() as session:
            lines = list((await session.execute(
                select(PollLog).where(PollLog.poll_device_id == card["id"])
            )).scalars())
        assert [line.message for line in lines] == ["дзвоню знову"]


@pytest.mark.asyncio
class TestWhatTheProtocolRefuses:
    async def test_a_reply_from_the_wrong_serial_is_not_stored(
        self, anon_client, session_ready
    ):
        """The reason a card names a corrector rather than a site.

        A different serial on the line means either a replacement nobody
        recorded or a call that reached the wrong place. Writing one meter's
        archive under another's name is worse than a missing reading.
        """
        agent, card = session_ready["agent"], session_ready["card"]
        resp = await anon_client.post(
            f"/polling/agent/devices/{card['id']}/data",
            json={
                "ser_num": 999999,
                "period_type": "hourly",
                "rows": [{"stamp": "2026-03-10T07:00:00", "volume": 12.5}],
            },
            headers=key(agent),
        )
        assert resp.status_code == 409
        assert "999999" in resp.json()["detail"]
        assert await archive_rows("hourly") == []

    async def test_a_second_agent_cannot_take_a_busy_device(
        self, admin_client, anon_client, session_ready
    ):
        # Two operators can tick the same device; this is how they avoid
        # dialling it at the same moment. 409 is a normal answer.
        other = await make_agent(admin_client, "АРМ Петренко")
        card = session_ready["card"]
        await anon_client.put(
            "/polling/agent/devices",
            json={"device_ids": [card["id"]]},
            headers=key(other),
        )
        resp = await anon_client.post(
            f"/polling/agent/devices/{card['id']}/start", headers=key(other)
        )
        assert resp.status_code == 409

    async def test_a_stale_claim_releases_itself(
        self, admin_client, anon_client, session_ready
    ):
        """An agent switched off mid-session must not hold a device forever,
        and there is no cleanup task to rely on."""
        card = session_ready["card"]
        async with async_session_factory() as session:
            await session.execute(text(
                "UPDATE poll_device SET polling_since = now() "
                "- interval '21 minutes' WHERE id = :id"
            ), {"id": card["id"]})
            await session.commit()

        other = await make_agent(admin_client, "АРМ Петренко")
        await anon_client.put(
            "/polling/agent/devices",
            json={"device_ids": [card["id"]]},
            headers=key(other),
        )
        resp = await anon_client.post(
            f"/polling/agent/devices/{card['id']}/start", headers=key(other)
        )
        assert resp.status_code == 200

    async def test_an_agent_cannot_write_to_a_device_it_was_not_given(
        self, admin_client, anon_client, session_ready
    ):
        other = await make_agent(admin_client, "АРМ Петренко")
        card = session_ready["card"]
        resp = await anon_client.post(
            f"/polling/agent/devices/{card['id']}/data",
            json={"period_type": "hourly", "rows": []},
            headers=key(other),
        )
        assert resp.status_code == 403

    async def test_an_unknown_period_type_is_refused(
        self, anon_client, session_ready
    ):
        agent, card = session_ready["agent"], session_ready["card"]
        resp = await anon_client.post(
            f"/polling/agent/devices/{card['id']}/data",
            json={"period_type": "instant", "rows": []},
            headers=key(agent),
        )
        assert resp.status_code == 400


@pytest.mark.asyncio
class TestIncrementalReading:
    async def test_the_plan_says_where_to_read_from(
        self, anon_client, session_ready
    ):
        """No coverage table: the answer is MAX() over the archive itself.

        DPD needed one because it backfills into the past through a metered
        API. A modem only ever reads forward from the last record, so a
        separate ledger could only drift from what is actually stored.
        """
        agent, card = session_ready["agent"], session_ready["card"]
        await anon_client.post(
            f"/polling/agent/devices/{card['id']}/data",
            json={
                "ser_num": 555001,
                "period_type": "hourly",
                "rows": [{"stamp": "2026-03-10T08:00:00", "volume": 13.0}],
            },
            headers=key(agent),
        )
        device = (await anon_client.get(
            "/polling/agent/plan", headers=key(agent)
        )).json()["devices"][0]
        assert device["last_hour"].startswith("2026-03-10T08:00")

    async def test_re_polling_the_same_period_is_safe(
        self, anon_client, session_ready
    ):
        # The unique constraint turns it into an update of the same values, so
        # two agents reaching the same device cannot double the archive.
        agent, card = session_ready["agent"], session_ready["card"]
        payload = {
            "ser_num": 555001,
            "period_type": "daily",
            "rows": [{"stamp": "2026-03-10T00:00:00", "volume": 100.0}],
        }
        await anon_client.post(
            f"/polling/agent/devices/{card['id']}/data",
            json=payload, headers=key(agent),
        )
        await anon_client.post(
            f"/polling/agent/devices/{card['id']}/data",
            json=payload, headers=key(agent),
        )
        assert len(await archive_rows("daily")) == 1
