"""Set-back DPD volumes: 0, marked, and days summed from their hours

A day or hour below zero is a totaliser that was set back — a corrector
replaced or its archive cleared — measured across the jump: "new reading minus
old" is minus everything the old one counted. On prod one such day
(−1 771 792 m³) made a line's total negative, which EnterpriseVolumeResponse
refuses, and the whole enterprise report for the range failed (28.10.2026).

Adds `volume_fix` to the four DPD archive tables (NULL / 'zeroed' / 'hours',
see backend/db/dao/volume_fix.py) and a partial index over the marked days.
The negative rows already stored are set to 0 and marked; every marked day
whose commercial day is fully in the hourly archive then gets the sum of
those hours. From here on the archive DAOs do the same on every write.

Downgrade drops the column and the index; the volumes stay as they are — the
negative values are not restored.

Revision ID: 331122169b34
Revises: 09ed82b9aa99
Create Date: 2026-10-28
"""
import logging
import os

import sqlalchemy as sa
from alembic import op

revision = "331122169b34"
down_revision = "09ed82b9aa99"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

#: (daily, hourly, key, volume columns, index on the marked days)
ARCHIVES = [
    ("dpd_daily_archive", "dpd_hourly_archive", "device_id",
     ("dvst_alwrk", "dvwrk_alwrk"), "ix_dpd_daily_volume_fix"),
    ("dpd_line_daily_archive", "dpd_line_hourly_archive", "dpd_line_id",
     ("volume",), "ix_dpd_line_daily_volume_fix"),
]


def mark_statements(table: str, columns) -> list[str]:
    negative = " OR ".join(f"{c} < 0" for c in columns)
    # CASE, not GREATEST: GREATEST(NULL, 0) is 0, and no data must stay NULL.
    sets = ", ".join(f"{c} = CASE WHEN {c} < 0 THEN 0 ELSE {c} END" for c in columns)
    return [f"UPDATE {table} SET {sets}, volume_fix = 'zeroed' WHERE {negative}"]


def sum_statement(daily: str, hourly: str, key: str, columns, hour: int, tz: str) -> str:
    """The repair of backend/db/dao/volume_fix.py over every marked day.
    Written out here rather than imported: a migration must not change when
    the application code does."""
    sums = ", ".join(f"sum(h.{c}) AS {c}" for c in columns)
    sets = ", ".join(f"{c} = COALESCE(s.{c}, d.{c})" for c in columns)
    return (
        f"UPDATE {daily} d SET {sets}, volume_fix = 'hours' "
        f"FROM ("
        f" SELECT a.id, {sums}, count(*) AS n,"
        f"  (SELECT count(*) FROM generate_series("
        f"     a.day + make_interval(hours => {hour}),"
        f"     a.day + make_interval(hours => {hour + 23}),"
        f"     interval '1 hour') AS g(t)"
        f"   WHERE (g.t AT TIME ZONE '{tz}') AT TIME ZONE '{tz}' = g.t) AS expected"
        f" FROM {daily} a"
        f" JOIN {hourly} h ON h.{key} = a.{key}"
        f"  AND h.stamp >= a.day + make_interval(hours => {hour})"
        f"  AND h.stamp < a.day + make_interval(hours => {hour + 24})"
        f" WHERE a.volume_fix IS NOT NULL"
        f" GROUP BY a.id, a.day"
        f") s "
        f"WHERE d.id = s.id AND s.n >= s.expected"
    )


def upgrade() -> None:
    conn = op.get_bind()
    hour = int(os.getenv("CONTRACT_HOUR", "7"))
    tz = os.getenv("TZ", "Europe/Kyiv")
    for daily, hourly, key, columns, index in ARCHIVES:
        for table in (daily, hourly):
            op.add_column(table, sa.Column("volume_fix", sa.String(length=8), nullable=True))
            for statement in mark_statements(table, columns):
                marked = conn.exec_driver_sql(statement).rowcount
                if marked:
                    logger.info("%s: %s set-back volumes stored as 0", table, marked)
        op.create_index(index, daily, [key, "day"],
                        postgresql_where=sa.text("volume_fix IS NOT NULL"))
        summed = conn.exec_driver_sql(sum_statement(daily, hourly, key, columns, hour, tz)).rowcount
        if summed:
            logger.info("%s: %s set-back days summed from their hours", daily, summed)


def downgrade() -> None:
    for daily, hourly, _key, _columns, index in ARCHIVES:
        op.drop_index(index, table_name=daily)
        for table in (daily, hourly):
            op.drop_column(table, "volume_fix")
