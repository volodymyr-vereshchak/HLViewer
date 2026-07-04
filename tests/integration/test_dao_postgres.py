"""DAO tests against the real test Postgres.

Focus: the Postgres-specific bulk paths (asyncpg COPY upsert, ON CONFLICT
update), the per-line "latest record" query, and the get_or_create /
update_if_exists idempotency helpers used by the ingest pipeline.
"""

from datetime import datetime

from sqlalchemy import text
from sqlmodel import select

from backend.db.dao.gas_volume_calc_dao import GasVolumeCalcDao
from backend.db.dao.hourly_archive_dao import HourlyArchiveDao
from backend.db.dao.line_dao import LineDao
from backend.db.dao.lumg_dao import LumgDao
from backend.db.engine import async_session_factory
from backend.db.models import GasVolumeCalc, Line
from backend.db.models.hourly_archive_model import (
    HOURLY_ARCHIVE_CONSTRAINT,
    HourlyArchive,
)


def _hour_record(line_id: int, hour: int, volume: float = 100.0, **overrides) -> dict:
    record = {
        "period": datetime(2024, 12, 25, hour),
        "volume": volume,
        "w_volume_dp": 0.1,
        "pressure": 5.2,
        "temperature": 20.5,
        "density": 0.7,
        "line_id": line_id,
    }
    record.update(overrides)
    return record


async def _all_hourly() -> list[HourlyArchive]:
    async with async_session_factory() as session:
        result = await session.execute(
            select(HourlyArchive).order_by(HourlyArchive.line_id, HourlyArchive.period)
        )
        return result.scalars().all()


class TestBulkUpsertViaCopy:
    async def test_inserts_records(self, seed_topology):
        line_id = seed_topology["line1"]
        records = [_hour_record(line_id, hour) for hour in range(3)]
        async with async_session_factory() as session:
            await HourlyArchiveDao(session).bulk_upsert_via_copy(
                records, HOURLY_ARCHIVE_CONSTRAINT
            )
        rows = await _all_hourly()
        assert len(rows) == 3
        assert rows[0].volume == 100.0
        assert rows[0].period == datetime(2024, 12, 25, 0)
        # created_at/updated_at are filled server-side (NOW()) for COPY rows
        assert rows[0].created_at is not None

    async def test_reimport_is_deduplicated(self, seed_topology):
        line_id = seed_topology["line1"]
        records = [_hour_record(line_id, hour) for hour in range(3)]
        async with async_session_factory() as session:
            dao = HourlyArchiveDao(session)
            await dao.bulk_upsert_via_copy(records, HOURLY_ARCHIVE_CONSTRAINT)
            # the updater re-imports overlapping windows every cycle
            await dao.bulk_upsert_via_copy(records, HOURLY_ARCHIVE_CONSTRAINT)
        assert len(await _all_hourly()) == 3

    async def test_duplicates_do_not_burn_sequence(self, seed_topology):
        """Regression: the WHERE NOT EXISTS pre-filter must keep nextval() from
        being evaluated for duplicate rows (this once exhausted a BigInt seq)."""
        line_id = seed_topology["line1"]
        records = [_hour_record(line_id, hour) for hour in range(3)]
        async with async_session_factory() as session:
            dao = HourlyArchiveDao(session)
            await dao.bulk_upsert_via_copy(records, HOURLY_ARCHIVE_CONSTRAINT)
            for _ in range(5):  # five overlapping re-imports
                await dao.bulk_upsert_via_copy(records, HOURLY_ARCHIVE_CONSTRAINT)
            await dao.bulk_upsert_via_copy(
                [_hour_record(line_id, 23)], HOURLY_ARCHIVE_CONSTRAINT
            )
        rows = await _all_hourly()
        # ids stay dense: 3 originals + 1 new → max id 4, no gap from re-imports
        assert max(r.id for r in rows) == 4

    async def test_mixed_new_and_duplicate(self, seed_topology):
        line_id = seed_topology["line1"]
        async with async_session_factory() as session:
            dao = HourlyArchiveDao(session)
            await dao.bulk_upsert_via_copy(
                [_hour_record(line_id, 0)], HOURLY_ARCHIVE_CONSTRAINT
            )
            await dao.bulk_upsert_via_copy(
                [_hour_record(line_id, 0), _hour_record(line_id, 1)],
                HOURLY_ARCHIVE_CONSTRAINT,
            )
        assert len(await _all_hourly()) == 2

    async def test_empty_list_is_noop(self, seed_topology):
        async with async_session_factory() as session:
            await HourlyArchiveDao(session).bulk_upsert_via_copy(
                [], HOURLY_ARCHIVE_CONSTRAINT
            )
        assert await _all_hourly() == []


