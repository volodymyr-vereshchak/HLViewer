"""Key daily_archive and hourly_archive by (line_id, period) alone

Both tables were unique on (line_id, period, volume). The source re-exports the
same records with the last float32 bit of volume changed (every overlapping
record in two Zaporizgaz exports of 07.10 and 22.10.2026 differed by 1 ULP),
so each export added a second row for the same hour: 270 daily and 7512
hourly duplicate groups on prod by 22.10.2026, all noise. The same key also
dropped a genuine record whenever two of them happened to share a volume.

Before the new key can go on, every (line_id, period) group is reduced to one
row, once:

* the repeated hour of the autumn clock change (hourly, last Sunday of
  October, 03:00) is two real hours from meters that write it as two records —
  27.10.2024 «Побут 1» and «ВП ДЕЛЬТА». Rows with no volume are dropped,
  volumes summed, pressure/temperature/density averaged, exactly as the
  importer now folds such a pair (hl_engine/archive_merge.py). Copies of the
  same record from another export (volume within 1e-5) are dropped first so
  that noise is never summed;
* every other group keeps its newest row with non-zero volume (or the newest
  row when all are zero): the newer export wins, as it does on import now.

The delete is not undone by downgrade; take a dump of both tables first.

Revision ID: 0a148d391231
Revises: d1f4a8c6b093
Create Date: 2026-10-22
"""
import logging

import sqlalchemy as sa
from alembic import op

revision = "0a148d391231"
down_revision = "d1f4a8c6b093"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

TABLES = {
    # table: (constraint name, fold the clock-change hour)
    "daily_archive": ("day_calc_id_line_period_constraint", False),
    "hourly_archive": ("hour_calc_id_line_period_constraint", True),
}

ZERO_VOLUME = 1e-6
NO_DATA = 1e-3
SAME_RECORD = 1e-5



def clock_back_hour(column: str) -> str:
    """03:00 on the last Sunday of October: the hour Europe/Kyiv lives twice."""
    return (
        f"EXTRACT(month FROM {column}) = 10 AND EXTRACT(dow FROM {column}) = 0 "
        f"AND EXTRACT(day FROM {column}) >= 25 AND EXTRACT(hour FROM {column}) = 3"
    )


def dedupe_statements(table: str, fold_clock_back: bool) -> list[str]:
    """SQL that leaves one row per (line_id, period) in `table`.

    Kept as plain statements so the test suite can run the very same SQL
    against a table it filled with duplicates.
    """
    statements = [
        "DROP TABLE IF EXISTS _dup_groups",
        f"CREATE TEMP TABLE _dup_groups AS "
        f"SELECT line_id, period FROM {table} "
        f"GROUP BY line_id, period HAVING count(*) > 1",
    ]
    if fold_clock_back:
        statements += [
            # Another export's copy of the same record is not a second hour.
            f"DELETE FROM {table} a USING _dup_groups g "
            f"WHERE a.line_id = g.line_id AND a.period = g.period "
            f"AND {clock_back_hour('g.period')} "
            f"AND EXISTS (SELECT 1 FROM {table} b "
            f"WHERE b.line_id = a.line_id AND b.period = a.period "
            f"AND b.id > a.id AND abs(b.volume - a.volume) "
            f"<= {SAME_RECORD} * greatest(abs(a.volume), abs(b.volume)))",
            "DROP TABLE IF EXISTS _folded",
            f"""CREATE TEMP TABLE _folded AS
            WITH grp AS (
                SELECT a.*,
                       bool_or(abs(a.volume) > {ZERO_VOLUME})
                           OVER w AS any_flow,
                       bool_or(abs(a.pressure) >= {NO_DATA}
                               AND abs(a.temperature) >= {NO_DATA})
                           OVER w AS any_measured,
                       max(a.id) OVER w AS last_id
                FROM {table} a
                JOIN _dup_groups g
                  ON a.line_id = g.line_id AND a.period = g.period
                WHERE {clock_back_hour('g.period')}
                WINDOW w AS (PARTITION BY a.line_id, a.period)
            ), kept AS (
                SELECT * FROM grp WHERE CASE
                    WHEN any_flow THEN abs(volume) > {ZERO_VOLUME}
                    WHEN any_measured THEN abs(pressure) >= {NO_DATA}
                                       AND abs(temperature) >= {NO_DATA}
                    ELSE id = last_id END
            )
            SELECT line_id, period, max(id) AS keep_id,
                   sum(volume) AS volume, sum(w_volume_dp) AS w_volume_dp,
                   avg(pressure) AS pressure, avg(temperature) AS temperature,
                   avg(density) AS density
            FROM kept GROUP BY line_id, period""",
            f"UPDATE {table} a SET volume = f.volume, "
            f"w_volume_dp = f.w_volume_dp, pressure = f.pressure, "
            f"temperature = f.temperature, density = f.density, "
            f"updated_at = NOW() "
            f"FROM _folded f WHERE a.id = f.keep_id",
            f"DELETE FROM {table} a USING _folded f "
            f"WHERE a.line_id = f.line_id AND a.period = f.period "
            f"AND a.id <> f.keep_id",
        ]
    statements += [
        "DROP TABLE IF EXISTS _kept",
        f"CREATE TEMP TABLE _kept AS "
        f"SELECT a.line_id, a.period, "
        f"(array_agg(a.id ORDER BY abs(a.volume) > {ZERO_VOLUME} DESC, "
        f"a.id DESC))[1] AS keep_id "
        f"FROM {table} a JOIN _dup_groups g "
        f"ON a.line_id = g.line_id AND a.period = g.period "
        f"GROUP BY a.line_id, a.period",
        f"DELETE FROM {table} a USING _kept k "
        f"WHERE a.line_id = k.line_id AND a.period = k.period "
        f"AND a.id <> k.keep_id",
        "DROP TABLE IF EXISTS _dup_groups",
        "DROP TABLE IF EXISTS _folded",
        "DROP TABLE IF EXISTS _kept",
    ]
    return statements


def upgrade() -> None:
    bind = op.get_bind()
    for table, (constraint, fold_clock_back) in TABLES.items():
        removed = 0
        for statement in dedupe_statements(table, fold_clock_back):
            result = bind.execute(sa.text(statement))
            if statement.lstrip().startswith("DELETE"):
                removed += result.rowcount
        logger.info("%s: %d duplicate rows removed", table, removed)
        op.drop_constraint(constraint, table, type_="unique")
        op.create_unique_constraint(constraint, table, ["line_id", "period"])


def downgrade() -> None:
    for table, (constraint, _) in TABLES.items():
        op.drop_constraint(constraint, table, type_="unique")
        op.create_unique_constraint(
            constraint, table, ["line_id", "period", "volume"]
        )
