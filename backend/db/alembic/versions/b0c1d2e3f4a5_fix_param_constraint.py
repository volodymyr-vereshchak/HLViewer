"""fix param constraint: restore line_id+period unique key

Revision ID: b0c1d2e3f4a5
Revises: a0b1c2d3e4f5
Create Date: 2026-03-22 14:00:00.000000

Migration d3579290ea96 replaced param_line_period_constraint(line_id, period)
with param_all_constraint(all value columns, no period). This breaks inserts
when the same line has identical parameter values across different periods.
This migration restores the correct constraint.
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'b0c1d2e3f4a5'
down_revision: Union[str, None] = 'a0b1c2d3e4f5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint('param_all_constraint', 'params', type_='unique')
    op.create_unique_constraint('param_line_period_constraint', 'params', ['line_id', 'period'])


def downgrade() -> None:
    op.drop_constraint('param_line_period_constraint', 'params', type_='unique')
    op.create_unique_constraint(
        'param_all_constraint', 'params',
        ['density', 'co2', 'n2', 'D20', 'd20', 'cutoff', 'roughness',
         'max_dp', 'min_dp', 'A0su', 'A1su', 'A2su', 'A0pipe', 'A1pipe', 'A2pipe',
         'radius', 'su_year', 'max_p', 'min_p', 'max_t', 'min_t', 'line_id']
    )
