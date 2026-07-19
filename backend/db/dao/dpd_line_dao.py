"""DAO for DPD lines: line/device-history queries and the per-line archive.

All methods flush but never commit — the caller owns the transaction."""
import logging
from datetime import date, datetime
from typing import Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from backend.db.dao.basic_dao import BasicDao
# Imported for SQLModel.metadata registration (schema creation in tests) —
# the archive DAO itself speaks raw SQL.
from backend.db.models.dpd_line_model import (  # noqa: F401
    DpdLine,
    DpdLineDailyArchive,
    DpdLineDevice,
    DpdLineHourlyArchive,
    DpdLineJob,
)

logger = logging.getLogger(__name__)

_TABLES = {
    "daily": ("dpd_line_daily_archive", "day", "uq_dpd_line_daily"),
    "hourly": ("dpd_line_hourly_archive", "stamp", "uq_dpd_line_hourly"),
}

_VALUE_COLS = ("volume", "pressure", "temperature", "press_unit")


def _table(period_type: str):
    try:
        return _TABLES[period_type]
    except KeyError:
        raise ValueError(f"Unknown period_type: {period_type!r}")


class DpdLineDao(BasicDao):
    def __init__(self, session: AsyncSession):
        super().__init__(session=session)

    async def get_all(
        self,
        branch_ids: Optional[List[int]] = None,
        include_in_trends: Optional[bool] = None,
        active: Optional[bool] = None,
    ) -> List[DpdLine]:
        stmt = select(DpdLine)
        if branch_ids is not None:
            stmt = stmt.where(DpdLine.branch_id.in_(branch_ids))
        if include_in_trends is not None:
            stmt = stmt.where(DpdLine.include_in_trends == include_in_trends)
        if active is not None:
            stmt = stmt.where(DpdLine.active == active)
        stmt = stmt.order_by(DpdLine.name)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_devices_resolved(self, dpd_line_id: int) -> List[Dict]:
        """Device history ordered by installed_from, with mf_dev/type_dev
        resolved through the corector_type → manufacturer catalog. Keys match
        what DPDClient.get_volumes expects (serNum/mfDev/typeDev/chNum)."""
        rows = (await self.session.execute(
            text(
                "SELECT dld.id, dld.ser_num, dld.corector_type_id, dld.ch_num, "
                "dld.installed_from, ct.type_dev, m.mf_dev, "
                "ct.model_name, m.short_name AS manufacturer_short_name "
                "FROM dpd_line_device dld "
                "JOIN corector_type ct ON ct.id = dld.corector_type_id "
                "JOIN manufacturer m ON m.id = ct.manufacturer_id "
                "WHERE dld.dpd_line_id = :line_id "
                "ORDER BY dld.installed_from"
            ),
            {"line_id": dpd_line_id},
        )).mappings().all()
        return [
            {
                "id": r["id"],
                "serNum": r["ser_num"],
                "mfDev": r["mf_dev"],
                "typeDev": r["type_dev"],
                "chNum": r["ch_num"],
                "corector_type_id": r["corector_type_id"],
                "installed_from": r["installed_from"],
                "model_name": r["model_name"],
                "manufacturer_short_name": r["manufacturer_short_name"],
            }
            for r in rows
        ]


class DpdLineArchiveDao(BasicDao):
    def __init__(self, session: AsyncSession):
        super().__init__(session=session)

    async def upsert_records(self, period_type: str, rows: List[Dict]) -> None:
        """Bulk insert/update archive rows.

        `rows`: dicts with dpd_line_id, stamp (datetime; date part is used
        for daily), volume, pressure, temperature, press_unit — unique per
        (dpd_line_id, stamp) within one call. COPY into a temp table + one
        INSERT ... ON CONFLICT DO UPDATE (same approach as DpdArchiveDao)."""
        if not rows:
            return
        table, stamp_col, constraint = _table(period_type)
        cols = ["dpd_line_id", stamp_col, *_VALUE_COLS]
        col_list = ", ".join(cols)
        tmp = f"_tmp_{table}"
        await self.session.execute(text(
            f"CREATE TEMP TABLE {tmp} AS SELECT {col_list} FROM {table} WHERE FALSE"
        ))
        sa_conn = await self.session.connection()
        raw = await sa_conn.get_raw_connection()
        records = [
            (
                r["dpd_line_id"],
                r["stamp"].date() if period_type == "daily" else r["stamp"],
                r.get("volume"), r.get("pressure"),
                r.get("temperature"), r.get("press_unit"),
            )
            for r in rows
        ]
        await raw.driver_connection.copy_records_to_table(
            tmp, records=records, columns=cols
        )
        set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in _VALUE_COLS)
        await self.session.execute(text(
            f"INSERT INTO {table} ({col_list}) SELECT {col_list} FROM {tmp} "
            f"ON CONFLICT ON CONSTRAINT {constraint} DO UPDATE SET {set_clause}"
        ))
        await self.session.execute(text(f"DROP TABLE {tmp}"))

    async def delete_line(
        self, dpd_line_id: int, period_type: Optional[str] = None
    ) -> None:
        """Wipe the line's archive (both tables unless period_type given) —
        the re-init semantics."""
        types = [period_type] if period_type else list(_TABLES)
        for pt in types:
            table, _, _ = _table(pt)
            await self.session.execute(
                text(f"DELETE FROM {table} WHERE dpd_line_id = :id"),
                {"id": dpd_line_id},
            )

    async def load_range(
        self,
        period_type: str,
        line_ids: List[int],
        range_from: datetime,
        range_to: datetime,
    ) -> List[Dict]:
        """Archive rows for the given lines within [range_from, range_to]
        (inclusive; for daily the dates of the bounds are used)."""
        if not line_ids:
            return []
        table, stamp_col, _ = _table(period_type)
        params: Dict = {"ids": line_ids}
        if period_type == "daily":
            params["from"], params["to"] = range_from.date(), range_to.date()
        else:
            params["from"], params["to"] = range_from, range_to
        rows = (await self.session.execute(
            text(
                f"SELECT dpd_line_id, {stamp_col} AS stamp, volume, pressure, "
                f"temperature, press_unit "
                f"FROM {table} "
                f"WHERE dpd_line_id = ANY(:ids) "
                f"AND {stamp_col} >= :from AND {stamp_col} <= :to "
                f"ORDER BY dpd_line_id, {stamp_col}"
            ),
            params,
        )).mappings().all()
        return [dict(r) for r in rows]

    async def last_period(
        self, dpd_line_id: int, period_type: str
    ) -> Optional[datetime | date]:
        """MAX(day) / MAX(stamp) of the line's archive, None when empty."""
        table, stamp_col, _ = _table(period_type)
        return await self.session.scalar(
            text(f"SELECT MAX({stamp_col}) FROM {table} WHERE dpd_line_id = :id"),
            {"id": dpd_line_id},
        )
