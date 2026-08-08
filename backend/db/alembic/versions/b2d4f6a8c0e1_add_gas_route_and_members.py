"""Gas transport routes and their member lines.

A route groups the lines that carry the same gas, so their ФХП must agree.
Membership is exclusive — `uq_gas_route_member_line` — because a line measured
against two different references would be reconciled twice, with two answers.

`is_reference` marks the member lines fed by a stream chromatograph; they are
the route's reference. The flag sits on the membership rather than on
`gas_volume_line` because it is a decision made inside the route editor and is
meaningless for a line that belongs to no route.

Also adds `idx_edit_line_type_period` to `edit_archive`: the report reads one
line's history of one quantity over a range and probes backwards for the value
in force before it, and neither access path is served by the existing
(line_id, period) / (line_id, edit_type_id) pair. `edit_archive` is the largest
table in the system, so the index is built CONCURRENTLY.

Revision ID: b2d4f6a8c0e1
Revises: a1c2e3f4b5d6
Create Date: 2026-08-08
"""
from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2d4f6a8c0e1"
down_revision: Union[str, None] = "a1c2e3f4b5d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "gas_route",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("branch_id", sa.BigInteger(), nullable=False),
        sa.Column("number", sqlmodel.sql.sqltypes.AutoString(length=16), nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(length=255), nullable=True),
        sa.Column("description", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["branch_id"], ["grmu_branch.id"],
            name="fk_gas_route_branch", ondelete="CASCADE",
        ),
        sa.UniqueConstraint("branch_id", "number", name="uq_gas_route_branch_number"),
    )
    op.create_index("idx_gas_route_branch", "gas_route", ["branch_id"])
    op.create_index("idx_gas_route_active", "gas_route", ["active"])

    op.create_table(
        "gas_route_member",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("route_id", sa.BigInteger(), nullable=False),
        sa.Column("line_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "is_reference", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["route_id"], ["gas_route.id"],
            name="fk_gas_route_member_route", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["line_id"], ["gas_volume_line.id"],
            name="fk_gas_route_member_line", ondelete="CASCADE",
        ),
        sa.UniqueConstraint("line_id", name="uq_gas_route_member_line"),
    )
    op.create_index("idx_gas_route_member_route", "gas_route_member", ["route_id"])

    # CONCURRENTLY cannot run inside a transaction, and it can leave an INVALID
    # index behind if it fails — re-running the migration then needs the index
    # dropped first, which is what IF NOT EXISTS alone would not tell you.
    with op.get_context().autocommit_block():
        op.create_index(
            "idx_edit_line_type_period",
            "edit_archive",
            ["line_id", "edit_type_id", "period"],
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "idx_edit_line_type_period",
            table_name="edit_archive",
            postgresql_concurrently=True,
            if_exists=True,
        )

    op.drop_index("idx_gas_route_member_route", table_name="gas_route_member")
    op.drop_table("gas_route_member")
    op.drop_index("idx_gas_route_active", table_name="gas_route")
    op.drop_index("idx_gas_route_branch", table_name="gas_route")
    op.drop_table("gas_route")
