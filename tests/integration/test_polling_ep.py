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
from backend.db.models.dpd_line_model import DpdLine
from backend.db.models.enterprise_model import (
    DpdDevice, Enterprise, EnterpriseDevice,
)
from backend.db.models.grmu_branch_model import GrmuBranch
from backend.db.models.polling_model import PollAgent


@pytest_asyncio.fixture
async def targets(seed_users) -> dict:
    """One site of each kind: a metering point and a DPD line."""
    async with async_session_factory() as session:
        branch = GrmuBranch(name="Тестова філія")
        session.add(branch)
        await session.flush()
        point = Enterprise(
            enterprise_name="Завод А", branch_id=branch.id,
            active=True, enabled=True,
        )
        line = DpdLine(name="Лінія 1", branch_id=branch.id)
        session.add(point)
        session.add(line)
        await session.commit()
        return {
            "branch_id": branch.id,
            "enterprise_id": point.id,
            "dpd_line_id": line.id,
        }


async def _fit(enterprise_id: int, ser_num: int, removed: bool = False) -> None:
    """Put a corrector at the point, optionally already taken off again."""
    async with async_session_factory() as session:
        device = DpdDevice(ser_num=ser_num, ch_num=0)
        session.add(device)
        await session.flush()
        session.add(EnterpriseDevice(
            enterprise_id=enterprise_id,
            device_id=device.id,
            installed_from=datetime(2026, 1, 1, 7) if not removed
            else datetime(2025, 1, 1, 7),
            removed_at=datetime(2026, 1, 1, 7) if removed else None,
        ))
        await session.commit()


