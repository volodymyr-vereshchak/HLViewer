"""The polling settings API: device cards, agents, default hours.

Step 1 of docs/plans/gsm-polling.md. Nothing polls anything yet, so what is
worth pinning here is who may do what and what the API refuses to tell:

  * settings are administrative, but asking for an out-of-turn poll is not —
    the person who notices a meter went quiet is rarely an admin;
  * an agent key exists in clear exactly once, in the response that creates it.
"""

import os
from datetime import datetime, timedelta

import pytest
import pytest_asyncio

from backend.db.engine import async_session_factory
from backend.db.models.device_catalog_model import CorectorType, Manufacturer
from backend.db.models.dpd_line_model import DpdLine
from sqlalchemy import select

from backend.db.models.enterprise_model import (
    DpdDevice, Enterprise, EnterpriseDevice,
)
from backend.db.models.grmu_branch_model import GrmuBranch
from backend.db.models.polling_model import PollAgent, PollDevice
from backend.settings import backend_settings


@pytest_asyncio.fixture
async def targets(seed_users) -> dict:
    """A metering point with a corrector fitted, and a DPD line."""
    async with async_session_factory() as session:
        branch = GrmuBranch(name="Тестова філія")
        session.add(branch)
        await session.flush()
        point = Enterprise(
            enterprise_name="Завод А", branch_id=branch.id,
            active=True, enabled=True,
        )
        line = DpdLine(name="Лінія 1", branch_id=branch.id)
        mfr = Manufacturer(short_name="Радміртех", full_name="Радміртех", mf_dev=1)
        session.add(point)
        session.add(line)
        session.add(mfr)
        await session.flush()
        ct = CorectorType(manufacturer_id=mfr.id, model_name="ВЕГА-1.01", type_dev=5)
        other = CorectorType(manufacturer_id=mfr.id, model_name="КПЛГ-1.01Р", type_dev=2)
        session.add(ct)
        session.add(other)
        await session.flush()
        device = DpdDevice(ser_num=555001, ch_num=0, corector_type_id=ct.id)
        session.add(device)
        await session.flush()
        session.add(EnterpriseDevice(
            enterprise_id=point.id, device_id=device.id,
            installed_from=datetime(2026, 1, 1, 7),
        ))
        await session.commit()
        return {
            "branch_id": branch.id,
            "enterprise_id": point.id,
            "dpd_line_id": line.id,
            "dpd_device_id": device.id,
            "corector_type_id": ct.id,
            "other_type_id": other.id,
        }


async def _set_protocol(corector_type_id: int, protocol_id: int) -> None:
    async with async_session_factory() as session:
        ct = await session.get(CorectorType, corector_type_id)
        ct.protocol_id = protocol_id
        await session.commit()


async def _replace(
    enterprise_id: int, old_device_id: int, ser_num: int,
    corector_type_id: int | None = None,
) -> int:
    """Swap the corrector at a point: the old one is taken off, a new one is
    fitted. Returns the new device id."""
    async with async_session_factory() as session:
        device = DpdDevice(
            ser_num=ser_num, ch_num=0, corector_type_id=corector_type_id,
        )
        session.add(device)
        await session.flush()
        old = (await session.execute(
            select(EnterpriseDevice)
            .where(EnterpriseDevice.device_id == old_device_id)
        )).scalars().first()
        old.removed_at = datetime(2026, 6, 1, 7)
        session.add(EnterpriseDevice(
            enterprise_id=enterprise_id,
            device_id=device.id,
            installed_from=datetime(2026, 6, 1, 7),
        ))
        await session.commit()
        return device.id


