"""Enterprise can be bound to a DPD line as an alternative to a physical one.

Adds enterprise.dpd_line_id (FK dpd_line, SET NULL) next to the existing
line_id (FK gas_volume_line). Exactly one kind of line link may be set —
enforced by a CHECK. Line ids never collide across kinds (shared_line_id_seq),
so the volume pipeline groups by the effective id COALESCE(line_id,
dpd_line_id) without further changes.

Revision ID: b5c6d7e8f9a0
Revises: a4b5c6d7e8f9
Create Date: 2026-07-19

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b5c6d7e8f9a0"
down_revision: Union[str, None] = "a4b5c6d7e8f9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "enterprise",
        sa.Column("dpd_line_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_enterprise_dpd_line",
        "enterprise",
        "dpd_line",
        ["dpd_line_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_enterprise_dpd_line", "enterprise", ["dpd_line_id"])
    op.create_check_constraint(
        "ck_enterprise_single_line",
        "enterprise",
        "line_id IS NULL OR dpd_line_id IS NULL",
    )


def downgrade() -> None:
    op.drop_constraint("ck_enterprise_single_line", "enterprise", type_="check")
    op.drop_index("ix_enterprise_dpd_line", table_name="enterprise")
    op.drop_constraint("fk_enterprise_dpd_line", "enterprise", type_="foreignkey")
    op.drop_column("enterprise", "dpd_line_id")
