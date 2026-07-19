"""Virtual line (ring) members can be DPD lines.

virtual_line_member.line_id becomes nullable and a parallel dpd_line_id
(FK dpd_line, CASCADE) is added — exactly one of the two must be set.
Same dual-column pattern as enterprise.dpd_line_id: ids never collide
across line kinds (shared_line_id_seq), so ring resolution keeps working
with one effective member id COALESCE(line_id, dpd_line_id).

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
Create Date: 2026-07-19

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c6d7e8f9a0b1"
down_revision: Union[str, None] = "b5c6d7e8f9a0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("virtual_line_member", "line_id", nullable=True)
    op.add_column(
        "virtual_line_member",
        sa.Column("dpd_line_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_vlm_dpd_line",
        "virtual_line_member",
        "dpd_line",
        ["dpd_line_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("idx_vlm_dpd_line", "virtual_line_member", ["dpd_line_id"])
    op.create_unique_constraint(
        "uq_vlm_virtual_line_dpd",
        "virtual_line_member",
        ["virtual_line_id", "dpd_line_id"],
    )
    op.create_check_constraint(
        "ck_vlm_single_line",
        "virtual_line_member",
        "(line_id IS NULL) <> (dpd_line_id IS NULL)",
    )


def downgrade() -> None:
    op.execute("DELETE FROM virtual_line_member WHERE line_id IS NULL")
    op.drop_constraint("ck_vlm_single_line", "virtual_line_member", type_="check")
    op.drop_constraint("uq_vlm_virtual_line_dpd", "virtual_line_member", type_="unique")
    op.drop_index("idx_vlm_dpd_line", table_name="virtual_line_member")
    op.drop_constraint("fk_vlm_dpd_line", "virtual_line_member", type_="foreignkey")
    op.drop_column("virtual_line_member", "dpd_line_id")
    op.alter_column("virtual_line_member", "line_id", nullable=False)
