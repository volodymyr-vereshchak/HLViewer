"""End-to-end ingest tests: generated binary hostlib files in a tmp dir →
Hostlib.read() (HourlyEngine/DailyEngine) → bulk_upsert_via_copy → rows in the
test database. Verifies auto-creation of calcs/lines, cache reuse and chunking.
"""

import struct
from datetime import date, datetime

from sqlmodel import select

from backend.db.dao.daily_archive_dao import DailyArchiveDao
from backend.db.dao.hourly_archive_dao import HourlyArchiveDao
from backend.db.engine import async_session_factory
from backend.db.models import GasVolumeCalc, Line
from backend.db.models.daily_archive_model import DAILY_ARCHIVE_CONSTRAINT, DailyArchive
from backend.db.models.hourly_archive_model import (
    HOURLY_ARCHIVE_CONSTRAINT,
    HourlyArchive,
)
from backend.hl_engine.daily_engine import DailyEngine
from backend.hl_engine.hourly_engine import HourlyEngine


def _pack_hours(day: int, hours: range) -> bytes:
    return b"".join(
        struct.pack(
            "=5B6f", 12, day, 24, hour, 0, 1000.0 + hour, 0.0, 0.1, 5.2, 20.5, 0.7
        )
        for hour in hours
    )


def _pack_days(days: range) -> bytes:
    return b"".join(
        struct.pack("=3B6f", 12, day, 24, 24000.0 + day, 0.0, 2.4, 5.2, 20.5, 0.7)
        for day in days
    )


async def _ingest_hourly(path: str, lumg_id: int, chunk_size: int = 900) -> int:
    """Run the same pipeline as the production updater: engine chunks → COPY upsert."""
    chunks = 0
    async with async_session_factory() as session:
        engine = HourlyEngine(
            session=session, path=path, lumg_id=lumg_id, chunk_size=chunk_size
        )
        dao = HourlyArchiveDao(session)
        async for chunk in engine.read():
            await dao.bulk_upsert_via_copy(chunk, HOURLY_ARCHIVE_CONSTRAINT)
            chunks += 1
    return chunks


class TestHourlyIngest:
    async def test_creates_topology_and_rows(self, seed_topology, tmp_path):
        # address 034 / line 1 does not exist yet → must be auto-created
        (tmp_path / "S034R1R.24C").write_bytes(_pack_hours(day=25, hours=range(24)))
        await _ingest_hourly(str(tmp_path), seed_topology["lumg"])

        async with async_session_factory() as session:
            calc = (
                await session.execute(
                    select(GasVolumeCalc).where(GasVolumeCalc.address == 34)
                )
            ).scalar_one()
            line = (
                await session.execute(
                    select(Line).where(Line.gas_volume_calc_id == calc.id)
                )
            ).scalar_one()
            rows = (
                (await session.execute(select(HourlyArchive))).scalars().all()
            )

        assert calc.name == "a34"  # technical name until ask.cfg rename
        assert calc.lumg_id == seed_topology["lumg"]
        assert line.name == "l1"
        assert len(rows) == 24
        assert all(r.line_id == line.id for r in rows)
        periods = sorted(r.period for r in rows)
        assert periods[0] == datetime(2024, 12, 25, 0)
        assert periods[-1] == datetime(2024, 12, 25, 23)

    async def test_existing_line_reused(self, seed_topology, tmp_path):
        # address 012 / line 1 already exist (seed_topology) → no duplicates
        (tmp_path / "S012R1R.24C").write_bytes(_pack_hours(day=25, hours=range(3)))
        await _ingest_hourly(str(tmp_path), seed_topology["lumg"])

        async with async_session_factory() as session:
            calcs = (await session.execute(select(GasVolumeCalc))).scalars().all()
            lines = (await session.execute(select(Line))).scalars().all()
            rows = (await session.execute(select(HourlyArchive))).scalars().all()

        assert len(calcs) == 1
        assert len(lines) == 2
        assert {r.line_id for r in rows} == {seed_topology["line1"]}

    async def test_multiple_files_and_chunking(self, seed_topology, tmp_path):
        (tmp_path / "S012R1R.24C").write_bytes(_pack_hours(day=25, hours=range(24)))
        (tmp_path / "S012R2R.24C").write_bytes(_pack_hours(day=26, hours=range(24)))
        # 48 records with chunk_size=20 → 3 chunks (20 + 20 + 8)
        chunks = await _ingest_hourly(
            str(tmp_path), seed_topology["lumg"], chunk_size=20
        )
        assert chunks == 3

        async with async_session_factory() as session:
            rows = (await session.execute(select(HourlyArchive))).scalars().all()
        assert len(rows) == 48
        by_line = {seed_topology["line1"]: 0, seed_topology["line2"]: 0}
        for r in rows:
            by_line[r.line_id] += 1
        assert by_line == {seed_topology["line1"]: 24, seed_topology["line2"]: 24}

    async def test_reingest_no_duplicates(self, seed_topology, tmp_path):
        (tmp_path / "S012R1R.24C").write_bytes(_pack_hours(day=25, hours=range(5)))
        await _ingest_hourly(str(tmp_path), seed_topology["lumg"])
        await _ingest_hourly(str(tmp_path), seed_topology["lumg"])

        async with async_session_factory() as session:
            rows = (await session.execute(select(HourlyArchive))).scalars().all()
        assert len(rows) == 5

    async def test_non_matching_files_ignored(self, seed_topology, tmp_path):
        (tmp_path / "readme.txt").write_text("not an archive")
        (tmp_path / "S012R1D.24C").write_bytes(_pack_days(range(25, 27)))  # daily mask
        chunks = await _ingest_hourly(str(tmp_path), seed_topology["lumg"])
        assert chunks == 0

        async with async_session_factory() as session:
            rows = (await session.execute(select(HourlyArchive))).scalars().all()
        assert rows == []


class TestDailyIngest:
    async def test_daily_rows_with_date_period(self, seed_topology, tmp_path):
        (tmp_path / "S012R1D.24C").write_bytes(_pack_days(range(20, 25)))

        async with async_session_factory() as session:
            engine = DailyEngine(
                session=session, path=str(tmp_path), lumg_id=seed_topology["lumg"]
            )
            dao = DailyArchiveDao(session)
            async for chunk in engine.read():
                await dao.bulk_upsert_via_copy(chunk, DAILY_ARCHIVE_CONSTRAINT)

        async with async_session_factory() as session:
            rows = (await session.execute(select(DailyArchive))).scalars().all()
        assert len(rows) == 5
        periods = sorted(r.period for r in rows)
        assert periods[0] == date(2024, 12, 20)
        assert periods[-1] == date(2024, 12, 24)
