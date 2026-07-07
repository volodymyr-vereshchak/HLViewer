from datetime import date, datetime, timedelta
from typing import Dict, List

from sqlalchemy import delete, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from backend.db.dao.basic_dao import BasicDao
from backend.db.models.dpd_cache_model import DpdVolumeCache

# Rows per INSERT statement: 8 params each, kept well under asyncpg's 32767
# bound-parameter limit (a full month for a large branch is ~15k rows).
_UPSERT_CHUNK = 2000


def _device_tuples(devices: List[Dict]) -> list[tuple]:
    return [
        (d["serNum"], d["mfDev"], d["typeDev"], d["chNum"]) for d in devices
    ]


class DpdCacheDao(BasicDao):
    """All methods flush but never commit — the caller owns the transaction
    (fetch_dpd_volumes holds it together with the per-branch advisory lock)."""

    def __init__(self, session: AsyncSession):
        super().__init__(session=session)
        self.model = DpdVolumeCache

    async def load_range(
        self,
        devices: List[Dict],
        period_type: str,
        day_from: date,
        day_to: date,
    ) -> List[DpdVolumeCache]:
        if not devices:
            return []
        result = await self.session.execute(
            select(DpdVolumeCache).where(
                DpdVolumeCache.period_type == period_type,
                DpdVolumeCache.day >= day_from,
                DpdVolumeCache.day <= day_to,
                tuple_(
                    DpdVolumeCache.ser_num,
                    DpdVolumeCache.mf_dev,
                    DpdVolumeCache.type_dev,
                    DpdVolumeCache.ch_num,
                ).in_(_device_tuples(devices)),
            )
        )
        return list(result.scalars().all())

    async def upsert_days(self, rows: List[Dict]) -> None:
        """Insert/replace per-(device, day) payload rows.

        `rows` are dicts with the DpdVolumeCache column names."""
        for start in range(0, len(rows), _UPSERT_CHUNK):
            chunk = rows[start:start + _UPSERT_CHUNK]
            stmt = insert(DpdVolumeCache).values(chunk)
            stmt = stmt.on_conflict_do_update(
                constraint="uq_dpd_cache_device_period_day",
                set_={
                    "payload": stmt.excluded.payload,
                    "fetched_at": stmt.excluded.fetched_at,
                },
            )
            await self.session.execute(stmt)

    async def delete_older_than(self, now: datetime, days: int = 7) -> None:
        """Drop rows no reader will trust again (freshness TTL is ≤ 24h)."""
        await self.session.execute(
            delete(DpdVolumeCache).where(
                DpdVolumeCache.fetched_at < now - timedelta(days=days)
            )
        )
