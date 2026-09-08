"""The polling settings API: device cards, agents, default hours.

Step 1 of docs/plans/gsm-polling.md. Nothing polls anything yet, so what is
worth pinning here is who may do what and what the API refuses to tell:

  * settings are administrative, but asking for an out-of-turn poll is not —
    the person who notices a meter went quiet is rarely an admin;
  * an agent key exists in clear exactly once, in the response that creates it.
"""

import pytest
import pytest_asyncio

from backend.db.engine import async_session_factory
from backend.db.models.enterprise_model import DpdDevice
from backend.db.models.gas_volume_calc_model import GasVolumeCalc
from backend.db.models.grmu_branch_model import GrmuBranch
from backend.db.models.lumg_model import Lumg
from backend.db.models.polling_model import PollAgent


@pytest_asyncio.fixture
async def targets(seed_users) -> dict:
    async with async_session_factory() as session:
        branch = GrmuBranch(name="Тестова філія")
        session.add(branch)
        await session.flush()
        lumg = Lumg(name="ЛУМГ", branch_id=branch.id)
        session.add(lumg)
        await session.flush()
        calc = GasVolumeCalc(
            name="Флоутек-1", lumg_id=lumg.id, address=1, c_time=1
        )
        device = DpdDevice(ser_num=555001, ch_num=0)
        session.add(calc)
        session.add(device)
        await session.commit()
        return {
            "branch_id": branch.id,
            "calc_id": calc.id,
            "dpd_device_id": device.id,
        }


async def make_card(client, **body) -> dict:
    resp = await client.post("/polling/devices", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.asyncio
class TestDeviceCards:
    async def test_create_and_list(self, admin_client, targets):
        card = await make_card(
            admin_client,
            gas_volume_calc_id=targets["calc_id"],
            phone="0501234567",
            protocol_id=1070,
        )
        assert card["target_kind"] == "calc"
        assert card["target_label"] == "Флоутек-1"
        # Nobody has taken it yet, and that means it is never polled.
        assert card["agent_ids"] == []

        listed = (await admin_client.get("/polling/devices")).json()
        assert [c["id"] for c in listed] == [card["id"]]

    async def test_an_enterprise_corrector_is_labelled_by_serial(
        self, admin_client, targets
    ):
        card = await make_card(
            admin_client, dpd_device_id=targets["dpd_device_id"]
        )
        assert (card["target_kind"], card["target_label"]) == (
            "dpd_device", "№555001",
        )

    async def test_a_card_must_name_exactly_one_target(self, admin_client, targets):
        for body in (
            {},
            {"gas_volume_calc_id": targets["calc_id"],
             "dpd_device_id": targets["dpd_device_id"]},
        ):
            resp = await admin_client.post("/polling/devices", json=body)
            # 422 from the schema, not 500 from the CHECK constraint: the
            # operator gets a sentence rather than a database error.
            assert resp.status_code == 422, resp.text

    async def test_a_second_card_for_the_same_corrector_is_a_conflict(
        self, admin_client, targets
    ):
        await make_card(admin_client, gas_volume_calc_id=targets["calc_id"])
        resp = await admin_client.post(
            "/polling/devices", json={"gas_volume_calc_id": targets["calc_id"]}
        )
        assert resp.status_code == 409

    async def test_update_changes_only_what_was_sent(self, admin_client, targets):
        card = await make_card(
            admin_client,
            gas_volume_calc_id=targets["calc_id"],
            phone="0501234567",
            baud=2400,
        )
        resp = await admin_client.put(
            f"/polling/devices/{card['id']}", json={"baud": 9600}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["baud"] == 9600
        assert resp.json()["phone"] == "0501234567"

    async def test_poll_hours_are_validated(self, admin_client, targets):
        card = await make_card(admin_client, gas_volume_calc_id=targets["calc_id"])
        ok = await admin_client.put(
            f"/polling/devices/{card['id']}", json={"poll_times": ["06:00", "18:30"]}
        )
        assert ok.status_code == 200
        # "25:00" would be a slot the agent silently never reaches.
        bad = await admin_client.put(
            f"/polling/devices/{card['id']}", json={"poll_times": ["25:00"]}
        )
        assert bad.status_code == 400

    async def test_delete(self, admin_client, targets):
        card = await make_card(admin_client, gas_volume_calc_id=targets["calc_id"])
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
        card = await make_card(admin_client, gas_volume_calc_id=targets["calc_id"])
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
        first = await make_card(admin_client, gas_volume_calc_id=targets["calc_id"])
        second = await make_card(
            admin_client, dpd_device_id=targets["dpd_device_id"]
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
        card = await make_card(admin_client, gas_volume_calc_id=targets["calc_id"])
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
        card = await make_card(admin_client, gas_volume_calc_id=targets["calc_id"])

        resp = await viewer_client.post(f"/polling/devices/{card['id']}/poll")
        assert resp.status_code == 202, resp.text
        assert resp.json()["requested_at"] is not None

    async def test_a_viewer_may_cancel_the_request_they_made(
        self, admin_client, viewer_client, targets
    ):
        card = await make_card(admin_client, gas_volume_calc_id=targets["calc_id"])
        await viewer_client.post(f"/polling/devices/{card['id']}/poll")
        resp = await viewer_client.post(
            f"/polling/devices/{card['id']}/poll", params={"cancel": True}
        )
        assert resp.status_code == 202
        assert resp.json()["requested_at"] is None

    async def test_a_viewer_may_look_at_the_list(
        self, admin_client, viewer_client, targets
    ):
        await make_card(admin_client, gas_volume_calc_id=targets["calc_id"])
        assert (await viewer_client.get("/polling/devices")).status_code == 200

    async def test_a_viewer_may_not_change_settings(
        self, admin_client, viewer_client, targets
    ):
        card = await make_card(admin_client, gas_volume_calc_id=targets["calc_id"])
        assert (await viewer_client.put(
            f"/polling/devices/{card['id']}", json={"baud": 2400}
        )).status_code == 403
        assert (await viewer_client.post(
            "/polling/devices", json={"dpd_device_id": targets["dpd_device_id"]}
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
            gas_volume_calc_id=targets["calc_id"],
            phone="0501234567",
        )
        assert (await viewer_client.get(
            f"/polling/devices/{card['id']}"
        )).status_code == 403

    async def test_signing_in_is_still_required(self, anon_client):
        assert (await anon_client.get("/polling/devices")).status_code == 401
        assert (await anon_client.post("/polling/devices/1/poll")).status_code == 401
