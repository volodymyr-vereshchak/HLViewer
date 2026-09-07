"""Importing an edited workbook back into the enterprise table.

The import matches a row to a point BY ITS DEVICE — serial, corrector type and
channel — which is what lets an export be edited and re-imported without an id
column. The same indirection is why a row can reach a point nobody named: a
serial typed into the wrong row belongs to whoever already has it.

These tests pin what an import may and may not overwrite. Postgres is real;
`upsert_enterprises` takes parsed rows, so no workbook is built here — the
parsing side is covered by test_enterprise_excel_install.py.
"""

from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import select

from backend.db.engine import async_session_factory
from backend.db.models.enterprise_model import (
    DpdDevice, Enterprise, EnterpriseDevice, EPOCH_INSTALLED_FROM,
)
from backend.db.models.grmu_branch_model import GrmuBranch
from backend.services.enterprise_excel import upsert_enterprises

MARCH = datetime(2026, 3, 10, 7)


@pytest_asyncio.fixture
async def branch_id(clean_db) -> int:
    async with async_session_factory() as session:
        branch = GrmuBranch(name="Тестова філія")
        session.add(branch)
        await session.commit()
        return branch.id


def row(name: str, ser_num: int, branch_id: int, installed_from=None, **over) -> dict:
    """One parsed workbook row, as parse_upload would hand it over."""
    return {
        "row": 3,
        "enterprise_name": name,
        "branch_id": branch_id,
        "ser_num": ser_num,
        "corector_type_id": None,
        "ch_num": 0,
        "line_id": None,
        "dpd_line_id": None,
        "active": True,
        "enabled": True,
        "installed_from": installed_from or EPOCH_INSTALLED_FROM,
        **over,
    }


async def point_named(name: str):
    async with async_session_factory() as session:
        return (await session.execute(
            select(Enterprise).where(Enterprise.enterprise_name == name)
        )).scalars().first()


async def history_of(enterprise_id: int):
    async with async_session_factory() as session:
        return list((await session.execute(
            select(EnterpriseDevice)
            .where(EnterpriseDevice.enterprise_id == enterprise_id)
            .order_by(EnterpriseDevice.installed_from)
        )).scalars())


@pytest.mark.asyncio
class TestCreatingAndUpdating:
    async def test_an_unknown_row_creates_a_point_with_one_entry(self, branch_id):
        ids, warnings = await upsert_enterprises([row("Завод А", 111, branch_id)])
        assert warnings == []
        assert len(ids) == 1
        assert len(await history_of(ids[0])) == 1

    async def test_re_importing_the_same_file_changes_nothing(self, branch_id):
        first, _ = await upsert_enterprises([row("Завод А", 111, branch_id)])
        again, warnings = await upsert_enterprises([row("Завод А", 111, branch_id)])
        assert again == first, "the row must find its own point, not make a second"
        assert warnings == []
        assert len(await history_of(first[0])) == 1

    async def test_a_row_updates_the_point_it_names(self, branch_id):
        ids, _ = await upsert_enterprises([row("Завод А", 111, branch_id)])
        await upsert_enterprises([row("завод а  ", 111, branch_id, active=False)])
        async with async_session_factory() as session:
            ent = await session.get(Enterprise, ids[0])
            # Case and stray spaces are typing noise, not a different point.
            assert ent.active is False


@pytest.mark.asyncio
class TestASerialUsedTwice:
    """The guard that made these tests worth writing: the device lookup is
    global, so a serial mistyped into a second row reaches the point the first
    row just updated — and renames it out of the table."""

    async def test_the_second_row_of_a_file_is_skipped(self, branch_id):
        first, _ = await upsert_enterprises([row("Завод А", 111, branch_id)])
        ids, warnings = await upsert_enterprises([
            row("Завод А", 111, branch_id),
            dict(row("Завод Б", 111, branch_id), row=4),
        ])

        assert ids == first, "only the row that owns the corrector may write"
        assert len(warnings) == 1
        assert "рядку 3" in warnings[0] and "Завод Б" not in warnings[0]
        assert await point_named("Завод А") is not None
        assert await point_named("Завод Б") is None

    async def test_it_holds_for_a_point_created_by_the_same_file(self, branch_id):
        # Nothing existed before this import: the first row creates the point,
        # the second must still not be able to take its corrector.
        ids, warnings = await upsert_enterprises([
            row("Завод А", 111, branch_id),
            dict(row("Завод Б", 111, branch_id), row=4),
        ])
        assert len(ids) == 1
        assert len(warnings) == 1
        assert await point_named("Завод Б") is None

    async def test_other_rows_of_the_same_file_still_import(self, branch_id):
        ids, warnings = await upsert_enterprises([
            row("Завод А", 111, branch_id),
            dict(row("Завод Б", 111, branch_id), row=4),
            dict(row("Завод В", 222, branch_id), row=5),
        ])
        # One bad row must not cost the operator the rest of the workbook.
        assert len(ids) == 2
        assert len(warnings) == 1
        assert await point_named("Завод В") is not None


