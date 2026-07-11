import json
from datetime import date, datetime, timedelta
from typing import Dict, List

from sqlalchemy import delete, text, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from backend.db.dao.basic_dao import BasicDao
from backend.db.models.dpd_cache_model import DpdVolumeCache

_COPY_COLS = ("ser_num", "mf_dev", "type_dev", "ch_num",
              "period_type", "day", "payload", "fetched_at")


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
        """Insert/merge per-(device, day) payload rows.

        `rows` are dicts with the DpdVolumeCache column names, unique per
        (device, period_type, day) — a key repeated within one call would make
        ON CONFLICT DO UPDATE fail ("cannot affect row a second time").

        On conflict the payload is MERGED stamp-wise in SQL (union by the
        record "date", the incoming record wins): polls run in parallel and
        two of them can write the same boundary day (one brings its
        00:00-06:00 hours, the other 07:00-23:00) — replacing the whole array
        would silently drop the other poll's records.

        asyncpg COPY into a temp table + one INSERT ... ON CONFLICT is ~20x
        faster than chunked multi-VALUES for a full-month poll (~9.5k JSONB
        rows). Everything runs inside the caller's transaction (no commit
        here — committing would release the poll's advisory lock mid-flight);
        temp tables are per-connection, so concurrent workers cannot clash."""
        if not rows:
            return
        cols = ", ".join(_COPY_COLS)
        await self.session.execute(text(
            "CREATE TEMP TABLE _tmp_dpd_volume_cache AS "
            f"SELECT {cols} FROM dpd_volume_cache WHERE FALSE"
        ))
        sa_conn = await self.session.connection()
        raw = await sa_conn.get_raw_connection()
        records = [
            tuple(json.dumps(r[c]) if c == "payload" else r[c] for c in _COPY_COLS)
            for r in rows
        ]
        await raw.driver_connection.copy_records_to_table(
            "_tmp_dpd_volume_cache", records=records, columns=list(_COPY_COLS)
        )
        await self.session.execute(text(
            f"INSERT INTO dpd_volume_cache ({cols}) "
            f"SELECT {cols} FROM _tmp_dpd_volume_cache "
            "ON CONFLICT ON CONSTRAINT uq_dpd_cache_device_period_day "
            "DO UPDATE SET payload = ("
            "  SELECT COALESCE(jsonb_agg(rec ORDER BY stamp), '[]'::jsonb)"
            "  FROM ("
            "    SELECT DISTINCT ON (rec->>'date') rec, rec->>'date' AS stamp"
            "    FROM ("
            "      SELECT r AS rec, 1 AS pri"
            "      FROM jsonb_array_elements(EXCLUDED.payload) AS r"
            "      UNION ALL"
            "      SELECT r AS rec, 0 AS pri"
            "      FROM jsonb_array_elements(dpd_volume_cache.payload) AS r"
            "    ) all_recs"
            "    ORDER BY rec->>'date', pri DESC"
            "  ) dedup"
            "), "
            "fetched_at = EXCLUDED.fetched_at"
        ))
        await self.session.execute(text("DROP TABLE _tmp_dpd_volume_cache"))

    async def touch(self, ids: List[int], now: datetime) -> None:
        """Sliding TTL: reading a final (closed-day) row extends its life."""
        if not ids:
            return
        await self.session.execute(
            update(DpdVolumeCache)
            .where(DpdVolumeCache.id.in_(ids))
            .values(fetched_at=now)
        )

    async def delete_older_than(self, now: datetime, days: int = 7) -> None:
        """Drop rows past the sliding TTL (untouched for `days`)."""
        await self.session.execute(
            delete(DpdVolumeCache).where(
                DpdVolumeCache.fetched_at < now - timedelta(days=days)
            )
        )