async def make_card(client, **body) -> dict:
    resp = await client.post("/polling/devices", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.asyncio
class TestDeviceCards:
    async def test_create_and_list(self, admin_client, targets):
        card = await make_card(
            admin_client,
            enterprise_id=targets["enterprise_id"],
            phone="0501234567",
            protocol_id=1070,
        )
        assert card["target_kind"] == "enterprise"
        assert card["target_label"] == "Завод А"
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
            {"enterprise_id": targets["enterprise_id"],
             "dpd_line_id": targets["dpd_line_id"]},
        ):
            resp = await admin_client.post("/polling/devices", json=body)
            # 422 from the schema, not 500 from the CHECK constraint: the
            # operator gets a sentence rather than a database error.
            assert resp.status_code == 422, resp.text

    async def test_a_second_card_for_the_same_site_is_a_conflict(
        self, admin_client, targets
    ):
        await make_card(admin_client, enterprise_id=targets["enterprise_id"])
        resp = await admin_client.post(
            "/polling/devices", json={"enterprise_id": targets["enterprise_id"]}
        )
        assert resp.status_code == 409

    async def test_update_changes_only_what_was_sent(self, admin_client, targets):
        card = await make_card(
            admin_client,
            enterprise_id=targets["enterprise_id"],
            phone="0501234567",
            repeat_count=2,
        )
        resp = await admin_client.put(
            f"/polling/devices/{card['id']}", json={"repeat_count": 5}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["repeat_count"] == 5
        assert resp.json()["phone"] == "0501234567"

    async def test_an_unknown_field_is_refused(self, admin_client, targets):
        # The connection speed moved to the agent; a client still sending it
        # is out of date, and saying so beats storing it where nothing reads.
        card = await make_card(admin_client, enterprise_id=targets["enterprise_id"])
        resp = await admin_client.put(
            f"/polling/devices/{card['id']}", json={"baud": 9600}
        )
        assert resp.status_code == 422

    async def test_poll_hours_are_validated(self, admin_client, targets):
        card = await make_card(admin_client, enterprise_id=targets["enterprise_id"])
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
        card = await make_card(admin_client, enterprise_id=targets["enterprise_id"])
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
        card = await make_card(admin_client, enterprise_id=targets["enterprise_id"])
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
        first = await make_card(admin_client, enterprise_id=targets["enterprise_id"])
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
        card = await make_card(admin_client, enterprise_id=targets["enterprise_id"])
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
class TestHowASiteIsRead:
    """A site can be read through the DPD API, through a modem, or both.

    The pair lives on the point (or the line), not on the corrector, because
    the modem does: one sits at the site and the correctors behind it get
    replaced. It is set from the poll card because that is the moment somebody
    decides it — they are configuring a modem precisely because the API does
    not serve that site.
    """

    async def test_a_site_is_assumed_to_be_on_dpd(self, admin_client, targets):
        # Everything that existed before the GSM poll came from DPD.
        card = await make_card(admin_client, enterprise_id=targets["enterprise_id"])
        assert (card["poll_dpd"], card["poll_gsm"]) == (True, False)

    async def test_a_card_can_declare_the_site_gsm_only(self, admin_client, targets):
        card = await make_card(
            admin_client,
            enterprise_id=targets["enterprise_id"],
            poll_dpd=False,
            poll_gsm=True,
        )
        assert (card["poll_dpd"], card["poll_gsm"]) == (False, True)

        async with async_session_factory() as session:
            point = await session.get(Enterprise, targets["enterprise_id"])
            # The flags belong to the point, not to the poll card.
            assert (point.poll_dpd, point.poll_gsm) == (False, True)

    async def test_both_paths_at_once(self, admin_client, targets):
        card = await make_card(
            admin_client, enterprise_id=targets["enterprise_id"], poll_gsm=True
        )
        assert (card["poll_dpd"], card["poll_gsm"]) == (True, True)

    async def test_it_can_be_switched_back(self, admin_client, targets):
        card = await make_card(
            admin_client,
            enterprise_id=targets["enterprise_id"],
            poll_dpd=False,
            poll_gsm=True,
        )
        resp = await admin_client.put(
            f"/polling/devices/{card['id']}", json={"poll_dpd": True}
        )
        assert resp.json()["poll_dpd"] is True

    async def test_a_dpd_line_carries_the_same_pair(self, admin_client, targets):
        card = await make_card(
            admin_client, dpd_line_id=targets["dpd_line_id"], poll_gsm=True
        )
        assert (card["poll_dpd"], card["poll_gsm"]) == (True, True)


@pytest.mark.asyncio
class TestTheCorrectorAtThePoint:
    """The card names a site; which corrector answers is decided at poll time.

    The list shows the one fitted now, because that is what the modem expects
    to find and what a reply is checked against — a poll that reaches a
    different serial writes nothing and is raised for the operator.
    """

    async def test_a_point_without_a_corrector_says_so(self, admin_client, targets):
        card = await make_card(admin_client, enterprise_id=targets["enterprise_id"])
        assert card["device_id"] is None
        assert card["device_ser_num"] is None

    async def test_the_fitted_corrector_is_shown(self, admin_client, targets):
        await _fit(targets["enterprise_id"], 555001)
        card = await make_card(admin_client, enterprise_id=targets["enterprise_id"])
        assert card["device_ser_num"] == 555001

    async def test_a_replacement_moves_the_card_to_the_new_one(
        self, admin_client, targets
    ):
        # Nothing about the card changes when a corrector is swapped: the
        # phone belongs to the site, and the poll follows whatever is fitted.
        await _fit(targets["enterprise_id"], 555001, removed=True)
        await _fit(targets["enterprise_id"], 555002)
        card = await make_card(admin_client, enterprise_id=targets["enterprise_id"])
        assert card["device_ser_num"] == 555002


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
        card = await make_card(admin_client, enterprise_id=targets["enterprise_id"])

        resp = await viewer_client.post(f"/polling/devices/{card['id']}/poll")
        assert resp.status_code == 202, resp.text
        assert resp.json()["requested_at"] is not None

    async def test_a_viewer_may_cancel_the_request_they_made(
        self, admin_client, viewer_client, targets
    ):
        card = await make_card(admin_client, enterprise_id=targets["enterprise_id"])
        await viewer_client.post(f"/polling/devices/{card['id']}/poll")
        resp = await viewer_client.post(
            f"/polling/devices/{card['id']}/poll", params={"cancel": True}
        )
        assert resp.status_code == 202
        assert resp.json()["requested_at"] is None

    async def test_a_viewer_may_look_at_the_list(
        self, admin_client, viewer_client, targets
    ):
        await make_card(admin_client, enterprise_id=targets["enterprise_id"])
        assert (await viewer_client.get("/polling/devices")).status_code == 200

    async def test_a_viewer_may_not_change_settings(
        self, admin_client, viewer_client, targets
    ):
        card = await make_card(admin_client, enterprise_id=targets["enterprise_id"])
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
            enterprise_id=targets["enterprise_id"],
            phone="0501234567",
        )
        assert (await viewer_client.get(
            f"/polling/devices/{card['id']}"
        )).status_code == 403

    async def test_signing_in_is_still_required(self, anon_client):
        assert (await anon_client.get("/polling/devices")).status_code == 401
        assert (await anon_client.post("/polling/devices/1/poll")).status_code == 401
