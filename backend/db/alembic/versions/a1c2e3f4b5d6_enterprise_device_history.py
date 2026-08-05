"""Enterprise device history: split the metering point from the corrector.

An `enterprise` row used to BE its corrector — name plus (ser_num,
corector_type_id, ch_num), with that triple as the table's unique key. A
corrector could therefore never be replaced without rewriting the point's
whole past, nor moved to another point at all (it would collide on the same
key).

This splits the two:

  dpd_device        one corrector channel, the unit DPD is addressed by
  enterprise        the metering point (name, branch, line, flags)
  enterprise_device "device D stood at point E from … to …"

and re-keys the archive from the point to the DEVICE, so a corrector keeps
one continuous archive wherever it stood and a point reads slices of it
through its windows. Moving a corrector then costs no re-poll.

The archive is NOT rewritten: today's mapping is one-to-one, so dpd_device
takes its ids straight from enterprise.id and only the column name and the
foreign key change. That keeps ~220k daily and up to ~5M hourly rows exactly
where they are.

Revision ID: a1c2e3f4b5d6
Revises: f0a1b2c3d4e5
Create Date: 2026-08-05
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1c2e3f4b5d6"
down_revision: Union[str, None] = "f0a1b2c3d4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The history of a point that has always had one device starts before any
# archive does, so every existing row keeps its point.
EPOCH = "2000-01-01 00:00:00"


def upgrade() -> None:
    # ── 1. Corrector registry, seeded 1:1 out of enterprise ──────────────────
    # The unique constraint repeats uq_enterprise_device_ct exactly, NULL
    # looseness included: anything the old table allowed must seed without
    # collapsing two rows into one, or the archive ids would stop matching.
    op.create_table(
        "dpd_device",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("ser_num", sa.BigInteger(), nullable=False),
        sa.Column("corector_type_id", sa.BigInteger(), nullable=True),
        sa.Column("mf_dev", sa.Integer(), nullable=True),
        sa.Column("type_dev", sa.Integer(), nullable=True),
        sa.Column("ch_num", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["corector_type_id"], ["corector_type.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "ser_num", "corector_type_id", "ch_num", name="uq_dpd_device_ident"
        ),
    )
    op.create_index("ix_dpd_device_ser", "dpd_device", ["ser_num"])

    op.execute(
        sa.text(
            "INSERT INTO dpd_device "
            "(id, ser_num, corector_type_id, mf_dev, type_dev, ch_num) "
            "SELECT id, ser_num, corector_type_id, mf_dev, type_dev, ch_num "
            "FROM enterprise"
        )
    )
    # is_called=false so the next insert takes max(id)+1 even on an empty table.
    op.execute(
        sa.text(
            "SELECT setval(pg_get_serial_sequence('dpd_device', 'id'), "
            "COALESCE((SELECT max(id) FROM dpd_device), 1), "
            "(SELECT count(*) > 0 FROM dpd_device))"
        )
    )

    # ── 2. Assignment history ────────────────────────────────────────────────
    op.create_table(
        "enterprise_device",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False),
        sa.Column("device_id", sa.BigInteger(), nullable=False),
        sa.Column("installed_from", sa.DateTime(), nullable=False),
        sa.Column("removed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprise.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["device_id"], ["dpd_device.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "enterprise_id", "installed_from", name="uq_enterprise_device_from"
        ),
    )
    op.create_index("ix_enterprise_device_ent", "enterprise_device", ["enterprise_id"])
    op.create_index(
        "ix_enterprise_device_dev", "enterprise_device", ["device_id", "installed_from"]
    )

    op.execute(
        sa.text(
            "INSERT INTO enterprise_device "
            "(enterprise_id, device_id, installed_from, removed_at) "
            f"SELECT id, id, TIMESTAMP '{EPOCH}', NULL FROM enterprise"
        )
    )

    # ── 3. Archive: enterprise_id → device_id, values untouched ──────────────
    for table, uq_old, uq_new, stamp_col in (
        ("dpd_daily_archive", "uq_dpd_daily_ent_day", "uq_dpd_daily_dev_day", "day"),
        ("dpd_hourly_archive", "uq_dpd_hourly_ent_stamp",
         "uq_dpd_hourly_dev_stamp", "stamp"),
    ):
        op.alter_column(table, "enterprise_id", new_column_name="device_id")
        op.drop_constraint(uq_old, table, type_="unique")
        op.create_unique_constraint(uq_new, table, ["device_id", stamp_col])
        op.drop_constraint(f"{table}_enterprise_id_fkey", table, type_="foreignkey")
        op.create_foreign_key(
            f"{table}_device_id_fkey", table, "dpd_device",
            ["device_id"], ["id"], ondelete="CASCADE",
        )

    op.alter_column("dpd_device_coverage", "enterprise_id", new_column_name="device_id")
    op.drop_constraint(
        "dpd_device_coverage_enterprise_id_fkey", "dpd_device_coverage",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "dpd_device_coverage_device_id_fkey", "dpd_device_coverage", "dpd_device",
        ["device_id"], ["id"], ondelete="CASCADE",
    )

    # ── 4. The point stops carrying a device ─────────────────────────────────
    op.drop_constraint("uq_enterprise_device_ct", "enterprise", type_="unique")
    op.drop_constraint("fk_enterprise_corector_type", "enterprise", type_="foreignkey")
    op.drop_index("idx_enterprise_corector_type", table_name="enterprise")
    for column in ("ser_num", "corector_type_id", "mf_dev", "type_dev", "ch_num"):
        op.drop_column("enterprise", column)


def downgrade() -> None:
    # Restores the device columns from the LAST history entry. Replacements
    # entered after the upgrade cannot survive a schema that has room for only
    # one device per point — they are lost here by construction.
    op.add_column("enterprise", sa.Column("ser_num", sa.BigInteger(), nullable=True))
    op.add_column("enterprise", sa.Column("corector_type_id", sa.BigInteger(), nullable=True))
    op.add_column("enterprise", sa.Column("mf_dev", sa.Integer(), nullable=True))
    op.add_column("enterprise", sa.Column("type_dev", sa.Integer(), nullable=True))
    op.add_column("enterprise", sa.Column("ch_num", sa.Integer(), nullable=True))

    op.execute(
        sa.text(
            """
            UPDATE enterprise e
            SET ser_num = d.ser_num,
                corector_type_id = d.corector_type_id,
                mf_dev = d.mf_dev,
                type_dev = d.type_dev,
                ch_num = d.ch_num
            FROM (
                SELECT DISTINCT ON (ed.enterprise_id)
                       ed.enterprise_id, ed.device_id
                FROM enterprise_device ed
                ORDER BY ed.enterprise_id, ed.installed_from DESC
            ) last
            JOIN dpd_device d ON d.id = last.device_id
            WHERE e.id = last.enterprise_id
            """
        )
    )
    # A point with no history at all cannot be represented by the old schema;
    # ser_num was NOT NULL there, so give it the sentinel 0 rather than fail.
    op.execute(sa.text("UPDATE enterprise SET ser_num = 0 WHERE ser_num IS NULL"))
    op.execute(sa.text("UPDATE enterprise SET ch_num = 0 WHERE ch_num IS NULL"))
    op.alter_column("enterprise", "ser_num", nullable=False)
    op.alter_column("enterprise", "ch_num", nullable=False)

    op.create_index("idx_enterprise_corector_type", "enterprise", ["corector_type_id"])
    op.create_foreign_key(
        "fk_enterprise_corector_type", "enterprise", "corector_type",
        ["corector_type_id"], ["id"], ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_enterprise_device_ct", "enterprise",
        ["ser_num", "corector_type_id", "ch_num"],
    )

    op.drop_constraint(
        "dpd_device_coverage_device_id_fkey", "dpd_device_coverage",
        type_="foreignkey",
    )
    op.alter_column("dpd_device_coverage", "device_id", new_column_name="enterprise_id")
    op.create_foreign_key(
        "dpd_device_coverage_enterprise_id_fkey", "dpd_device_coverage", "enterprise",
        ["enterprise_id"], ["id"], ondelete="CASCADE",
    )

    for table, uq_old, uq_new, stamp_col in (
        ("dpd_daily_archive", "uq_dpd_daily_ent_day", "uq_dpd_daily_dev_day", "day"),
        ("dpd_hourly_archive", "uq_dpd_hourly_ent_stamp",
         "uq_dpd_hourly_dev_stamp", "stamp"),
    ):
        op.drop_constraint(f"{table}_device_id_fkey", table, type_="foreignkey")
        op.drop_constraint(uq_new, table, type_="unique")
        op.alter_column(table, "device_id", new_column_name="enterprise_id")
        op.create_unique_constraint(uq_old, table, ["enterprise_id", stamp_col])
        op.create_foreign_key(
            f"{table}_enterprise_id_fkey", table, "enterprise",
            ["enterprise_id"], ["id"], ondelete="CASCADE",
        )

    op.drop_index("ix_enterprise_device_dev", table_name="enterprise_device")
    op.drop_index("ix_enterprise_device_ent", table_name="enterprise_device")
    op.drop_table("enterprise_device")
    op.drop_index("ix_dpd_device_ser", table_name="dpd_device")
    op.drop_table("dpd_device")
