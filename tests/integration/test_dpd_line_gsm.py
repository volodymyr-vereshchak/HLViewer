"""A ДПД line can have a modem of its own, set up like an enterprise's.

The line card already existed — the monitor has always been able to hold one —
but the only way to create it was the poll screen, which meant a line was set
up in one place and its modem in another. Now it travels with the line.
"""
from datetime import datetime

import pytest

from backend.db.engine import async_session_factory
from backend.db.models.device_catalog_model import CorectorType, Manufacturer
from backend.db.models.grmu_branch_model import GrmuBranch


@pytest.fixture
async def line_target(seed_users) -> dict:
    """A branch and a corrector model to hang a line on."""
    async with async_session_factory() as session:
        branch = GrmuBranch(name="Філія з лінією")
        mfr = Manufacturer(short_name="Тандем", full_name="Тандем", mf_dev=4)
        session.add(branch)
        session.add(mfr)
        await session.flush()
        kind = CorectorType(manufacturer_id=mfr.id, model_name="ТАНДЕМ-ТР",
                            type_dev=1)
        session.add(kind)
        await session.flush()
        await session.commit()
        return {"branch_id": branch.id, "corector_type_id": kind.id}


def body(target, **extra) -> dict:
    return {
        "name": "Радушне",
        "branch_id": target["branch_id"],
        "devices": [{
            "ser_num": 1099,
            "corector_type_id": target["corector_type_id"],
            "ch_num": 0,
            "installed_from": datetime(2026, 1, 1, 7).isoformat(),
        }],
        **extra,
    }


MODEM = {
    "phone": "+380671234567",
    "auto_poll": True,
    "poll_cron": "0 8 * * *",
    "agent_ids": [],
    "password": "11",
}


@pytest.mark.asyncio
class TestLineModem:
    async def test_a_line_saved_with_a_modem_keeps_it(self, admin_client, line_target):
        created = await admin_client.post("/dpd_lines/", json=body(line_target, gsm=MODEM))
        assert created.status_code == 201, created.text
        assert created.json()["gsm"]["phone"] == "+380671234567"

        listed = (await admin_client.get("/dpd_lines/")).json()
        assert listed[0]["gsm"] == {
            "phone": "+380671234567", "auto_poll": True,
            "poll_cron": "0 8 * * *", "agent_ids": [], "password": "11",
        }

    async def test_a_line_without_one_says_so(self, admin_client, line_target):
        created = await admin_client.post("/dpd_lines/", json=body(line_target))
        assert created.json()["gsm"] is None

    async def test_the_number_is_brought_to_one_shape(self, admin_client, line_target):
        # "050…" is what people write down, and it is not what a modem dials.
        created = await admin_client.post(
            "/dpd_lines/", json=body(line_target, gsm={**MODEM, "phone": "0671234567"}))
        assert created.json()["gsm"]["phone"] == "+380671234567"

    async def test_a_number_no_modem_can_dial_is_refused(self, admin_client, line_target):
        refused = await admin_client.post(
            "/dpd_lines/", json=body(line_target, gsm={**MODEM, "phone": "+1555"}))
        assert refused.status_code == 422

    async def test_clearing_the_number_removes_the_card(self, admin_client, line_target):
        created = await admin_client.post("/dpd_lines/", json=body(line_target, gsm=MODEM))
        line_id = created.json()["id"]

        updated = await admin_client.patch(
            f"/dpd_lines/{line_id}", json=body(line_target, gsm={**MODEM, "phone": ""}))
        assert updated.status_code == 200, updated.text
        assert updated.json()["gsm"] is None
        # And it is gone from the poll screen too, rather than left as a card
        # that is scheduled and can never dial.
        cards = (await admin_client.get("/polling/devices")).json()
        assert [c for c in cards if c.get("dpd_line_id") == line_id] == []

    async def test_a_line_polled_by_gsm_appears_on_the_poll_screen(
        self, admin_client, line_target
    ):
        created = await admin_client.post("/dpd_lines/", json=body(line_target, gsm=MODEM))
        line_id = created.json()["id"]

        cards = (await admin_client.get("/polling/devices")).json()
        card = next(c for c in cards if c.get("dpd_line_id") == line_id)
        # Named by the line, and pointed at the corrector the line's own
        # history says is on it today.
        assert card["target_kind"] == "dpd_line"
        assert card["target_label"] == "Радушне"
        assert card["ser_num"] == 1099


@pytest.mark.asyncio
class TestLineName:
    """A name is unique within its branch; taking one twice is the
    operator's mistake to fix, so it is a 409 with a sentence, not a 500."""

    async def test_a_second_line_with_the_same_name_is_a_conflict(
        self, admin_client, line_target
    ):
        assert (await admin_client.post(
            "/dpd_lines/", json=body(line_target))).status_code == 201
        again = await admin_client.post("/dpd_lines/", json=body(line_target))
        assert again.status_code == 409
        assert "Радушне" in again.json()["detail"]

    async def test_renaming_onto_a_taken_name_is_a_conflict(
        self, admin_client, line_target
    ):
        await admin_client.post("/dpd_lines/", json=body(line_target))
        other = (await admin_client.post(
            "/dpd_lines/", json=body(line_target, name="Інша"))).json()
        resp = await admin_client.patch(
            f"/dpd_lines/{other['id']}", json=body(line_target))
        assert resp.status_code == 409
