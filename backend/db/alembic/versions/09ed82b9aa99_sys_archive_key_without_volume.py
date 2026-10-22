"""Key sys_archive by (line_id, period, sys_type_id), not by volume

The same change 0a148d391231 made for daily and hourly rows. An event is
identified by its line, its moment (to the second) and its type; the value it
carries is not part of what it is. With volume in the key every re-export
whose value moved in the last float32 bit added the event again: 91 723
duplicate groups in October 2026 and 23 in September on prod (22.10.2026),
all of them noise (relative difference < 1e-6). Nothing else on prod and
nothing in the Zaporizgaz exports has two different values for one
(line, moment, type): the only same-moment records inside a file are exact
copies of each other.

Besides the noise, prod holds 19 pairs from March–April 2025 where one copy
carries the value and the other the same float32's bits read as an integer
(8.274288 next to 1090807000) — old hostlib files encode event values that
way. Of such a pair the decoded value is kept, whichever row is newer;
everywhere else the newest row stays.

Deleted rows are not restored by downgrade.

Revision ID: 09ed82b9aa99
Revises: 0a148d391231
Create Date: 2026-10-22
"""
import logging

import sqlalchemy as sa
from alembic import op

revision = "09ed82b9aa99"
down_revision = "0a148d391231"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

CONSTRAINT = "sys_all_constraint"

#: A float32's bits read as an integer: a whole number far beyond any event
#: value the archive holds (none above 1e8 since May 2025).
RAW_BITS = "(abs(a.volume) > 1e8 AND a.volume = trunc(a.volume))"

STATEMENTS = [
    "DROP TABLE IF EXISTS _sys_kept",
    f"""CREATE TEMP TABLE _sys_kept AS
    SELECT a.line_id, a.period, a.sys_type_id,
           (array_agg(a.id ORDER BY {RAW_BITS} ASC, a.id DESC))[1] AS keep_id
      FROM sys_archive a
      JOIN (SELECT line_id, period, sys_type_id FROM sys_archive
             GROUP BY line_id, period, sys_type_id HAVING count(*) > 1) g
        ON a.line_id = g.line_id AND a.period = g.period
       AND a.sys_type_id = g.sys_type_id
     GROUP BY a.line_id, a.period, a.sys_type_id""",
    "DELETE FROM sys_archive a USING _sys_kept k "
    "WHERE a.line_id = k.line_id AND a.period = k.period "
    "AND a.sys_type_id = k.sys_type_id AND a.id <> k.keep_id",
    "DROP TABLE IF EXISTS _sys_kept",
]


def upgrade() -> None:
    bind = op.get_bind()
    removed = 0
    for statement in STATEMENTS:
        result = bind.execute(sa.text(statement))
        if statement.startswith("DELETE"):
            removed = result.rowcount
    logger.info("sys_archive: %d duplicate rows removed", removed)
    op.drop_constraint(CONSTRAINT, "sys_archive", type_="unique")
    op.create_unique_constraint(
        CONSTRAINT, "sys_archive", ["line_id", "period", "sys_type_id"]
    )


def downgrade() -> None:
    op.drop_constraint(CONSTRAINT, "sys_archive", type_="unique")
    op.create_unique_constraint(
        CONSTRAINT, "sys_archive", ["period", "sys_type_id", "line_id", "volume"]
    )
