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
from backend.db.models.polling_model import PollAgent, PollDevice
from backend.settings import backend_settings

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

    async def test_a_negative_volume_is_not_stored(self, anon_client, session_ready):
        """A totaliser set back — installation, a cleared archive — measured
        across the jump. Not gas; stored, it takes a thousand cubic metres off a
        day. Refused here whatever the agent's build, the rest of the batch kept.
        """
        agent, card = session_ready["agent"], session_ready["card"]
        resp = await anon_client.post(
            f"/polling/agent/devices/{card['id']}/data",
            json={
                "ser_num": 555001,
                "period_type": "hourly",
                "rows": [
                    {"stamp": "2026-03-10T07:00:00", "volume": 12.5},
                    {"stamp": "2026-03-10T08:00:00", "volume": -4970.0},
                    {"stamp": "2026-03-10T09:00:00", "volume": 3.0,
                     "volume_work": -1.0},
                ],
            },
            headers=key(agent),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["stored"] == 1

        rows = await archive_rows("hourly")
        assert [r["dvst_alwrk"] for r in rows] == [12.5]

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

        # Not this minute, though: a line that was dead a moment ago is dead
        # now, and redialling at plan speed is a phone bill, not a retry.
        plan = (await anon_client.get(
            "/polling/agent/plan", headers=key(agent)
        )).json()
        assert plan["devices"][0]["due"] is False
        assert plan["devices"][0]["due_reason"] == "cooling_off"

        async with async_session_factory() as session:
            stored = await session.get(PollDevice, card["id"])
            stored.last_attempt_at = datetime.now() - timedelta(minutes=20)
            await session.commit()

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

    async def test_a_failed_manual_request_is_cleared_as_well(
        self, admin_client, anon_client, session_ready
    ):
        """The button is one call, not a standing order.

        Cleared only on success, a manual request that failed stayed newer
        than `last_poll_at` for ever — and that is exactly what the plan reads
        to decide a device is due. The agent redialled every fifteen seconds,
        three attempts a time, and nothing in the loop could end it: each
        failure left the state that caused it. Whether to try again is the
        decision of whoever pressed the button and watched it fail.
        """
        agent, card = session_ready["agent"], session_ready["card"]
        await admin_client.post(f"/polling/devices/{card['id']}/poll")
        await anon_client.post(
            f"/polling/agent/devices/{card['id']}/finish",
            json={"status": "error", "error_code": "no_carrier",
                  "error_text": "No Carrier Detect"},
            headers=key(agent),
        )

        listed = (await admin_client.get("/polling/devices")).json()
        assert listed[0]["manual_requested_at"] is None

        plan = (await anon_client.get(
            "/polling/agent/plan", headers=key(agent)
        )).json()
        assert plan["devices"][0]["due"] is False

    async def test_the_progress_of_a_long_read_keeps_the_agent_online(
        self, anon_client, session_ready, tmp_path, monkeypatch
    ):
        """A call is the one time an agent cannot say hello.

        `/state` goes between sessions, and a ВЕГА takes a hundred seconds for
        a day of hours — an hour for a first fill, one Modbus frame per record.
        The screen watching that poll used to show the agent offline while it
        was working, because nothing else touched `last_seen_at`.
        """
        monkeypatch.setitem(backend_settings, "POLL_LOG_DIR", str(tmp_path))
        agent, card = session_ready["agent"], session_ready["card"]

        async with async_session_factory() as session:
            row = await session.get(PollAgent, agent["id"])
            row.last_seen_at = datetime.now() - timedelta(minutes=5)
            await session.commit()

        await anon_client.post(
            f"/polling/agent/devices/{card['id']}/log",
            json={"lines": [], "progress": {"done": 120, "total": 720}},
            headers=key(agent),
        )

        async with async_session_factory() as session:
            row = await session.get(PollAgent, agent["id"])
            assert datetime.now() - row.last_seen_at < timedelta(seconds=30)

    async def test_the_log_is_replaced_each_session(
        self, anon_client, session_ready, tmp_path, monkeypatch
    ):
        monkeypatch.setitem(backend_settings, "POLL_LOG_DIR", str(tmp_path))
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
        text = _journal(tmp_path, session_ready)
        assert "дзвоню знову" in text
        assert "CONNECT 9600" not in text

    async def test_a_raw_wire_dump_does_not_break_the_log(
        self, anon_client, session_ready, tmp_path, monkeypatch
    ):
        """A driver's own trace carries the bytes it read, NUL padding and all.

        A control byte inside a line would split or truncate it for every
        reader afterwards, and the request carrying it is the report of a call
        that already happened and cannot be repeated — so losing it to a 500
        loses the only account of the failure.
        """
        monkeypatch.setitem(backend_settings, "POLL_LOG_DIR", str(tmp_path))
        agent, card = session_ready["agent"], session_ready["card"]
        resp = await anon_client.post(
            f"/polling/agent/devices/{card['id']}/log",
            json={"reset": True, "lines": [
                {"seq": 1, "message": "<-- VegaCoL\x00\x01!\x00\x00 2311"},
            ]},
            headers=key(agent),
        )
        assert resp.status_code == 200

        text = _journal(tmp_path, session_ready)
        assert "\x00" not in text
        assert "VegaCoL" in text and "2311" in text


def _journal(folder, session_ready) -> str:
    """The site's journal file, whatever it is named for this card."""
    written = list(folder.glob("*.log"))
    assert len(written) == 1, written
    return written[0].read_text(encoding="utf-8")


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


@pytest.mark.asyncio
class TestACardThatFollowsTheEnterprise:
    """The modem is at the site; the corrector under it gets replaced."""

    async def test_the_plan_names_the_corrector_fitted_today(
        self, admin_client, anon_client, fleet
    ):
        agent = await make_agent(admin_client)
        card = await make_card(
            admin_client, enterprise_id=fleet["enterprise_id"],
            phone="+380501234567",
        )
        await anon_client.put("/polling/agent/devices",
                              json={"device_ids": [card["id"]]}, headers=key(agent))

        plan = (await anon_client.get("/polling/agent/plan", headers=key(agent))).json()
        assert len(plan["devices"]) == 1
        device = plan["devices"][0]
        assert device["ser_num"] == 555001
        # Nothing set this on the card: it comes from the corrector's model.
        assert device["protocol_id"] == 1054

    async def test_a_replacement_needs_nobody_to_repoint_anything(
        self, admin_client, anon_client, fleet
    ):
        """The old corrector comes off, a new one goes on, the card is untouched.

        This is the whole reason the card names the enterprise. Under the old
        arrangement the card kept the old serial, and the readings from the new
        meter arrived under a name that had left the site.
        """
        agent = await make_agent(admin_client)
        card = await make_card(
            admin_client, enterprise_id=fleet["enterprise_id"],
            phone="+380501234567",
        )
        await anon_client.put("/polling/agent/devices",
                              json={"device_ids": [card["id"]]}, headers=key(agent))

        async with async_session_factory() as session:
            fitted = (await session.execute(
                select(EnterpriseDevice).where(
                    EnterpriseDevice.enterprise_id == fleet["enterprise_id"]
                )
            )).scalars().one()
            fitted.removed_at = datetime(2026, 6, 1)
            newer = DpdDevice(ser_num=555002, ch_num=0,
                              corector_type_id=None)
            session.add(newer)
            await session.flush()
            session.add(EnterpriseDevice(
                enterprise_id=fleet["enterprise_id"], device_id=newer.id,
                installed_from=datetime(2026, 6, 1),
            ))
            await session.commit()

        plan = (await anon_client.get("/polling/agent/plan", headers=key(agent))).json()
        assert plan["devices"][0]["ser_num"] == 555002

    async def test_a_site_with_nothing_fitted_is_not_dialled(
        self, admin_client, anon_client, fleet
    ):
        """A call to a modem with no meter behind it looks like a dead meter.

        Better to say why before the phone is picked up.
        """
        agent = await make_agent(admin_client)
        card = await make_card(
            admin_client, enterprise_id=fleet["enterprise_id"],
            phone="+380501234567",
        )
        await anon_client.put("/polling/agent/devices",
                              json={"device_ids": [card["id"]]}, headers=key(agent))

        async with async_session_factory() as session:
            fitted = (await session.execute(
                select(EnterpriseDevice).where(
                    EnterpriseDevice.enterprise_id == fleet["enterprise_id"]
                )
            )).scalars().one()
            fitted.removed_at = datetime(2026, 6, 1)
            await session.commit()

        device = (await anon_client.get(
            "/polling/agent/plan", headers=key(agent)
        )).json()["devices"][0]
        assert device["due"] is False
        assert device["due_reason"] == "немає встановлених корректорів"
        assert device["ser_num"] is None

    async def test_a_card_needs_exactly_one_target(self, admin_client, fleet):
        resp = await admin_client.post("/polling/devices", json={
            "enterprise_id": fleet["enterprise_id"],
            "dpd_device_id": fleet["device_id"],
        })
        assert resp.status_code == 422


@pytest.mark.asyncio
class TestGivingUpOnALineThatWillNotAnswer:
    """Three calls per scheduled slot, then quiet until the next one.

    The retry pause alone still meant a dead line was dialled four times an
    hour until somebody noticed. Three attempts cover what a retry can fix;
    after that the answer stops changing.
    """

    async def test_the_fourth_attempt_is_not_offered(
        self, admin_client, anon_client, session_ready
    ):
        agent, card = session_ready["agent"], session_ready["card"]
        headers = key(agent)

        for _ in range(3):
            await anon_client.post(
                f"/polling/agent/devices/{card['id']}/start", headers=headers)
            await anon_client.post(
                f"/polling/agent/devices/{card['id']}/finish",
                json={"status": "error", "error_code": "no_carrier",
                      "error_text": "No Carrier Detect", "rows": {},
                      "duration_ms": 1000, "connect_ms": 0},
                headers=headers,
            )
            # The pause between attempts is not what is being tested here.
            async with async_session_factory() as session:
                stored = await session.get(PollDevice, card["id"])
                stored.last_attempt_at = datetime.now() - timedelta(minutes=20)
                await session.commit()

        plan = (await anon_client.get(
            "/polling/agent/plan", headers=headers)).json()
        assert plan["devices"][0]["due"] is False
        assert plan["devices"][0]["due_reason"] == "gave_up"

    async def test_a_manual_failure_does_not_spend_the_budget(
        self, admin_client, anon_client, session_ready
    ):
        """Checking a failing site by hand must not remove its retries.

        Otherwise the operator who looks at the problem is the reason the
        schedule stops trying — which is exactly backwards.
        """
        agent, card = session_ready["agent"], session_ready["card"]
        headers = key(agent)

        # Two scheduled failures, and one the operator asked for in between.
        for manual in (False, True, False):
            if manual:
                await admin_client.post(f"/polling/devices/{card['id']}/poll")
            await anon_client.post(
                f"/polling/agent/devices/{card['id']}/start", headers=headers)
            await anon_client.post(
                f"/polling/agent/devices/{card['id']}/finish",
                json={"status": "error", "error_code": "no_carrier",
                      "error_text": "No Carrier Detect", "rows": {},
                      "duration_ms": 1000, "connect_ms": 0},
                headers=headers,
            )
            async with async_session_factory() as session:
                stored = await session.get(PollDevice, card["id"])
                stored.last_attempt_at = datetime.now() - timedelta(minutes=20)
                await session.commit()

        # Three calls have been made, but only two of them were the schedule's.
        plan = (await anon_client.get(
            "/polling/agent/plan", headers=headers)).json()
        assert plan["devices"][0]["due"] is True

    async def test_a_person_may_still_ask_for_it(
        self, admin_client, anon_client, session_ready
    ):
        """Somebody who knows the line is back does not wait for a slot."""
        agent, card = session_ready["agent"], session_ready["card"]
        headers = key(agent)

        for _ in range(3):
            await anon_client.post(
                f"/polling/agent/devices/{card['id']}/start", headers=headers)
            await anon_client.post(
                f"/polling/agent/devices/{card['id']}/finish",
                json={"status": "error", "error_code": "no_carrier",
                      "error_text": "No Carrier Detect", "rows": {},
                      "duration_ms": 1000, "connect_ms": 0},
                headers=headers,
            )

        await admin_client.post(f"/polling/devices/{card['id']}/poll")

        plan = (await anon_client.get(
            "/polling/agent/plan", headers=headers)).json()
        assert plan["devices"][0]["due"] is True
        assert plan["devices"][0]["due_reason"] == "manual_request"


@pytest.mark.asyncio
class TestTwoAgentsOnOneSite:
    """The same site given to two machines — the shape that makes a poll
    survive one of them being switched off.

    Nothing divides the sites between agents: both see the same plan and the
    claim settles who dials. What has to hold is that the one who loses the
    race is told, rather than left asking again as fast as the network allows.
    """

    async def test_a_site_being_dialled_is_not_due_for_the_other_agent(
        self, admin_client, anon_client, fleet
    ):
        first = await make_agent(admin_client, "АРМ перший")
        second = await make_agent(admin_client, "АРМ другий")
        card = await make_card(
            admin_client, enterprise_id=fleet["enterprise_id"],
            phone="+380501234567", auto_poll=True, poll_times=["00:01"],
        )
        for agent in (first, second):
            await anon_client.put(
                "/polling/agent/devices",
                json={"device_ids": [card["id"]]}, headers=key(agent),
            )

        # The first one takes it.
        assert (await anon_client.post(
            f"/polling/agent/devices/{card['id']}/start", headers=key(first),
        )).status_code == 200

        plan = (await anon_client.get(
            "/polling/agent/plan", headers=key(second))).json()
        assert plan["devices"][0]["due"] is False
        assert plan["devices"][0]["due_reason"] == "опитує інший агент"

        # And the holder still sees its own session as its work to do.
        mine = (await anon_client.get(
            "/polling/agent/plan", headers=key(first))).json()
        assert mine["devices"][0]["due"] is True

    async def test_the_site_comes_back_when_the_session_ends(
        self, admin_client, anon_client, fleet
    ):
        first = await make_agent(admin_client, "АРМ перший")
        second = await make_agent(admin_client, "АРМ другий")
        card = await make_card(
            admin_client, enterprise_id=fleet["enterprise_id"],
            phone="+380501234567", auto_poll=True, poll_times=["00:01"],
        )
        for agent in (first, second):
            await anon_client.put(
                "/polling/agent/devices",
                json={"device_ids": [card["id"]]}, headers=key(agent),
            )
        await anon_client.post(
            f"/polling/agent/devices/{card['id']}/start", headers=key(first))
        await anon_client.post(
            f"/polling/agent/devices/{card['id']}/finish",
            json={"status": "ok", "error_code": None, "error_text": None,
                  "rows": {"hour": 1, "day": 0}, "duration_ms": 1000,
                  "connect_ms": 500},
            headers=key(first),
        )

        plan = (await anon_client.get(
            "/polling/agent/plan", headers=key(second))).json()
        # Polled, so not due — but for the schedule's reason, not the claim's.
        assert plan["devices"][0]["due_reason"] != "опитує інший агент"


class TestTheAgentMustBeTheBuildTheServerHandsOut:
    """An agent of another version does not poll.

    The build of 14.09 read a ВЕГА's display unit and filed the archive's own
    number under it — 6.15 кгс/см² stored as 6.15 МПа, a tenfold error that
    reads as an ordinary pressure. The fix shipped an hour later and the
    workstation carried on running the old .exe, writing the same wrong column
    again. A poll by a build known to be wrong is worth less than no poll: a
    gap is visible, a plausible number is not.
    """

    async def test_an_older_agent_is_told_so_and_gets_nothing_to_do(
        self, anon_client, session_ready, tmp_path, monkeypatch
    ):
        monkeypatch.setitem(backend_settings, "AGENT_DIST_DIR", str(tmp_path))
        (tmp_path / "hlv-poller-0.3.0.zip").write_bytes(b"PK")
        agent, card = session_ready["agent"], session_ready["card"]

        await anon_client.post(
            "/polling/agent/state",
            json={"version": "0.2.9", "host": "АРМ", "due_count": 0},
            headers=key(agent),
        )

        plan = (await anon_client.get(
            "/polling/agent/plan", headers=key(agent)
        )).json()
        assert all(d["due"] is False for d in plan["devices"])
        assert "0.2.9" in plan["devices"][0]["due_reason"]
        assert "0.3.0" in plan["devices"][0]["due_reason"]

        # And the door is shut too, not only the sign above it.
        refused = await anon_client.post(
            f"/polling/agent/devices/{card['id']}/start", headers=key(agent)
        )
        assert refused.status_code == 409
        assert "0.3.0" in refused.json()["detail"]

    async def test_a_newer_agent_is_refused_just_the_same(
        self, anon_client, session_ready, tmp_path, monkeypatch
    ):
        """Newer means the server was not updated, which is the same mistake."""
        monkeypatch.setitem(backend_settings, "AGENT_DIST_DIR", str(tmp_path))
        (tmp_path / "hlv-poller-0.3.0.zip").write_bytes(b"PK")
        agent, card = session_ready["agent"], session_ready["card"]

        await anon_client.post(
            "/polling/agent/state",
            json={"version": "0.4.0", "host": "АРМ", "due_count": 0},
            headers=key(agent),
        )
        refused = await anon_client.post(
            f"/polling/agent/devices/{card['id']}/start", headers=key(agent)
        )
        assert refused.status_code == 409

    async def test_the_matching_build_polls_as_before(
        self, anon_client, session_ready, tmp_path, monkeypatch
    ):
        monkeypatch.setitem(backend_settings, "AGENT_DIST_DIR", str(tmp_path))
        (tmp_path / "hlv-poller-0.3.0.zip").write_bytes(b"PK")
        agent, card = session_ready["agent"], session_ready["card"]

        await anon_client.post(
            f"/polling/agent/devices/{card['id']}/finish",
            json={"status": "ok", "rows": {}},
            headers=key(agent),
        )
        await anon_client.post(
            "/polling/agent/state",
            json={"version": "0.3.0", "host": "АРМ", "due_count": 0},
            headers=key(agent),
        )
        taken = await anon_client.post(
            f"/polling/agent/devices/{card['id']}/start", headers=key(agent)
        )
        assert taken.status_code == 200

    async def test_a_server_with_no_build_blocks_nobody(
        self, anon_client, session_ready, tmp_path, monkeypatch
    ):
        """No build published is not a stale fleet — it is a server nobody has
        published an agent from, and it must not stop the polling it already
        has."""
        monkeypatch.setitem(backend_settings, "AGENT_DIST_DIR", str(tmp_path))
        agent, card = session_ready["agent"], session_ready["card"]

        await anon_client.post(
            f"/polling/agent/devices/{card['id']}/finish",
            json={"status": "ok", "rows": {}},
            headers=key(agent),
        )
        await anon_client.post(
            "/polling/agent/state",
            json={"version": "0.1.0", "host": "АРМ", "due_count": 0},
            headers=key(agent),
        )
        taken = await anon_client.post(
            f"/polling/agent/devices/{card['id']}/start", headers=key(agent)
        )
        assert taken.status_code == 200


@pytest.mark.asyncio
class TestHolesAreReadAgain:
    """A hole behind the newest record used to stay a hole for good.

    The plan said only where the archive ends, and an agent read forward from
    there. So an hour lost to a bad line — a ВЕГА record the line swallowed, a
    КПЛГ page skipped — was never asked for again. The plan now carries every
    period missing behind the newest record, as deep as any corrector keeps
    one; the agent clips that by where this corrector's archive starts (known
    only on the call) and reads what is left. An enterprise's archive is kept
    by corrector, so the installation date does not limit it — the enterprise
    takes the periods it needs by its own installation windows. An agent that reads a range to
    its end and still finds nothing says so, so that a corrector which was
    simply switched off is not asked for the same empty hours on every call.
    """

    @staticmethod
    def _hours():
        base = datetime.now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=10)
        return base, [base + timedelta(hours=i) for i in range(9) if i not in (4, 5)]

    async def _store(self, anon_client, agent, card, stamps, checked=(),
                     period="hourly"):
        resp = await anon_client.post(
            f"/polling/agent/devices/{card['id']}/data",
            json={
                "ser_num": 555001, "period_type": period,
                "rows": [{"stamp": s.isoformat(), "volume": 1.0} for s in stamps],
                "checked": [[a.isoformat(), b.isoformat()] for a, b in checked],
            },
            headers=key(agent),
        )
        assert resp.status_code == 200, resp.text

    async def _plan(self, anon_client, agent, card):
        plan = (await anon_client.get("/polling/agent/plan", headers=key(agent))).json()
        return next(d for d in plan["devices"] if d["id"] == card["id"])

    @staticmethod
    def _range(a, b):
        return [a.isoformat(), b.isoformat()]

    async def _installed(self, fleet, when):
        async with async_session_factory() as session:
            await session.execute(
                text("UPDATE enterprise_device SET installed_from = :w "
                     "WHERE device_id = :d"),
                {"w": when, "d": fleet["device_id"]},
            )
            await session.commit()

    async def test_a_hole_behind_the_newest_hour_is_in_the_plan(
        self, anon_client, session_ready
    ):
        agent, card = session_ready["agent"], session_ready["card"]
        base, stamps = self._hours()
        await self._store(anon_client, agent, card, stamps)

        device = await self._plan(anon_client, agent, card)
        assert device["due"] is True
        hole = self._range(base + timedelta(hours=4), base + timedelta(hours=5))
        assert hole in device["gap_hours"]
        # And the history before our first record, which the corrector may
        # still hold — the agent clips it by where the corrector's ring starts.
        assert device["gap_hours"][0][1] == (base - timedelta(hours=1)).isoformat()

    async def test_the_installation_date_does_not_limit_a_correctors_archive(
        self, anon_client, session_ready, fleet
    ):
        """An enterprise's readings are stored by corrector, and the enterprise
        takes the periods it needs by the installation windows. So what the
        corrector holds from before it came here is still its own archive —
        worth filling, and harmless to the enterprise."""
        agent, card = session_ready["agent"], session_ready["card"]
        base, stamps = self._hours()
        await self._installed(fleet, base)
        await self._store(anon_client, agent, card, stamps)

        device = await self._plan(anon_client, agent, card)
        assert device["gap_hours"][0][1] == (base - timedelta(hours=1)).isoformat()

    async def test_a_hole_read_to_its_end_and_still_empty_is_not_asked_again(
        self, anon_client, session_ready, fleet
    ):
        agent, card = session_ready["agent"], session_ready["card"]
        base, stamps = self._hours()
        await self._installed(fleet, base)
        await self._store(anon_client, agent, card, stamps)
        hole = (base + timedelta(hours=4), base + timedelta(hours=5))

        # Read through, nothing there: the corrector was off.
        await self._store(anon_client, agent, card, [], checked=[hole])

        device = await self._plan(anon_client, agent, card)
        assert self._range(*hole) not in device["gap_hours"]
        assert all(start > (base + timedelta(hours=5)).isoformat()
                   or end < (base + timedelta(hours=4)).isoformat()
                   for start, end in device["gap_hours"])

    async def test_a_hole_that_was_read_is_gone(
        self, anon_client, session_ready, fleet
    ):
        agent, card = session_ready["agent"], session_ready["card"]
        base, stamps = self._hours()
        await self._installed(fleet, base)
        await self._store(anon_client, agent, card, stamps)
        await self._store(anon_client, agent, card,
                          [base + timedelta(hours=4), base + timedelta(hours=5)])

        device = await self._plan(anon_client, agent, card)
        # Only the history before our first record is left to read.
        assert device["gap_hours"][-1][1] == (base - timedelta(hours=1)).isoformat()

    async def test_what_is_newer_than_the_archive_is_not_a_hole(
        self, anon_client, session_ready
    ):
        # The hours after the newest record are read by the ordinary window;
        # listing them as holes would read them twice.
        agent, card = session_ready["agent"], session_ready["card"]
        base, stamps = self._hours()
        await self._store(anon_client, agent, card, stamps[:3])
        device = await self._plan(anon_client, agent, card)
        newest = stamps[2].isoformat()
        assert all(end <= newest for _start, end in device["gap_hours"])

    async def test_a_missing_day_is_in_the_plan(
        self, anon_client, session_ready, fleet
    ):
        agent, card = session_ready["agent"], session_ready["card"]
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        await self._installed(fleet, today - timedelta(days=7))
        days = [today - timedelta(days=d) for d in (6, 5, 3, 2, 1)]
        await self._store(anon_client, agent, card, days, period="daily")

        device = await self._plan(anon_client, agent, card)
        missing = today - timedelta(days=4)
        assert self._range(missing, missing) in device["gap_days"]

    async def test_a_card_that_is_not_due_carries_no_holes(
        self, anon_client, admin_client, session_ready
    ):
        # Finding holes reads the archive; the plan is fetched every few
        # seconds by every agent, so it is only done for a call about to be made.
        agent, card = session_ready["agent"], session_ready["card"]
        base, stamps = self._hours()
        await self._store(anon_client, agent, card, stamps)
        await admin_client.put(f"/polling/devices/{card['id']}",
                               json={"enabled": False})
        device = await self._plan(anon_client, agent, card)
        assert device["due"] is False
        assert device["gap_hours"] == []