class TestBulkUpsertWithUpdate:
    async def test_conflict_updates_row(self, seed_topology):
        line_id = seed_topology["line1"]
        record = _hour_record(line_id, 0, pressure=5.2)
        async with async_session_factory() as session:
            dao = HourlyArchiveDao(session)
            await dao.bulk_upsert_with_update([record], HOURLY_ARCHIVE_CONSTRAINT)
            # same constraint key (line_id, period, volume) → update, not insert
            changed = dict(record, pressure=9.9)
            await dao.bulk_upsert_with_update([changed], HOURLY_ARCHIVE_CONSTRAINT)
        rows = await _all_hourly()
        assert len(rows) == 1
        assert rows[0].pressure == 9.9


class TestGetLastPerLineIds:
    async def test_latest_record_per_line(self, seed_topology):
        line1, line2 = seed_topology["line1"], seed_topology["line2"]
        records = [
            _hour_record(line1, 0),
            _hour_record(line1, 5, volume=105.0),
            _hour_record(line2, 3, volume=203.0),
        ]
        async with async_session_factory() as session:
            dao = HourlyArchiveDao(session)
            await dao.bulk_upsert_via_copy(records, HOURLY_ARCHIVE_CONSTRAINT)
            latest = await dao.get_last_per_line_ids()
        by_line = {r.line_id: r for r in latest}
        assert set(by_line) == {line1, line2}
        assert by_line[line1].period == datetime(2024, 12, 25, 5)
        assert by_line[line2].volume == 203.0

    async def test_to_date_and_line_filter(self, seed_topology):
        line1, line2 = seed_topology["line1"], seed_topology["line2"]
        records = [
            _hour_record(line1, 0),
            _hour_record(line1, 10, volume=110.0),
            _hour_record(line2, 1, volume=201.0),
        ]
        async with async_session_factory() as session:
            dao = HourlyArchiveDao(session)
            await dao.bulk_upsert_via_copy(records, HOURLY_ARCHIVE_CONSTRAINT)
            latest = await dao.get_last_per_line_ids(
                to_date=datetime(2024, 12, 25, 5), line_ids=[line1]
            )
        assert len(latest) == 1
        assert latest[0].line_id == line1
        assert latest[0].period == datetime(2024, 12, 25, 0)


class TestGetOrCreate:
    async def test_calc_created_with_technical_name(self, seed_topology):
        async with async_session_factory() as session:
            dao = GasVolumeCalcDao(session)
            calc = await dao.get_or_create(address=77, lumg_id=seed_topology["lumg"])
        assert calc.id is not None
        assert calc.name == "a77"  # default technical name

    async def test_calc_existing_reused(self, seed_topology):
        async with async_session_factory() as session:
            dao = GasVolumeCalcDao(session)
            calc = await dao.get_or_create(address=12, lumg_id=seed_topology["lumg"])
            assert calc.id == seed_topology["calc"]
            count = len(
                (await session.execute(select(GasVolumeCalc))).scalars().all()
            )
        assert count == 1

    async def test_line_created_with_technical_name(self, seed_topology):
        async with async_session_factory() as session:
            dao = LineDao(session)
            line = await dao.get_or_create(
                gas_volume_calc_id=seed_topology["calc"], line=3
            )
        assert line.name == "l3"

    async def test_line_existing_reused(self, seed_topology):
        async with async_session_factory() as session:
            dao = LineDao(session)
            line = await dao.get_or_create(
                gas_volume_calc_id=seed_topology["calc"], line=1
            )
            assert line.id == seed_topology["line1"]
            count = len((await session.execute(select(Line))).scalars().all())
        assert count == 2


class TestUpdateIfExists:
    async def test_calc_updated(self, seed_topology):
        async with async_session_factory() as session:
            dao = GasVolumeCalcDao(session)
            updated = await dao.update_if_exists(
                address=12, lumg_id=seed_topology["lumg"], name="ГРС Оновлена"
            )
        assert updated is not None
        assert updated.name == "ГРС Оновлена"

    async def test_calc_missing_not_created(self, seed_topology):
        async with async_session_factory() as session:
            dao = GasVolumeCalcDao(session)
            result = await dao.update_if_exists(
                address=999, lumg_id=seed_topology["lumg"], name="Привид"
            )
            count = len(
                (await session.execute(select(GasVolumeCalc))).scalars().all()
            )
        assert result is None
        assert count == 1  # only the seeded calc

    async def test_line_updated(self, seed_topology):
        async with async_session_factory() as session:
            dao = LineDao(session)
            updated = await dao.update_if_exists(
                gas_volume_calc_id=seed_topology["calc"],
                line=1,
                name="Лінія №1",
                meter=True,
            )
        assert updated is not None
        assert updated.name == "Лінія №1"
        assert updated.meter is True

    async def test_lumg_update_if_exist(self, seed_topology):
        async with async_session_factory() as session:
            dao = LumgDao(session)
            assert await dao.update_if_exist("TESTLUMG") is not None
            assert await dao.update_if_exist("NOSUCHLUMG") is None