@pytest.mark.asyncio
class TestRenaming:
    """Editing the name cell and re-importing is how a point gets renamed, so
    a single row carrying a name the device does not know cannot be refused —
    it is indistinguishable from a mistyped serial. It is reported instead."""

    async def test_the_rename_goes_through(self, branch_id):
        ids, _ = await upsert_enterprises([row("Завод А", 111, branch_id)])
        again, warnings = await upsert_enterprises([row("Завод Б", 111, branch_id)])

        assert again == ids, "a rename must not fork the point in two"
        assert await point_named("Завод А") is None
        assert await point_named("Завод Б") is not None
        # …but the operator is told which point just changed name, because a
        # serial typed into a new row looks exactly like this.
        assert len(warnings) == 1
        assert "Завод А" in warnings[0]

    async def test_the_same_name_is_not_reported(self, branch_id):
        await upsert_enterprises([row("Завод А", 111, branch_id)])
        _, warnings = await upsert_enterprises([row("  завод а ", 111, branch_id)])
        # Case and stray spaces are typing noise, not a rename.
        assert warnings == []


@pytest.mark.asyncio
class TestTheInstallDate:
    async def test_a_date_in_the_file_is_written(self, branch_id):
        ids, _ = await upsert_enterprises([row("Завод А", 111, branch_id)])
        await upsert_enterprises([row("Завод А", 111, branch_id, installed_from=MARCH)])
        assert (await history_of(ids[0]))[0].installed_from == MARCH

    async def test_a_blank_date_does_not_erase_a_known_one(self, branch_id):
        """Blanking it would drag this corrector's window back to the epoch,
        over everything its predecessor recorded."""
        ids, _ = await upsert_enterprises([
            row("Завод А", 111, branch_id, installed_from=MARCH)
        ])
        _, warnings = await upsert_enterprises([row("Завод А", 111, branch_id)])

        assert (await history_of(ids[0]))[0].installed_from == MARCH
        assert len(warnings) == 1
        assert "10.03.2026" in warnings[0]

    async def test_a_blank_date_is_fine_when_there_was_none(self, branch_id):
        # The normal case for a point that never had a replacement: every
        # workbook exported before the install columns existed looks like this.
        ids, _ = await upsert_enterprises([row("Завод А", 111, branch_id)])
        _, warnings = await upsert_enterprises([row("Завод А", 111, branch_id)])
        assert warnings == []
        assert (await history_of(ids[0]))[0].installed_from == EPOCH_INSTALLED_FROM


@pytest.mark.asyncio
class TestHistoryLeftAlone:
    async def test_a_replacement_survives_a_re_import_of_the_older_file(self, branch_id):
        """The whole reason the file carries only the current corrector: the
        admin panel is where replacements are entered, and an operator
        re-importing last week's export must not undo one."""
        ids, _ = await upsert_enterprises([row("Завод А", 111, branch_id)])
        async with async_session_factory() as session:
            device = DpdDevice(ser_num=222, ch_num=0)
            session.add(device)
            await session.flush()
            session.add(EnterpriseDevice(
                enterprise_id=ids[0], device_id=device.id, installed_from=MARCH,
            ))
            await session.commit()

        await upsert_enterprises([row("Завод А", 111, branch_id)])

        history = await history_of(ids[0])
        assert len(history) == 2
        assert history[-1].installed_from == MARCH

    async def test_a_point_missing_from_the_file_is_untouched(self, branch_id):
        kept, _ = await upsert_enterprises([row("Завод А", 111, branch_id)])
        await upsert_enterprises([row("Завод Б", 222, branch_id)])
        # Import is upsert-only: a partial workbook updates what it names and
        # leaves the rest of the table alone.
        assert await point_named("Завод А") is not None
        assert len(await history_of(kept[0])) == 1