async def make_card(client, **body) -> dict:
    resp = await client.post("/polling/devices", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.asyncio
class TestDeviceCards:
    async def test_create_and_list(self, admin_client, targets):
        card = await make_card(
            admin_client,
            dpd_device_id=targets["dpd_device_id"],
            phone="+380501234567",
        )
        assert card["target_kind"] == "dpd_device"
        # The label says where the corrector stands; the serial is what the
        # modem will check the reply against; the model says what will answer.
        assert card["target_label"] == "Завод А"
        assert card["ser_num"] == 555001
        assert (card["model_name"], card["manufacturer"]) == (
            "ВЕГА-1.01", "Радміртех",
        )
        # Nobody has taken it yet, and that means it is never polled.
        assert card["agent_ids"] == []

        listed = (await admin_client.get("/polling/devices")).json()
        assert [c["id"] for c in listed] == [card["id"]]

    async def test_a_dpd_line_is_named_by_its_line(self, admin_client, targets):
        card = await make_card(admin_client, dpd_line_id=targets["dpd_line_id"])
        assert (card["target_kind"], card["target_label"]) == (
            "dpd_line", "Лінія 1",
        )

    async def test_a_card_must_name_exactly_one_target(self, admin_client, targets):
        for body in (
            {},
            {"dpd_device_id": targets["dpd_device_id"],
             "dpd_line_id": targets["dpd_line_id"]},
        ):
            resp = await admin_client.post("/polling/devices", json=body)
            # 422 from the schema, not 500 from the CHECK constraint: the
            # operator gets a sentence rather than a database error.
            assert resp.status_code == 422, resp.text

    async def test_a_second_card_for_the_same_site_is_a_conflict(
        self, admin_client, targets
    ):
        await make_card(admin_client, dpd_device_id=targets["dpd_device_id"])
        resp = await admin_client.post(
            "/polling/devices", json={"dpd_device_id": targets["dpd_device_id"]}
        )
        assert resp.status_code == 409

    async def test_update_changes_only_what_was_sent(self, admin_client, targets):
        card = await make_card(
            admin_client,
            dpd_device_id=targets["dpd_device_id"],
            phone="+380501234567",
            repeat_count=2,
        )
        resp = await admin_client.put(
            f"/polling/devices/{card['id']}", json={"repeat_count": 5}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["repeat_count"] == 5
        assert resp.json()["phone"] == "+380501234567"

    async def test_an_unknown_field_is_refused(self, admin_client, targets):
        # The connection speed moved to the agent; a client still sending it
        # is out of date, and saying so beats storing it where nothing reads.
        card = await make_card(admin_client, dpd_device_id=targets["dpd_device_id"])
        resp = await admin_client.put(
            f"/polling/devices/{card['id']}", json={"baud": 9600}
        )
        assert resp.status_code == 422

    async def test_the_schedule_is_validated(self, admin_client, targets):
        card = await make_card(admin_client, dpd_device_id=targets["dpd_device_id"])
        ok = await admin_client.put(
            f"/polling/devices/{card['id']}", json={"poll_cron": " 30 18,6 * * * "}
        )
        assert ok.status_code == 200
        # Tidied on the way in; what it says is what comes back.
        assert ok.json()["poll_cron"] == "30 18,6 * * *"
        # "25:00" would be a slot the agent silently never reaches.
        bad = await admin_client.put(
            f"/polling/devices/{card['id']}", json={"poll_cron": "0 25 * * *"}
        )
        assert bad.status_code == 400

    async def test_the_phone_is_normalised(self, admin_client, targets):
        # However it was written down, the agent gets one shape to dial.
        card = await make_card(
            admin_client,
            dpd_device_id=targets["dpd_device_id"],
            phone=" 050 123-45-67 ",
        )
        assert card["phone"] == "+380501234567"

    async def test_a_number_that_cannot_be_dialled_is_refused(
        self, admin_client, targets
    ):
        # Otherwise it surfaces as "no dialtone" on somebody else's machine.
        resp = await admin_client.post(
            "/polling/devices",
            json={"dpd_device_id": targets["dpd_device_id"], "phone": "050123456"},
        )
        assert resp.status_code == 400

    async def test_priority_is_a_range(self, admin_client, targets):
        card = await make_card(
            admin_client, dpd_device_id=targets["dpd_device_id"], priority=5
        )
        assert card["priority"] == 5
        bad = await admin_client.put(
            f"/polling/devices/{card['id']}", json={"priority": 9}
        )
        assert bad.status_code == 400

    async def test_delete(self, admin_client, targets):
        card = await make_card(admin_client, dpd_device_id=targets["dpd_device_id"])
        assert (await admin_client.delete(
            f"/polling/devices/{card['id']}"
        )).status_code == 204
        assert (await admin_client.get("/polling/devices")).json() == []


@pytest.mark.asyncio
class TestAgents:
    async def test_the_key_is_shown_once_and_never_again(
        self, admin_client, targets
    ):
        created = await admin_client.post(
            "/polling/agents",
            json={"name": "АРМ Іваненко", "branch_id": targets["branch_id"]},
        )
        assert created.status_code == 201, created.text
        assert len(created.json()["key"]) > 20

        listed = (await admin_client.get("/polling/agents")).json()
        assert [a["name"] for a in listed] == ["АРМ Іваненко"]
        assert "key" not in listed[0]

        # Only the hash is stored, so the key cannot be recovered from the DB.
        async with async_session_factory() as session:
            agent = await session.get(PollAgent, listed[0]["id"])
            assert created.json()["key"] not in agent.key_hash

    async def test_rotating_a_key_replaces_it(self, admin_client, targets):
        created = (await admin_client.post(
            "/polling/agents", json={"name": "АРМ"}
        )).json()
        rotated = await admin_client.post(f"/polling/agents/{created['id']}/key")
        assert rotated.status_code == 200
        assert rotated.json()["key"] != created["key"]

    async def test_names_are_unique(self, admin_client, targets):
        await admin_client.post("/polling/agents", json={"name": "АРМ"})
        resp = await admin_client.post("/polling/agents", json={"name": "АРМ"})
        assert resp.status_code == 409

    async def test_an_agent_picks_its_own_devices(self, admin_client, targets):
        card = await make_card(admin_client, dpd_device_id=targets["dpd_device_id"])
        agent = (await admin_client.post(
            "/polling/agents", json={"name": "АРМ"}
        )).json()

        resp = await admin_client.put(
            f"/polling/agents/{agent['id']}/devices",
            json={"device_ids": [card["id"]]},
        )
        assert resp.status_code == 200
        assert resp.json() == [card["id"]]

        listed = (await admin_client.get("/polling/devices")).json()
        assert listed[0]["agent_ids"] == [agent["id"]]

    async def test_the_set_is_replaced_whole(self, admin_client, targets):
        # Operators divide the fleet by ticking boxes, so a save is the whole
        # set: unticking has to actually untick.
        first = await make_card(admin_client, dpd_device_id=targets["dpd_device_id"])
        second = await make_card(
            admin_client, dpd_line_id=targets["dpd_line_id"]
        )
        agent = (await admin_client.post(
            "/polling/agents", json={"name": "АРМ"}
        )).json()

        await admin_client.put(
            f"/polling/agents/{agent['id']}/devices",
            json={"device_ids": [first["id"], second["id"]]},
        )
        resp = await admin_client.put(
            f"/polling/agents/{agent['id']}/devices",
            json={"device_ids": [second["id"]]},
        )
        assert resp.json() == [second["id"]]

    async def test_deleting_an_agent_leaves_its_devices(self, admin_client, targets):
        card = await make_card(admin_client, dpd_device_id=targets["dpd_device_id"])
        agent = (await admin_client.post(
            "/polling/agents", json={"name": "АРМ"}
        )).json()
        await admin_client.put(
            f"/polling/agents/{agent['id']}/devices",
            json={"device_ids": [card["id"]]},
        )

        assert (await admin_client.delete(
            f"/polling/agents/{agent['id']}"
        )).status_code == 204

        listed = (await admin_client.get("/polling/devices")).json()
        # The device survives and is now nobody's — which is exactly the state
        # the UI has to shout about.
        assert [c["agent_ids"] for c in listed] == [[]]


@pytest.mark.asyncio
class TestTheDriverComesFromTheModel:
    """Which driver can dial a corrector is a property of its model, so it is
    set once in Типи коректорів and never typed on a card. Retyping it per
    device invites a typo, and a wrong driver looks exactly like a dead
    meter."""

    async def test_the_card_takes_the_driver_of_its_model(
        self, admin_client, targets
    ):
        await _set_protocol(targets["corector_type_id"], 54)
        card = await make_card(admin_client, dpd_device_id=targets["dpd_device_id"])
        assert card["protocol_id"] == 54

    async def test_a_model_without_a_driver_says_nothing(
        self, admin_client, targets
    ):
        # ТКБ, smart104 and ТАНДЕМ appear in none of the Ask2 driver
        # assemblies: null here is the truth, not a missing setting.
        card = await make_card(admin_client, dpd_device_id=targets["dpd_device_id"])
        assert card["protocol_id"] is None

    async def test_the_model_follows_the_corrector(self, admin_client, targets):
        # A serial alone does not say what answers the call, and the model is
        # what decides the driver and the alarm dictionary.
        card = await make_card(admin_client, dpd_device_id=targets["dpd_device_id"])
        new_id = await _replace(
            targets["enterprise_id"], targets["dpd_device_id"], 555002,
            corector_type_id=targets["other_type_id"],
        )
        resp = await admin_client.put(
            f"/polling/devices/{card['id']}", json={"dpd_device_id": new_id}
        )
        assert resp.json()["model_name"] == "КПЛГ-1.01Р"

    async def test_repointing_re_reads_the_driver(self, admin_client, targets):
        # A replacement is often a different model, and a card left on the old
        # driver would dial the new device in a language it does not speak.
        await _set_protocol(targets["corector_type_id"], 54)
        card = await make_card(admin_client, dpd_device_id=targets["dpd_device_id"])
        new_id = await _replace(
            targets["enterprise_id"], targets["dpd_device_id"], 555002,
            corector_type_id=targets["other_type_id"],
        )
        await _set_protocol(targets["other_type_id"], 52)

        resp = await admin_client.put(
            f"/polling/devices/{card['id']}", json={"dpd_device_id": new_id}
        )
        assert resp.json()["protocol_id"] == 52


@pytest.mark.asyncio
class TestTheNetworkAddress:
    """Only Floutek gives an operator a real choice: several correctors share
    one line there. Everywhere else the address is sent and checked but is
    always the default, so it is filled in rather than asked for."""

    async def test_a_non_floutek_card_gets_the_default(self, admin_client, targets):
        await _set_protocol(targets["corector_type_id"], 1054)  # ВЕГА
        card = await make_card(
            admin_client, dpd_device_id=targets["dpd_device_id"], device_address=7
        )
        # Typed 7, stored 1: the field is not a choice for this driver, and a
        # stray value here reads as a dead meter later.
        assert card["device_address"] == 1
        assert card["address_matters"] is False

    async def test_a_floutek_card_keeps_what_was_typed(self, admin_client, targets):
        await _set_protocol(targets["corector_type_id"], 1070)  # Флоутек ВР-2
        card = await make_card(
            admin_client, dpd_device_id=targets["dpd_device_id"], device_address=3
        )
        assert card["device_address"] == 3
        assert card["address_matters"] is True

    async def test_repointing_to_another_family_resets_it(
        self, admin_client, targets
    ):
        await _set_protocol(targets["corector_type_id"], 1070)
        card = await make_card(
            admin_client, dpd_device_id=targets["dpd_device_id"], device_address=3
        )
        new_id = await _replace(
            targets["enterprise_id"], targets["dpd_device_id"], 555002,
            corector_type_id=targets["other_type_id"],
        )
        await _set_protocol(targets["other_type_id"], 1054)

        resp = await admin_client.put(
            f"/polling/devices/{card['id']}", json={"dpd_device_id": new_id}
        )
        # The new model is not a Floutek, so the old address is not a choice.
        assert resp.json()["device_address"] == 1


@pytest.mark.asyncio
class TestReplacingTheCorrector:
    """A replacement is recorded by repointing the card: same phone, new
    serial, and the modem reads the new device from then on.

    The point's own history stays continuous in Підприємства — the poll has no
    opinion about it. What the card must never do is quietly keep dialling a
    corrector that has been taken off, which is why the list says whether the
    one it names is still fitted.
    """

    async def test_the_card_says_which_corrector_it_expects(
        self, admin_client, targets
    ):
        card = await make_card(admin_client, dpd_device_id=targets["dpd_device_id"])
        assert card["ser_num"] == 555001
        assert card["still_installed"] is True

    async def test_a_removed_corrector_is_flagged(self, admin_client, targets):
        card = await make_card(admin_client, dpd_device_id=targets["dpd_device_id"])
        await _replace(targets["enterprise_id"], targets["dpd_device_id"], 555002)

        listed = (await admin_client.get("/polling/devices")).json()
        row = next(c for c in listed if c["id"] == card["id"])
        # Still dialling the old device: the replacement was entered at the
        # point but nobody moved the phone.
        assert row["still_installed"] is False
        assert row["ser_num"] == 555001

    async def test_moving_the_phone_to_the_new_corrector(
        self, admin_client, targets
    ):
        card = await make_card(
            admin_client, dpd_device_id=targets["dpd_device_id"], phone="+380501234567"
        )
        new_id = await _replace(
            targets["enterprise_id"], targets["dpd_device_id"], 555002
        )

        resp = await admin_client.put(
            f"/polling/devices/{card['id']}", json={"dpd_device_id": new_id}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["ser_num"] == 555002
        assert resp.json()["still_installed"] is True
        # The phone belongs to the site and stays where it was.
        assert resp.json()["phone"] == "+380501234567"

    async def test_two_cards_cannot_name_one_corrector(self, admin_client, targets):
        first = await make_card(
            admin_client, dpd_device_id=targets["dpd_device_id"]
        )
        new_id = await _replace(
            targets["enterprise_id"], targets["dpd_device_id"], 555002
        )
        await make_card(admin_client, dpd_device_id=new_id)

        resp = await admin_client.put(
            f"/polling/devices/{first['id']}", json={"dpd_device_id": new_id}
        )
        assert resp.status_code == 409

    async def test_a_line_card_is_not_repointed_by_hand(self, admin_client, targets):
        # A DPD line keeps its corrector in its own history, so there is
        # nothing on this screen to move.
        card = await make_card(admin_client, dpd_line_id=targets["dpd_line_id"])
        resp = await admin_client.put(
            f"/polling/devices/{card['id']}",
            json={"dpd_device_id": targets["dpd_device_id"]},
        )
        assert resp.status_code == 400


@pytest.mark.asyncio
class TestSchedule:
    async def test_defaults_exist_before_anyone_sets_them(self, admin_client):
        """One hour, and it is inside the working day.

        An archive read before anybody arrives is an archive whose failure
        nobody sees for two hours.
        """
        resp = await admin_client.get("/polling/schedule")
        assert resp.status_code == 200
        assert resp.json()["poll_cron"] == "0 8 * * *"

    async def test_setting_the_schedule(self, admin_client):
        resp = await admin_client.put(
            "/polling/schedule", json={"poll_cron": "0 7,19 * * *"}
        )
        assert resp.json()["poll_cron"] == "0 7,19 * * *"
        assert (await admin_client.get("/polling/schedule")).json()["poll_cron"] == (
            "0 7,19 * * *"
        )

    async def test_hourly_is_five_characters_rather_than_a_list_of_24(self, admin_client):
        resp = await admin_client.put("/polling/schedule", json={"poll_cron": "0 * * * *"})
        assert resp.json()["poll_cron"] == "0 * * * *"

    async def test_a_schedule_that_could_never_fire_is_refused(self, admin_client):
        resp = await admin_client.put(
            "/polling/schedule", json={"poll_cron": "о 7 ранку"}
        )
        assert resp.status_code == 400


@pytest.mark.asyncio
class TestWhoMayDoWhat:
    async def test_a_viewer_may_ask_for_a_poll(
        self, admin_client, viewer_client, targets
    ):
        """The decision that motivated the exception in the auth middleware:
        every other write here is admin-only, this one is not."""
        card = await make_card(admin_client, dpd_device_id=targets["dpd_device_id"])

        resp = await viewer_client.post(f"/polling/devices/{card['id']}/poll")
        assert resp.status_code == 202, resp.text
        assert resp.json()["requested_at"] is not None

    async def test_a_viewer_may_cancel_the_request_they_made(
        self, admin_client, viewer_client, targets
    ):
        card = await make_card(admin_client, dpd_device_id=targets["dpd_device_id"])
        await viewer_client.post(f"/polling/devices/{card['id']}/poll")
        resp = await viewer_client.post(
            f"/polling/devices/{card['id']}/poll", params={"cancel": True}
        )
        assert resp.status_code == 202
        assert resp.json()["requested_at"] is None

    async def test_a_viewer_may_look_at_the_list(
        self, admin_client, viewer_client, targets
    ):
        await make_card(admin_client, dpd_device_id=targets["dpd_device_id"])
        assert (await viewer_client.get("/polling/devices")).status_code == 200

    async def test_a_viewer_may_not_change_settings(
        self, admin_client, viewer_client, targets
    ):
        card = await make_card(admin_client, dpd_device_id=targets["dpd_device_id"])
        assert (await viewer_client.put(
            f"/polling/devices/{card['id']}", json={"baud": 2400}
        )).status_code == 403
        assert (await viewer_client.post(
            "/polling/devices", json={"dpd_line_id": targets["dpd_line_id"]}
        )).status_code == 403
        assert (await viewer_client.delete(
            f"/polling/devices/{card['id']}"
        )).status_code == 403

    async def test_a_viewer_may_not_see_the_agent_registry(self, viewer_client):
        # It carries hostnames and key hashes, and the full card behind it
        # carries phone numbers and access codes.
        assert (await viewer_client.get("/polling/agents")).status_code == 403

    async def test_a_viewer_may_not_open_a_full_card(
        self, admin_client, viewer_client, targets
    ):
        card = await make_card(
            admin_client,
            dpd_device_id=targets["dpd_device_id"],
            phone="+380501234567",
        )
        assert (await viewer_client.get(
            f"/polling/devices/{card['id']}"
        )).status_code == 403

    async def test_signing_in_is_still_required(self, anon_client):
        assert (await anon_client.get("/polling/devices")).status_code == 401
        assert (await anon_client.post("/polling/devices/1/poll")).status_code == 401


class TestPollingAnEnterpriseNow:
    """The button in «Опитування промисловості», and everything it refuses.

    Each refusal here is one the operator would otherwise meet as a call that
    goes nowhere, several minutes later and with nothing to show for it.
    """

    async def _live_agent(self, admin_client, anon_client, card_id: int) -> dict:
        agent = (await admin_client.post(
            "/polling/agents", json={"name": "АРМ біля модема"}
        )).json()
        headers = {"X-Agent-Key": agent["key"]}
        await anon_client.put("/polling/agent/devices",
                              json={"device_ids": [card_id]}, headers=headers)
        # Fetching a plan is what marks an agent as being on the line.
        await anon_client.get("/polling/agent/plan", headers=headers)
        return {"agent": agent, "headers": headers}

    async def test_a_site_with_no_modem_says_so(self, admin_client, targets):
        resp = await admin_client.post(
            f"/polling/enterprises/{targets['enterprise_id']}/poll"
        )
        assert resp.status_code == 422
        assert "не налаштовано модем" in resp.json()["detail"]

    async def test_a_site_with_nothing_fitted_says_so(
        self, admin_client, anon_client, targets
    ):
        await _set_protocol(targets["corector_type_id"], 1054)
        card = await make_card(
            admin_client, enterprise_id=targets["enterprise_id"],
            phone="+380501234567",
        )
        await self._live_agent(admin_client, anon_client, card["id"])

        async with async_session_factory() as session:
            fitted = (await session.execute(
                select(EnterpriseDevice).where(
                    EnterpriseDevice.enterprise_id == targets["enterprise_id"]
                )
            )).scalars().one()
            fitted.removed_at = datetime(2026, 6, 1)
            await session.commit()

        resp = await admin_client.post(
            f"/polling/enterprises/{targets['enterprise_id']}/poll"
        )
        assert resp.status_code == 422
        assert resp.json()["detail"] == "Немає встановлених корректорів"

    async def test_a_site_nobody_was_given_says_so(self, admin_client, targets):
        """Not "немає вільного модема" — there is no modem to be free.

        The two refusals used to share that sentence, and it is advice for
        neither: a card nobody was given needs an agent chosen, and an agent
        that is switched off needs somebody to start it. An operator reading
        "жоден агент не на зв'язку" for a site that was never assigned goes
        looking at the workstation, which is fine, and finds it running.
        """
        await _set_protocol(targets["corector_type_id"], 1054)
        await make_card(
            admin_client, enterprise_id=targets["enterprise_id"],
            phone="+380501234567",
        )
        resp = await admin_client.post(
            f"/polling/enterprises/{targets['enterprise_id']}/poll"
        )
        assert resp.status_code == 422
        assert "не призначено жодного агента" in resp.json()["detail"]

    async def test_an_assigned_but_sleeping_agent_is_named(
        self, admin_client, targets
    ):
        """An assignment held by a switched-off machine is not a free modem.

        The request would sit unread until morning, and the screen would show
        a poll that never finished rather than one that never started. The
        machine is named, because that is the one thing the operator needs in
        order to go and switch it on.
        """
        await _set_protocol(targets["corector_type_id"], 1054)
        card = await make_card(
            admin_client, enterprise_id=targets["enterprise_id"],
            phone="+380501234567",
        )
        agent = (await admin_client.post(
            "/polling/agents", json={"name": "АРМ вимкнений"})).json()
        await admin_client.put(
            f"/polling/devices/{card['id']}/agents",
            json={"agent_ids": [agent["id"]]},
        )

        resp = await admin_client.post(
            f"/polling/enterprises/{targets['enterprise_id']}/poll"
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert "АРМ вимкнений" in detail and "не на зв'язку" in detail

    async def test_a_model_without_a_catalogue_driver_is_still_dialled(
        self, admin_client, anon_client, targets
    ):
        """The corrector says what it is; the catalogue no longer decides.

        A new model used to stay undiallable until somebody typed a driver
        number against it in the catalogue. Every family the agent reads names
        itself on the first question, so the call is the test — and a model
        nobody reads ends with the corrector's own name in the journal.
        """
        card = await make_card(
            admin_client, enterprise_id=targets["enterprise_id"],
            phone="+380501234567",
        )
        await self._live_agent(admin_client, anon_client, card["id"])
        resp = await admin_client.post(
            f"/polling/enterprises/{targets['enterprise_id']}/poll"
        )
        assert resp.status_code == 202, resp.text

    async def test_what_answered_is_remembered_for_the_next_call(
        self, admin_client, anon_client, targets
    ):
        """The next call asks the right question first.

        Trying each family in turn costs seconds of silence per wrong guess,
        on every call. What answered last time is what the plan hands down,
        ahead of whatever the catalogue guessed — and a call that reached
        nobody does not wipe it.
        """
        from backend.db.models.polling_model import PollDevice

        await _set_protocol(targets["corector_type_id"], 1054)    # catalogue: ВЕГА
        card = await make_card(
            admin_client, enterprise_id=targets["enterprise_id"],
            phone="+380501234567",
        )
        live = await self._live_agent(admin_client, anon_client, card["id"])
        headers = live["headers"]

        await anon_client.post(f"/polling/agent/devices/{card['id']}/start", headers=headers)
        await anon_client.post(
            f"/polling/agent/devices/{card['id']}/finish",
            json={"status": "ok", "rows": {}, "protocol_id": 2002,
                  "model": "Floutek-TM-2-3-4"},
            headers=headers,
        )
        plan = (await anon_client.get("/polling/agent/plan", headers=headers)).json()
        mine = next(d for d in plan["devices"] if d["id"] == card["id"])
        assert mine["protocol_id"] == 2002
        assert mine["device_password"] == "11"

        # A call that reached nobody says nothing about who is there.
        await anon_client.post(f"/polling/agent/devices/{card['id']}/start", headers=headers)
        await anon_client.post(
            f"/polling/agent/devices/{card['id']}/finish",
            json={"status": "error", "error_code": "no_carrier"},
            headers=headers,
        )
        async with async_session_factory() as session:
            stored = await session.get(PollDevice, card["id"])
            assert (stored.detected_protocol, stored.detected_model) == (
                2002, "Floutek-TM-2-3-4",
            )

    async def test_the_request_is_accepted_and_the_log_starts_clean(
        self, admin_client, anon_client, targets
    ):
        await _set_protocol(targets["corector_type_id"], 1054)
        card = await make_card(
            admin_client, enterprise_id=targets["enterprise_id"],
            phone="+380501234567",
        )
        live = await self._live_agent(admin_client, anon_client, card["id"])

        # Something from the previous session, which must not linger on the
        # screen once a new one is asked for.
        await anon_client.post(f"/polling/agent/devices/{card['id']}/start",
                               headers=live["headers"])
        await anon_client.post(
            f"/polling/agent/devices/{card['id']}/log",
            json={"lines": [{"seq": 1, "message": "минулого разу: зайнято"}]},
            headers=live["headers"],
        )
        await anon_client.post(
            f"/polling/agent/devices/{card['id']}/finish",
            json={"status": "error", "error_code": "busy", "rows": {}},
            headers=live["headers"],
        )

        resp = await admin_client.post(
            f"/polling/enterprises/{targets['enterprise_id']}/poll"
        )
        assert resp.status_code == 202
        assert resp.json()["ser_num"] == 555001
        assert resp.json()["agent_name"] == "АРМ біля модема"

        watch = (await admin_client.get(
            f"/polling/enterprises/{targets['enterprise_id']}/poll"
        )).json()
        assert watch["status"] == "waiting"
        assert watch["lines"] == []

    async def test_the_screen_follows_the_session(
        self, admin_client, anon_client, targets, tmp_path, monkeypatch
    ):
        """Lines, progress and outcome, asked for one refresh at a time.

        The lines come from the site's journal file — the same one the monitor
        shows afterwards — so the test gets a folder of its own.
        """
        monkeypatch.setitem(backend_settings, "POLL_LOG_DIR", str(tmp_path))
        await _set_protocol(targets["corector_type_id"], 1054)
        card = await make_card(
            admin_client, enterprise_id=targets["enterprise_id"],
            phone="+380501234567",
        )
        live = await self._live_agent(admin_client, anon_client, card["id"])
        await admin_client.post(
            f"/polling/enterprises/{targets['enterprise_id']}/poll"
        )

        await anon_client.post(f"/polling/agent/devices/{card['id']}/start",
                               headers=live["headers"])
        await anon_client.post(
            f"/polling/agent/devices/{card['id']}/log",
            json={
                "lines": [
                    {"seq": 1, "message": "Набираю: ATDP+380501234567"},
                    {"seq": 2, "message": "Модем: CONNECT 9600/RLP"},
                ],
                "progress": {"done": 7, "total": 49},
            },
            headers=live["headers"],
        )

        watch = (await admin_client.get(
            f"/polling/enterprises/{targets['enterprise_id']}/poll"
        )).json()
        assert watch["status"] == "polling"
        assert watch["done"] == 7 and watch["total"] == 49
        assert [l["message"] for l in watch["lines"]] == [
            "Набираю: ATDP+380501234567", "Модем: CONNECT 9600/RLP",
        ]

        # The browser already has those two, and says so.
        later = (await admin_client.get(
            f"/polling/enterprises/{targets['enterprise_id']}/poll?after_seq=2"
        )).json()
        assert later["lines"] == []


class TestTheScreenAndTheJournalAreOneFile:
    """One account of one call, read by both screens.

    The live screen used to read a database copy that was deleted at the start
    of every session. The file it now reads is the same one the monitor shows
    afterwards — which is why the one case worth pinning is a request nobody
    has picked up yet: the file still holds the PREVIOUS call, and showing it
    would read as this one already running.
    """

    async def test_a_request_nobody_took_does_not_show_the_last_call(
        self, admin_client, anon_client, targets, tmp_path, monkeypatch
    ):
        monkeypatch.setitem(backend_settings, "POLL_LOG_DIR", str(tmp_path))
        await _set_protocol(targets["corector_type_id"], 1054)
        card = await make_card(
            admin_client, enterprise_id=targets["enterprise_id"],
            phone="+380501234567",
        )
        agent = (await admin_client.post(
            "/polling/agents", json={"name": "ПК біля модема"})).json()
        headers = {"X-Agent-Key": agent["key"]}
        await anon_client.put("/polling/agent/devices",
                              json={"device_ids": [card["id"]]}, headers=headers)
        await anon_client.get("/polling/agent/plan", headers=headers)

        # A call that happened and ended.
        await anon_client.post(
            f"/polling/agent/devices/{card['id']}/start", headers=headers)
        await anon_client.post(
            f"/polling/agent/devices/{card['id']}/log",
            json={"reset": True, "lines": [
                {"seq": 1, "level": "error", "message": "Немає лінії"},
            ]},
            headers=headers,
        )
        await anon_client.post(
            f"/polling/agent/devices/{card['id']}/finish",
            json={"status": "error", "error_code": "no_dialtone",
                  "error_text": "Немає лінії", "rows": {"hour": 0, "day": 0},
                  "duration_ms": 1000, "connect_ms": 0},
            headers=headers,
        )

        # Asked for again; the agent has not picked it up yet.
        await admin_client.post(
            f"/polling/enterprises/{targets['enterprise_id']}/poll")

        watch = (await admin_client.get(
            f"/polling/enterprises/{targets['enterprise_id']}/poll")).json()
        assert watch["status"] == "waiting"
        assert watch["lines"] == []

        # And the previous call is still there for the monitor: a request that
        # nobody answers must not destroy the only account of the last one.
        journal = (await admin_client.get(
            f"/polling/devices/{card['id']}/log/last")).json()
        assert "Немає лінії" in journal["text"]


class TestWhatAPollReportsStoring:
    """«Записано» must mean new, not fetched.

    A DPD poll asks for the same month every time, so counting what came back
    tells an operator nothing: the number is the same whether the archive
    gained seven hundred hours or none.
    """

    async def test_the_upsert_separates_new_rows_from_rewritten_ones(
        self, admin_client, targets
    ):
        from datetime import datetime as dt

        from backend.db.dao.dpd_archive_dao import DpdArchiveDao

        rows = [
            {"device_id": targets["dpd_device_id"], "stamp": dt(2026, 9, 1, h),
             "dvst_alwrk": 10.0 + h, "dvwrk_alwrk": 4.0 + h,
             "press": 2.4, "temper": 20.0, "press_unit": "кгс/см3"}
            for h in range(3)
        ]
        async with async_session_factory() as session:
            dao = DpdArchiveDao(session)
            first = await dao.upsert_records("hourly", rows)
            await session.commit()

            # The same hours again, plus one that is genuinely new.
            rows.append({**rows[0], "stamp": dt(2026, 9, 1, 3)})
            second = await dao.upsert_records("hourly", rows)
            await session.commit()

        assert first == {"inserted": 3, "updated": 0}
        assert second == {"inserted": 1, "updated": 3}

    async def test_storing_nothing_is_not_an_error(self, targets):
        from backend.db.dao.dpd_archive_dao import DpdArchiveDao

        async with async_session_factory() as session:
            assert await DpdArchiveDao(session).upsert_records("hourly", []) == {
                "inserted": 0, "updated": 0,
            }


class TestWhoDialsThisSite:
    """Assignment asked from the site's side, which is how the monitor asks."""

    async def test_agents_can_be_set_and_replaced_for_one_site(
        self, admin_client, targets
    ):
        card = await make_card(
            admin_client, enterprise_id=targets["enterprise_id"],
            phone="+380501234567",
        )
        first = (await admin_client.post(
            "/polling/agents", json={"name": "АРМ перший"})).json()
        second = (await admin_client.post(
            "/polling/agents", json={"name": "АРМ другий"})).json()

        assigned = (await admin_client.put(
            f"/polling/devices/{card['id']}/agents",
            json={"agent_ids": [first["id"], second["id"]]},
        )).json()
        assert sorted(assigned) == sorted([first["id"], second["id"]])

        # Moving the site to one machine must not need the other's whole set
        # rewritten — that is the reason this endpoint exists.
        moved = (await admin_client.put(
            f"/polling/devices/{card['id']}/agents",
            json={"agent_ids": [second["id"]]},
        )).json()
        assert moved == [second["id"]]

    async def test_a_site_nobody_took_says_so(self, admin_client, targets):
        card = await make_card(
            admin_client, enterprise_id=targets["enterprise_id"],
            phone="+380501234567",
        )
        cleared = (await admin_client.put(
            f"/polling/devices/{card['id']}/agents", json={"agent_ids": []},
        )).json()
        assert cleared == []


class TestHandingOutTheAgent:
    """The .exe an operator downloads from the agents screen.

    A build artifact rather than source, so the server may simply not have one
    — and that has to be an answer, not a 500 and not a broken button.
    """

    async def test_no_build_on_the_server_is_an_answer(
        self, admin_client, tmp_path, monkeypatch
    ):
        monkeypatch.setitem(backend_settings, "AGENT_DIST_DIR", str(tmp_path))

        info = (await admin_client.get("/polling/agents/installer/info")).json()
        assert info["available"] is False

        # And the file route says so in words, rather than serving nothing.
        missing = await admin_client.get("/polling/agents/installer")
        assert missing.status_code == 404
        assert "агент" in missing.json()["detail"].lower()

    async def test_the_newest_build_is_the_one_offered(
        self, admin_client, tmp_path, monkeypatch
    ):
        old = tmp_path / "hlv-poller-0.1.0.exe"
        new = tmp_path / "hlv-poller-0.2.0.exe"
        old.write_bytes(b"MZ old")
        new.write_bytes(b"MZ new")
        os.utime(old, (1_700_000_000, 1_700_000_000))
        os.utime(new, (1_800_000_000, 1_800_000_000))
        monkeypatch.setitem(backend_settings, "AGENT_DIST_DIR", str(tmp_path))

        info = (await admin_client.get("/polling/agents/installer/info")).json()
        assert info["available"] is True
        assert info["version"] == "0.2.0"
        assert info["filename"] == "hlv-poller-0.2.0.exe"
        assert info["size"] == len(b"MZ new")

        got = await admin_client.get("/polling/agents/installer")
        assert got.status_code == 200
        assert got.content == b"MZ new"
        # Named in the response, or the browser saves it as "installer".
        assert "hlv-poller-0.2.0.exe" in got.headers["content-disposition"]

    async def test_the_build_travels_as_a_zip(
        self, admin_client, tmp_path, monkeypatch
    ):
        """And is served as one.

        The .exe inside is already compressed — the archive saves a tenth of a
        megabyte. It is a zip because of where it has to go: into git, so a
        bundle carries it to an offline server, and out through a browser,
        where a bare .exe is what proxies and mail filters strike out.
        """
        (tmp_path / "hlv-poller-0.3.0.zip").write_bytes(b"PK zipped")
        monkeypatch.setitem(backend_settings, "AGENT_DIST_DIR", str(tmp_path))

        info = (await admin_client.get("/polling/agents/installer/info")).json()
        assert info["available"] is True
        assert info["version"] == "0.3.0"
        assert info["filename"] == "hlv-poller-0.3.0.zip"

        got = await admin_client.get("/polling/agents/installer")
        assert got.status_code == 200
        assert got.content == b"PK zipped"
        assert got.headers["content-type"] == "application/zip"
        assert "hlv-poller-0.3.0.zip" in got.headers["content-disposition"]

    async def test_a_server_holding_the_older_plain_exe_still_serves_it(
        self, admin_client, tmp_path, monkeypatch
    ):
        """Servers set up before the zip have an .exe sitting there."""
        (tmp_path / "hlv-poller-0.2.0.exe").write_bytes(b"MZ")
        monkeypatch.setitem(backend_settings, "AGENT_DIST_DIR", str(tmp_path))

        got = await admin_client.get("/polling/agents/installer")
        assert got.status_code == 200
        assert got.headers["content-type"].endswith("portable-executable")

    async def test_a_viewer_does_not_get_the_agent(self, viewer_client, tmp_path,
                                                   monkeypatch):
        (tmp_path / "hlv-poller-0.1.0.exe").write_bytes(b"MZ")
        monkeypatch.setitem(backend_settings, "AGENT_DIST_DIR", str(tmp_path))

        assert (await viewer_client.get(
            "/polling/agents/installer")).status_code == 403


class TestWhetherAnAgentIsThere:
    """Online is a fact about the last minute, not about the registry.

    The screen and the refusal «немає вільного модема» have to agree: an agent
    shown as on the line whose poll is then refused is worse than one shown as
    offline.
    """

    async def test_a_silent_agent_is_not_online(self, admin_client):
        agent = (await admin_client.post(
            "/polling/agents", json={"name": "АРМ мовчазний"})).json()
        assert agent["online"] is False           # never been heard from

        async with async_session_factory() as session:
            row = await session.get(PollAgent, agent["id"])
            row.last_seen_at = datetime.now() - timedelta(minutes=5)
            await session.commit()

        listed = (await admin_client.get("/polling/agents")).json()
        assert [a["online"] for a in listed if a["id"] == agent["id"]] == [False]

    async def test_an_agent_heard_from_just_now_is_online(self, admin_client):
        agent = (await admin_client.post(
            "/polling/agents", json={"name": "АРМ живий"})).json()

        async with async_session_factory() as session:
            row = await session.get(PollAgent, agent["id"])
            row.last_seen_at = datetime.now() - timedelta(seconds=5)
            await session.commit()

        listed = (await admin_client.get("/polling/agents")).json()
        assert [a["online"] for a in listed if a["id"] == agent["id"]] == [True]


class TestWhetherAnAgentIsBusy:
    """Online says the machine is switched on; busy says it is on the phone.

    The distinction is the whole of the question an operator asks when a poll
    is late: is my agent doing nothing, or is it already dialling somebody
    else. The claim on the card is the only record the server has of a call in
    progress — the modem is on the operator's machine — so that is what the
    screen reads.
    """

    async def test_an_agent_dialling_nobody_is_free(self, admin_client, targets):
        agent = (await admin_client.post(
            "/polling/agents", json={"name": "АРМ вільний"})).json()
        listed = (await admin_client.get("/polling/agents")).json()
        assert [a["busy"] for a in listed if a["id"] == agent["id"]] == [None]

    async def test_the_site_on_the_line_is_named(self, admin_client, targets):
        card = await make_card(
            admin_client, enterprise_id=targets["enterprise_id"],
            phone="+380671234567")
        agent = (await admin_client.post(
            "/polling/agents", json={"name": "АРМ на лінії"})).json()

        async with async_session_factory() as session:
            row = await session.get(PollDevice, card["id"])
            row.polling_agent_id = agent["id"]
            row.polling_since = datetime.now() - timedelta(minutes=2)
            row.progress_phase = "hourly"
            row.progress_done = 120
            row.progress_total = 168
            await session.commit()

        listed = (await admin_client.get("/polling/agents")).json()
        busy = next(a for a in listed if a["id"] == agent["id"])["busy"]
        assert busy["label"] == "Завод А"
        assert busy["poll_device_id"] == card["id"]
        assert (busy["phase"], busy["done"], busy["total"]) == ("hourly", 120, 168)

    async def test_a_claim_that_was_never_released_stops_counting(
        self, admin_client, targets
    ):
        # A machine switched off mid-session leaves its claim behind. It frees
        # itself after twenty minutes, and until then the screen would keep
        # reporting a call that ended long ago.
        card = await make_card(
            admin_client, dpd_device_id=targets["dpd_device_id"],
            phone="+380671234567")
        agent = (await admin_client.post(
            "/polling/agents", json={"name": "АРМ зниклий"})).json()

        async with async_session_factory() as session:
            row = await session.get(PollDevice, card["id"])
            row.polling_agent_id = agent["id"]
            row.polling_since = datetime.now() - timedelta(hours=3)
            await session.commit()

        listed = (await admin_client.get("/polling/agents")).json()
        assert [a["busy"] for a in listed if a["id"] == agent["id"]] == [None]


class TestTheLogOfTheLastPoll:
    """The file that answers "what did the last call do".

    The live log is wiped when the next session starts — deliberately, since
    the screen it feeds is about the call happening now. The file is what is
    left five minutes later, when somebody has to decide whether to send a
    person out to the meter.
    """

    async def test_a_site_never_polled_says_so_rather_than_failing(
        self, admin_client, targets, tmp_path, monkeypatch
    ):
        monkeypatch.setitem(backend_settings, "POLL_LOG_DIR", str(tmp_path))
        card = await make_card(
            admin_client, enterprise_id=targets["enterprise_id"],
            phone="+380501234567",
        )

        body = (await admin_client.get(
            f"/polling/devices/{card['id']}/log/last")).json()
        assert body["text"] is None

    async def test_the_session_is_written_and_read_back(
        self, admin_client, anon_client, targets, tmp_path, monkeypatch
    ):
        monkeypatch.setitem(backend_settings, "POLL_LOG_DIR", str(tmp_path))
        card, headers = await _claimed_card(admin_client, anon_client, targets)

        await anon_client.post(
            f"/polling/agent/devices/{card['id']}/log",
            json={"reset": True, "lines": [
                {"seq": 1, "level": "info", "message": "Набираю +380501234567"},
            ]},
            headers=headers,
        )
        await anon_client.post(
            f"/polling/agent/devices/{card['id']}/finish",
            json={"status": "ok", "error_code": None, "error_text": None,
                  "rows": {"hour": 24, "day": 1}, "duration_ms": 42000,
                  "connect_ms": 21000},
            headers=headers,
        )

        body = (await admin_client.get(
            f"/polling/devices/{card['id']}/log/last")).json()
        assert "Набираю +380501234567" in body["text"]
        # It opens with who was dialled and ends with the outcome, so neither
        # has to be inferred from the middle.
        assert "Завод А" in body["text"]
        assert "Готово: годин 24, діб 1" in body["text"]
        assert body["updated_at"] is not None

    async def test_a_new_session_replaces_the_last_one(
        self, admin_client, anon_client, targets, tmp_path, monkeypatch
    ):
        monkeypatch.setitem(backend_settings, "POLL_LOG_DIR", str(tmp_path))
        card, headers = await _claimed_card(admin_client, anon_client, targets)

        for message in ("Перший дзвінок", "Другий дзвінок"):
            await anon_client.post(
                f"/polling/agent/devices/{card['id']}/start", headers=headers)
            await anon_client.post(
                f"/polling/agent/devices/{card['id']}/log",
                json={"reset": True, "lines": [
                    {"seq": 1, "level": "info", "message": message},
                ]},
                headers=headers,
            )
            await anon_client.post(
                f"/polling/agent/devices/{card['id']}/finish",
                json={"status": "ok", "error_code": None, "error_text": None,
                      "rows": {"hour": 1, "day": 0}, "duration_ms": 1000,
                      "connect_ms": 500},
                headers=headers,
            )

        text = (await admin_client.get(
            f"/polling/devices/{card['id']}/log/last")).json()["text"]
        assert "Другий дзвінок" in text
        assert "Перший дзвінок" not in text


async def _claimed_card(admin_client, anon_client, targets) -> tuple[dict, dict]:
    """A card with a modem, an agent holding it, and the session started."""
    card = await make_card(
        admin_client, enterprise_id=targets["enterprise_id"],
        phone="+380501234567",
    )
    agent = (await admin_client.post(
        "/polling/agents", json={"name": "АРМ журнальний"})).json()
    await admin_client.put(
        f"/polling/devices/{card['id']}/agents", json={"agent_ids": [agent["id"]]},
    )
    headers = {"X-Agent-Key": agent["key"]}
    await anon_client.post(
        f"/polling/agent/devices/{card['id']}/start", headers=headers)
    return card, headers


@pytest.mark.asyncio
class TestStoppingAPollThatWasStartedByMistake:
    """Two different things wear the same button.

    A request nobody has taken is simply withdrawn. A call already running
    cannot be stopped from here at all — the modem is on another machine — so
    a flag is raised and the agent hangs up between records, which is the only
    moment at which nothing is half-read.
    """

    async def test_a_request_nobody_took_is_withdrawn(self, admin_client, targets):
        await _set_protocol(targets["corector_type_id"], 1054)
        card = await make_card(
            admin_client, enterprise_id=targets["enterprise_id"],
            phone="+380501234567",
        )
        async with async_session_factory() as session:
            row = await session.get(PollDevice, card["id"])
            row.manual_requested_at = datetime.now()
            await session.commit()

        resp = await admin_client.post(
            f"/polling/enterprises/{targets['enterprise_id']}/poll/cancel"
        )
        assert resp.status_code == 200
        assert resp.json()["outcome"] == "queued"

        async with async_session_factory() as session:
            row = await session.get(PollDevice, card["id"])
            assert row.manual_requested_at is None
            # Nothing was running, so nothing is asked to stop: a flag left
            # standing would end the retry somebody presses next.
            assert row.cancel_requested_at is None

    async def test_a_running_call_is_asked_to_hang_up(self, admin_client, targets):
        await _set_protocol(targets["corector_type_id"], 1054)
        card = await make_card(
            admin_client, enterprise_id=targets["enterprise_id"],
            phone="+380501234567",
        )
        agent = (await admin_client.post(
            "/polling/agents", json={"name": "АРМ на дзвінку"})).json()
        async with async_session_factory() as session:
            row = await session.get(PollDevice, card["id"])
            row.polling_agent_id = agent["id"]
            row.polling_since = datetime.now()
            await session.commit()

        resp = await admin_client.post(
            f"/polling/enterprises/{targets['enterprise_id']}/poll/cancel"
        )
        assert resp.json()["outcome"] == "asked"

        watch = (await admin_client.get(
            f"/polling/enterprises/{targets['enterprise_id']}/poll")).json()
        assert watch["cancelling"] is True

    async def test_nothing_running_is_said_rather_than_refused(
        self, admin_client, targets
    ):
        await make_card(
            admin_client, enterprise_id=targets["enterprise_id"],
            phone="+380501234567",
        )
        resp = await admin_client.post(
            f"/polling/enterprises/{targets['enterprise_id']}/poll/cancel"
        )
        assert resp.status_code == 200
        assert resp.json()["outcome"] == "idle"
