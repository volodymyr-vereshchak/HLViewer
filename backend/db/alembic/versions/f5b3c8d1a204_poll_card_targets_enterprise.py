"""Point poll cards at the enterprise instead of the corrector

The modem stands at the site and stays there; the corrector under it gets
replaced. Binding the card to the corrector meant every replacement was a
thing somebody had to remember to repoint, and forgetting it does not fail
loudly — the card keeps dialling the same phone and the readings arrive under
the serial of a meter that is no longer there.

So the card names the enterprise, and which corrector to read is resolved at
poll time from the installation history. That also gives the two refusals the
operators asked for: nothing fitted means "немає встановлених корректорів",
and a corrector that answers with another serial stops the session.

Existing cards are moved to the enterprise their corrector stands at. One that
stands nowhere — a corrector already taken off — has no enterprise to move to
and is left as it is, because deleting somebody's settings to satisfy a
migration is not a trade this should make on its own.

Revision ID: f5b3c8d1a204
Revises: e2a4c6b8d0f3
"""
import sqlalchemy as sa
from alembic import op

revision = "f5b3c8d1a204"
down_revision = "e2a4c6b8d0f3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "poll_device",
        sa.Column("enterprise_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_poll_device_enterprise", "poll_device", "enterprise",
        ["enterprise_id"], ["id"], ondelete="CASCADE",
    )
    op.create_index(
        "uq_poll_device_enterprise", "poll_device", ["enterprise_id"],
        unique=True, postgresql_where=sa.text("enterprise_id IS NOT NULL"),
    )

    # The old rule — exactly one of the two device columns — forbids the very
    # state the migration is moving rows into, so it goes before the data and
    # its replacement comes after.
    op.drop_constraint("ck_poll_device_single_target", "poll_device", type_="check")

    # Move what exists. The corrector's current point is the enterprise whose
    # history still has it fitted; where a corrector was moved between points,
    # the latest installation is the one that counts.
    op.execute("""
        UPDATE poll_device AS p
           SET enterprise_id = latest.enterprise_id,
               dpd_device_id = NULL
          FROM (
                SELECT DISTINCT ON (ed.device_id)
                       ed.device_id, ed.enterprise_id
                  FROM enterprise_device ed
                 WHERE ed.removed_at IS NULL
                 ORDER BY ed.device_id, ed.installed_from DESC
               ) AS latest
         WHERE p.dpd_device_id = latest.device_id
           AND NOT EXISTS (
                 SELECT 1 FROM poll_device other
                  WHERE other.enterprise_id = latest.enterprise_id
               )
    """)

    op.create_check_constraint(
        "ck_poll_device_single_target", "poll_device",
        "(dpd_device_id IS NOT NULL)::int"
        " + (dpd_line_id IS NOT NULL)::int"
        " + (enterprise_id IS NOT NULL)::int = 1",
    )


def downgrade() -> None:
    op.drop_constraint("ck_poll_device_single_target", "poll_device", type_="check")

    # Put each enterprise card back on whatever corrector is fitted there, so
    # the old constraint can hold again.
    op.execute("""
        UPDATE poll_device AS p
           SET dpd_device_id = latest.device_id,
               enterprise_id = NULL
          FROM (
                SELECT DISTINCT ON (ed.enterprise_id)
                       ed.enterprise_id, ed.device_id
                  FROM enterprise_device ed
                 WHERE ed.removed_at IS NULL
                 ORDER BY ed.enterprise_id, ed.installed_from DESC
               ) AS latest
         WHERE p.enterprise_id = latest.enterprise_id
    """)
    op.execute("DELETE FROM poll_device WHERE enterprise_id IS NOT NULL")

    op.create_check_constraint(
        "ck_poll_device_single_target", "poll_device",
        "(dpd_device_id IS NOT NULL) <> (dpd_line_id IS NOT NULL)",
    )
    op.drop_index("uq_poll_device_enterprise", table_name="poll_device")
    op.drop_constraint("fk_poll_device_enterprise", "poll_device", type_="foreignkey")
    op.drop_column("poll_device", "enterprise_id")
