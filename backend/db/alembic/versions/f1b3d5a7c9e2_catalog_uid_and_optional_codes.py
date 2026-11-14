"""Catalog entries get a cross-installation identity, and may have no ДПД code

Two changes to `manufacturer` and `corector_type`, made together because the
second does not work without the first.

1. `export_uid`. The catalog travels between installations as
   device_catalog.json, and correctors travel in config bundles as a reference
   to a catalog entry. Until now a manufacturer was matched by `mf_dev` and a
   model by its name, so a model renamed on one server arrived on another as a
   second model. The uid is the identity instead.

   Existing rows get a uid DERIVED from what identified them so far — the
   manufacturer's code, and the manufacturer's code plus the model name — so
   that every installation mints the same uid for the same entry without
   having to talk to the others. Rows added later get a random one.

2. `mf_dev` and `type_dev` become nullable. Изодром flow computers are read
   over a modem only: ДПД has neither the manufacturer nor its models, so
   there is no code to store — and nothing but the uid to tell them by.

Revision ID: f1b3d5a7c9e2
Revises: e7a9c3d1f4b2
Create Date: 2026-11-14
"""
import uuid

import sqlalchemy as sa
from alembic import op

revision = "f1b3d5a7c9e2"
down_revision = "e7a9c3d1f4b2"
branch_labels = None
depends_on = None

# Frozen: changing it would give the same entry a different uid on a server
# migrated later. device_catalog.json was written with these very values.
NAMESPACE = uuid.UUID("6f1d2c3a-9b4e-5a70-8c1d-2e3f4a5b6c7d")


def upgrade() -> None:
    bind = op.get_bind()
    for table in ("manufacturer", "corector_type"):
        op.add_column(table, sa.Column("export_uid", sa.Uuid(), nullable=True))

    manufacturers = bind.execute(
        sa.text("SELECT id, mf_dev FROM manufacturer")).fetchall()
    code_of = {}
    for row_id, mf_dev in manufacturers:
        code_of[row_id] = mf_dev
        bind.execute(
            sa.text("UPDATE manufacturer SET export_uid = :uid WHERE id = :id"),
            {"uid": uuid.uuid5(NAMESPACE, f"manufacturer:{mf_dev}"), "id": row_id},
        )
    for row_id, manufacturer_id, model_name in bind.execute(
            sa.text("SELECT id, manufacturer_id, model_name FROM corector_type")).fetchall():
        key = f"model:{code_of.get(manufacturer_id)}:{model_name}"
        bind.execute(
            sa.text("UPDATE corector_type SET export_uid = :uid WHERE id = :id"),
            {"uid": uuid.uuid5(NAMESPACE, key), "id": row_id},
        )

    for table in ("manufacturer", "corector_type"):
        # The default is for rows inserted past the models; gen_random_uuid()
        # is built into Postgres 13+.
        op.alter_column(table, "export_uid", existing_type=sa.Uuid(), nullable=False,
                        server_default=sa.text("gen_random_uuid()"))
        op.create_unique_constraint(f"uq_{table}_export_uid", table, ["export_uid"])

    # The unique constraint on mf_dev stays — Postgres lets any number of
    # NULLs through it.
    op.alter_column("manufacturer", "mf_dev", existing_type=sa.Integer(), nullable=True)
    op.alter_column("corector_type", "type_dev", existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    # Rows without a code cannot go back under NOT NULL. They are given codes
    # no real device has, below zero, rather than deleted: correctors may
    # already point at these models, and the reference is RESTRICT.
    op.execute("UPDATE manufacturer SET mf_dev = -id WHERE mf_dev IS NULL")
    op.execute("UPDATE corector_type SET type_dev = -id WHERE type_dev IS NULL")
    op.alter_column("corector_type", "type_dev", existing_type=sa.Integer(), nullable=False)
    op.alter_column("manufacturer", "mf_dev", existing_type=sa.Integer(), nullable=False)
    for table in ("corector_type", "manufacturer"):
        op.drop_constraint(f"uq_{table}_export_uid", table, type_="unique")
        op.drop_column(table, "export_uid")
