"""add lumg_id to virtual_line

Revision ID: 9a8b7c6d5e4f
Revises: 8b9c0d1e2f3a
Create Date: 2026-03-18 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '9a8b7c6d5e4f'
down_revision: Union[str, None] = '8b9c0d1e2f3a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'virtual_line',
        sa.Column('lumg_id', sa.Integer(), nullable=True)
    )
    op.create_foreign_key(
        'fk_virtual_line_lumg_id',
        'virtual_line', 'lumg',
        ['lumg_id'], ['id'],
        ondelete='SET NULL'
    )
    # Shift sequence so new IDs are >= 1000
    op.execute(
        "SELECT setval(pg_get_serial_sequence('virtual_line','id'), "
        "GREATEST(1000, (SELECT COALESCE(MAX(id), 0) FROM virtual_line)))"
    )


def downgrade() -> None:
    op.drop_constraint('fk_virtual_line_lumg_id', 'virtual_line', type_='foreignkey')
    op.drop_column('virtual_line', 'lumg_id')
