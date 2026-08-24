"""DAO for the scheduler-fed DPD archive (daily/hourly tables + coverage).

All methods flush but never commit — the caller owns the transaction."""
import logging
from datetime import date, datetime, timedelta
from typing import Dict, List

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.dao.basic_dao import BasicDao
# Imported for SQLModel.metadata registration (schema creation in tests and
# anywhere create_all is used) — the DAO itself speaks raw SQL.
from backend.db.models.dpd_cache_model import (  # noqa: F401
    DpdDailyArchive,
    DpdDeviceCoverage,
    DpdHourlyArchive,
    DpdRefreshJob,
)

logger = logging.getLogger(__name__)

# Retention (user decisions 2026-07-12): records older than a year (by the
# record's own date) are pruned once nobody has read them for 7 days.
RETENTION_DAYS = 365
RETENTION_ACCESS_GRACE_DAYS = 7

_TABLES = {
    "daily": ("dpd_daily_archive", "day"),
    "hourly": ("dpd_hourly_archive", "stamp"),
}

_VALUE_COLS = ("dvst_alwrk", "dvwrk_alwrk", "press", "temper", "press_unit")


def _table(period_type: str):
    try:
        return _TABLES[period_type]
    except KeyError:
        raise ValueError(f"Unknown period_type: {period_type!r}")


