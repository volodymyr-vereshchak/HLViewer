"""DAO for the scheduler-fed DPD archive (daily/hourly tables + coverage).

All methods flush but never commit — the caller owns the transaction."""
import logging
from datetime import date, datetime
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

# Retention was removed 07.09.2026. These tables stopped being a cache when
# the GSM poll started writing into them: a pruned row used to be re-fetchable
# from the DPD API, but a row a modem brought has no second source — the device
# itself keeps weeks, not years. Measured cost of keeping everything: ~292 B per
# hourly record, about 1.25 GB a year at full coverage. Cheaper than a job that
# deletes what cannot be recovered.
#
# `accessed_at` and `touch_accessed` went with it: they existed only to feed
# the prune, and they cost a write on every read.

# Which source a row came from. A DPD refresh must never overwrite a row the
# modem brought; a GSM poll overwrites anything, because it asked the device.
SOURCE_DPD = "dpd"
SOURCE_GSM = "gsm"

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

    async def upsert_records(
        self, period_type: str, rows: List[Dict], source: str = SOURCE_DPD
    ) -> Dict[str, int]:
        """Bulk insert/update archive rows.

        `rows`: dicts with device_id, stamp (datetime; date part is used
        for daily), dvst_alwrk, dvwrk_alwrk, press, temper, press_unit —
        unique per (device_id, stamp) within one call. COPY into a temp
        table + one INSERT ... ON CONFLICT DO UPDATE (plain columns, no JSONB
        merging), ~20x faster than multi-VALUES on full-month refreshes.

        `source` decides who wins a collision. UNIQUE(device_id, stamp) means
        an hour has room for one row, so "the GSM poll is the truth" has to be
        an UPSERT rule: without it the nightly DPD refresh would silently
        overwrite what the modem read off the device.

        Returns how many rows were new and how many were rewritten. A poll
        that fetched a month and added two hours has done something quite
        different from one that added seven hundred, and "fetched 720" says
        neither."""
        if not rows:
            return {"inserted": 0, "updated": 0}
        table, stamp_col = _table(period_type)
        cols = ["device_id", stamp_col, *_VALUE_COLS, "source"]
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
                source,
            )
            for r in rows
        ]
        await raw.driver_connection.copy_records_to_table(
            tmp, records=records, columns=cols
        )
        # The unit is the one column a writer may legitimately not know: a
        # modem reads the number a corrector stores and, for most families,
        # nothing that says what it is in. Overwriting a unit ДПД had reported
        # with that silence leaves the row unreadable — the archive says 0.15
        # and the column header falls back to кгс/см², a factor of ten out.
        set_clause = ", ".join(
            f"{c} = COALESCE(EXCLUDED.{c}, {table}.{c})" if c == "press_unit"
            else f"{c} = EXCLUDED.{c}"
            for c in (*_VALUE_COLS, "source")
        )
        constraint = (
            "uq_dpd_daily_dev_day" if period_type == "daily"
            else "uq_dpd_hourly_dev_stamp"
        )
        # xmax = 0 marks a row this statement inserted rather than updated —
        # the only way to tell "new data" from "the same data again" without
        # reading the table first.
        written = (await self.session.execute(text(
            f"INSERT INTO {table} ({col_list}) SELECT {col_list} FROM {tmp} "
            f"ON CONFLICT ON CONSTRAINT {constraint} DO UPDATE SET {set_clause} "
            f"WHERE {table}.source <> '{SOURCE_GSM}' "
            f"OR EXCLUDED.source = '{SOURCE_GSM}' "
            f"RETURNING (xmax = 0) AS inserted"
        ))).scalars().all()
        await self.session.execute(text(f"DROP TABLE {tmp}"))
        inserted = sum(1 for new in written if new)
        return {"inserted": inserted, "updated": len(written) - inserted}

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
