"""The polling settings API: device cards, agents, default hours.

Step 1 of docs/plans/gsm-polling.md. Nothing polls anything yet, so what is
worth pinning here is who may do what and what the API refuses to tell:

  * settings are administrative, but asking for an out-of-turn poll is not —
    the person who notices a meter went quiet is rarely an admin;
  * an agent key exists in clear exactly once, in the response that creates it.
"""

from datetime import datetime

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
from backend.db.models.polling_model import PollAgent


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
        # modem will check the reply against.
        assert card["target_label"] == "Завод А"
        assert card["ser_num"] == 555001
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

    async def test_poll_hours_are_validated(self, admin_client, targets):
        card = await make_card(admin_client, dpd_device_id=targets["dpd_device_id"])
        ok = await admin_client.put(
            f"/polling/devices/{card['id']}", json={"poll_times": ["18:30", "06:00"]}
        )
        assert ok.status_code == 200
        # Sorted on the way in: the list is read as a daily rhythm.
        assert ok.json()["poll_times"] == ["06:00", "18:30"]
        # "25:00" would be a slot the agent silently never reaches.
        bad = await admin_client.put(
            f"/polling/devices/{card['id']}", json={"poll_times": ["25:00"]}
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
        resp = await admin_client.get("/polling/schedule")
        assert resp.status_code == 200
        assert resp.json()["poll_times"] == ["06:00"]

    async def test_setting_hours(self, admin_client):
        resp = await admin_client.put(
            "/polling/schedule", json={"poll_times": ["07:00", "19:00"]}
        )
        assert resp.json()["poll_times"] == ["07:00", "19:00"]
        assert (await admin_client.get("/polling/schedule")).json()["poll_times"] == [
            "07:00", "19:00",
        ]

    async def test_a_bad_hour_is_refused(self, admin_client):
        resp = await admin_client.put(
            "/polling/schedule", json={"poll_times": ["7:00 ранку"]}
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