class DpdArchiveDao(BasicDao):
    def __init__(self, session: AsyncSession):
        super().__init__(session=session)

    async def load_range(
        self,
        device_ids: List[int],
        period_type: str,
        range_from: datetime,
        range_to: datetime,
    ) -> List[Dict]:
        """Archive rows for the given devices within [range_from, range_to]
        (inclusive; for daily the dates of the bounds are used)."""
        if not device_ids:
            return []
        table, stamp_col = _table(period_type)
        params: Dict = {"ids": device_ids}
        if period_type == "daily":
            params["from"], params["to"] = range_from.date(), range_to.date()
        else:
            params["from"], params["to"] = range_from, range_to
        rows = (await self.session.execute(
            text(
                f"SELECT device_id, {stamp_col} AS stamp, dvst_alwrk, "
                f"dvwrk_alwrk, press, temper, press_unit "
                f"FROM {table} "
                f"WHERE device_id = ANY(:ids) "
                f"AND {stamp_col} >= :from AND {stamp_col} <= :to"
            ),
            params,
        )).mappings().all()
        return [dict(r) for r in rows]

    async def load_windows(
        self, period_type: str, windows: List[Dict], hours: List[int] = None
    ) -> List[Dict]:
        """Archive rows per ASSIGNMENT window, tagged with the assignment.

        `windows`: dicts with tag, device_id, win_from, win_to (inclusive
        datetimes). Each window reads its own device's archive clipped to it,
        so a corrector that moved between metering points contributes to each
        only for the stretch it actually stood there — and a period no device
        covered yields no row at all rather than the previous device's gas.

        One query over a VALUES join: a point with a long history would
        otherwise cost a round trip per entry.

        `hours` (hourly only) keeps just those wall-clock hours. The night
        report reads nine of twenty-four, and the rows it would throw away
        cost the same to fetch, hand to Python and aggregate as the ones it
        keeps.
        """
        if not windows:
            return []
        table, stamp_col = _table(period_type)
        rows_in = []
        for w in windows:
            win_from, win_to = w["win_from"], w["win_to"]
            if period_type == "daily":
                win_from, win_to = win_from.date(), win_to.date()
            rows_in.append(
                {"tag": w["tag"], "device_id": w["device_id"],
                 "wf": win_from, "wt": win_to}
            )
        # Every VALUES parameter carries an explicit cast: Postgres cannot
        # infer a type for a bare placeholder there and asyncpg then tries to
        # send everything as text. CAST(), not `::` — text() would read the
        # second colon as the start of another bind parameter.
        stamp_type = "date" if period_type == "daily" else "timestamp"
        values = ", ".join(
            f"(CAST(:tag{i} AS bigint), CAST(:device_id{i} AS bigint), "
            f"CAST(:wf{i} AS {stamp_type}), CAST(:wt{i} AS {stamp_type}))"
            for i in range(len(rows_in))
        )
        params: Dict = {}
        for i, r in enumerate(rows_in):
            params[f"tag{i}"] = r["tag"]
            params[f"device_id{i}"] = r["device_id"]
            params[f"wf{i}"] = r["wf"]
            params[f"wt{i}"] = r["wt"]
        hour_clause = ""
        if hours and period_type != "daily":
            params["hours"] = list(hours)
            # CAST: EXTRACT returns numeric, and numeric = ANY(int[]) has no
            # operator — Postgres would reject the comparison outright.
            hour_clause = (
                f"AND CAST(EXTRACT(HOUR FROM a.{stamp_col}) AS int) = ANY(:hours) "
            )
        rows = (await self.session.execute(
            text(
                f"WITH w (tag, device_id, wf, wt) AS (VALUES {values}) "
                f"SELECT w.tag, a.device_id, a.{stamp_col} AS stamp, "
                f"a.dvst_alwrk, a.dvwrk_alwrk, a.press, a.temper, a.press_unit "
                f"FROM w JOIN {table} a ON a.device_id = w.device_id "
                f"AND a.{stamp_col} >= w.wf AND a.{stamp_col} <= w.wt "
                f"{hour_clause}"
            ),
            params,
        )).mappings().all()
        return [dict(r) for r in rows]

    async def touch_windows(self, period_type: str, windows: List[Dict]) -> None:
        """Mark the rows a window read as read today (retention input).

        Devices that share a span are marked together. A report over a branch
        asks the same month of every device, so this is one statement instead
        of one per device — five hundred round trips, each re-scanning its
        slice of the archive, for a bookkeeping column.
        """
        if not windows:
            return
        by_span: Dict[tuple, List[int]] = {}
        by_device: Dict[int, List[Dict]] = {}
        for w in windows:
            by_device.setdefault(w["device_id"], []).append(w)
        for device_id, group in by_device.items():
            span = (
                min(w["win_from"] for w in group),
                max(w["win_to"] for w in group),
            )
            by_span.setdefault(span, []).append(device_id)
        for (span_from, span_to), device_ids in by_span.items():
            await self.touch_accessed(
                device_ids, period_type, span_from, span_to
            )

    async def upsert_records(self, period_type: str, rows: List[Dict]) -> None:
        """Bulk insert/update archive rows.

        `rows`: dicts with device_id, stamp (datetime; date part is used
        for daily), dvst_alwrk, dvwrk_alwrk, press, temper, press_unit —
        unique per (device_id, stamp) within one call. COPY into a temp
        table + one INSERT ... ON CONFLICT DO UPDATE (plain columns, no JSONB
        merging), ~20x faster than multi-VALUES on full-month refreshes."""
        if not rows:
            return
        table, stamp_col = _table(period_type)
        today = date.today()
        cols = ["device_id", stamp_col, *_VALUE_COLS, "accessed_at"]
        col_list = ", ".join(cols)
        tmp = f"_tmp_{table}"
        await self.session.execute(text(
            f"CREATE TEMP TABLE {tmp} AS SELECT {col_list} FROM {table} WHERE FALSE"
        ))
        sa_conn = await self.session.connection()
        raw = await sa_conn.get_raw_connection()
        records = [
            (
                r["device_id"],
                r["stamp"].date() if period_type == "daily" else r["stamp"],
                r.get("dvst_alwrk"), r.get("dvwrk_alwrk"),
                r.get("press"), r.get("temper"), r.get("press_unit"),
                today,
            )
            for r in rows
        ]
        await raw.driver_connection.copy_records_to_table(
            tmp, records=records, columns=cols
        )
        set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in _VALUE_COLS)
        constraint = (
            "uq_dpd_daily_dev_day" if period_type == "daily"
            else "uq_dpd_hourly_dev_stamp"
        )
        await self.session.execute(text(
            f"INSERT INTO {table} ({col_list}) SELECT {col_list} FROM {tmp} "
            f"ON CONFLICT ON CONSTRAINT {constraint} DO UPDATE SET {set_clause}"
        ))
        await self.session.execute(text(f"DROP TABLE {tmp}"))

    async def touch_accessed(
        self,
        device_ids: List[int],
        period_type: str,
        range_from: datetime,
        range_to: datetime,
    ) -> None:
        """Mark rows as read today (at most one write per row per day)."""
        if not device_ids:
            return
        table, stamp_col = _table(period_type)
        params: Dict = {"ids": device_ids, "today": date.today()}
        if period_type == "daily":
            params["from"], params["to"] = range_from.date(), range_to.date()
        else:
            params["from"], params["to"] = range_from, range_to
        await self.session.execute(
            text(
                f"UPDATE {table} SET accessed_at = :today "
                f"WHERE device_id = ANY(:ids) "
                f"AND {stamp_col} >= :from AND {stamp_col} <= :to "
                f"AND accessed_at < :today"
            ),
            params,
        )

    async def prune(self, period_type: str) -> int:
        """Retention: delete records older than a year (by record date) that
        nobody read for 7 days, then RAISE coverage.loaded_from of affected
        devices to the horizon so the pruned range is backfillable again."""
        table, stamp_col = _table(period_type)
        cutoff = date.today() - timedelta(days=RETENTION_DAYS)
        access_cutoff = date.today() - timedelta(days=RETENTION_ACCESS_GRACE_DAYS)
        result = await self.session.execute(
            text(
                f"DELETE FROM {table} "
                f"WHERE {stamp_col} < :cutoff AND accessed_at < :access_cutoff "
                f"RETURNING device_id"
            ),
            {"cutoff": cutoff, "access_cutoff": access_cutoff},
        )
        pruned_ids = {row[0] for row in result}
        if pruned_ids:
            await self.session.execute(
                text(
                    "UPDATE dpd_device_coverage SET loaded_from = :cutoff "
                    "WHERE period_type = :pt AND device_id = ANY(:ids) "
                    "AND loaded_from < :cutoff"
                ),
                {"cutoff": cutoff, "pt": period_type, "ids": list(pruned_ids)},
            )
        return len(pruned_ids)

    async def get_coverage(
        self, device_ids: List[int], period_type: str
    ) -> Dict[int, date]:
        """device_id -> loaded_from. Devices absent from the result were
        never fetched at all."""
        if not device_ids:
            return {}
        rows = await self.session.execute(
            text(
                "SELECT device_id, loaded_from FROM dpd_device_coverage "
                "WHERE period_type = :pt AND device_id = ANY(:ids)"
            ),
            {"pt": period_type, "ids": device_ids},
        )
        return {r[0]: r[1] for r in rows}

    async def lower_loaded_from(
        self, device_ids: List[int], period_type: str, loaded_from: date
    ) -> None:
        """Record that [loaded_from, ...] has now been requested from DPD for
        these devices (insert or lower, never raise)."""
        if not device_ids:
            return
        await self.session.execute(
            text(
                "INSERT INTO dpd_device_coverage "
                "(device_id, period_type, loaded_from) "
                "SELECT unnest(CAST(:ids AS bigint[])), :pt, :lf "
                "ON CONFLICT (device_id, period_type) "
                "DO UPDATE SET loaded_from = LEAST("
                "dpd_device_coverage.loaded_from, EXCLUDED.loaded_from)"
            ),
            {"ids": device_ids, "pt": period_type, "lf": loaded_from},
        )

    async def clear_all(self) -> None:
        """Admin wipe: both archives + coverage (next scheduler run reloads)."""
        for table in ("dpd_daily_archive", "dpd_hourly_archive",
                      "dpd_device_coverage"):
            await self.session.execute(text(f"DELETE FROM {table}"))
