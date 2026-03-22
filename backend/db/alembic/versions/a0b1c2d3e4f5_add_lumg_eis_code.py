"""add lumg_eis_code

Revision ID: a0b1c2d3e4f5
Revises: 8c9d0e1f2a3b
Create Date: 2026-03-22 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a0b1c2d3e4f5'
down_revision: Union[str, None] = '8c9d0e1f2a3b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'lumg_eis_code',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('lumg_id', sa.Integer(), nullable=False),
        sa.Column('eis_code', sa.String(length=100), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['lumg_id'], ['lumg.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('eis_code', name='uq_lumg_eis_code'),
    )
    op.create_index('idx_lumg_eis_code_lumg_id', 'lumg_eis_code', ['lumg_id'])


def downgrade() -> None:
    op.drop_index('idx_lumg_eis_code_lumg_id', table_name='lumg_eis_code')
    op.drop_table('lumg_eis_code')
