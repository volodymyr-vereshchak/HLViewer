"""Poll schedule as a cron expression

The hours were a list — ["08:00", "20:00"] — which says "twice a day" well and
"every hour" badly: that one meant twenty-four entries, and "every four hours"
meant counting them out. One cron expression says both, and says the things a
list cannot say at all (weekdays only, the first of the month).

The lists that exist convert exactly, because the fleet's schedules are whole
hours: ["08:00", "20:00"] becomes "0 8,20 * * *". Nothing is left behind, so
the old columns go with the same migration rather than lingering as a second
place the answer could come from.

Revision ID: e5b7c1a93d24
Revises: d8e4b1f07a92
Create Date: 2026-09-20
"""
from alembic import op
import sqlalchemy as sa


revision = "e5b7c1a93d24"
down_revision = "d8e4b1f07a92"
branch_labels = None
depends_on = None


#: ["08:00","20:00"] -> "0 8,20 * * *", in SQL because that is where the data
#: is. jsonb_array_elements_text gives one row per slot; the two halves of
#: "HH:MM" become the minute and hour fields.
CONVERT = """
    UPDATE {table} SET poll_cron = sub.cron
    FROM (
        SELECT t.id,
               string_agg(DISTINCT lpad(split_part(slot, ':', 2), 1, ' '), ',')
                 || ' '
                 || string_agg(DISTINCT ltrim(split_part(slot, ':', 1), '0'), ',')
                 || ' * * *' AS cron
        FROM {table} t,
             LATERAL jsonb_array_elements_text(t.poll_times) AS slot
        WHERE t.poll_times IS NOT NULL
          AND jsonb_array_length(t.poll_times) > 0
        GROUP BY t.id
    ) AS sub
    WHERE {table}.id = sub.id
"""


def upgrade() -> None:
    op.add_column("poll_device", sa.Column("poll_cron", sa.String(64), nullable=True))
    op.add_column(
        "poll_settings",
        sa.Column("poll_cron", sa.String(64), nullable=False,
                  server_default="0 8 * * *"),
    )

    op.execute(CONVERT.format(table="poll_device"))
    op.execute(CONVERT.format(table="poll_settings"))
    op.execute("UPDATE poll_settings SET poll_cron = '0 8 * * *' "
               "WHERE poll_cron IS NULL OR poll_cron = ''")

    op.drop_column("poll_device", "poll_times")
    op.drop_column("poll_settings", "poll_times")


def downgrade() -> None:
    op.add_column("poll_device", sa.Column("poll_times", sa.JSON(), nullable=True))
    op.add_column(
        "poll_settings",
        sa.Column("poll_times", sa.JSON(), nullable=False,
                  server_default='["08:00"]'),
    )
    # Back to a list, for the shapes a list can hold: one minute and a set of
    # hours. Anything cleverer than that has no list to go back to, and keeps
    # the default rather than silently becoming a schedule nobody asked for.
    op.execute("""
        UPDATE poll_device SET poll_times = sub.times FROM (
            SELECT id, to_jsonb(array_agg(
                       lpad(hour, 2, '0') || ':' ||
                       lpad(split_part(poll_cron, ' ', 1), 2, '0')))
                       AS times
            FROM poll_device,
                 LATERAL unnest(string_to_array(split_part(poll_cron, ' ', 2), ',')) AS hour
            WHERE poll_cron IS NOT NULL
              AND split_part(poll_cron, ' ', 1) ~ '^[0-9]+$'
              AND split_part(poll_cron, ' ', 2) ~ '^[0-9,]+$'
            GROUP BY id, poll_cron
        ) AS sub WHERE poll_device.id = sub.id
    """)
    op.drop_column("poll_device", "poll_cron")
    op.drop_column("poll_settings", "poll_cron")
