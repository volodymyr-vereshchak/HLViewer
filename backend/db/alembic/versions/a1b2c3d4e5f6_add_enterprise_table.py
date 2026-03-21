"""add enterprise table

Revision ID: a1b2c3d4e5f6
Revises: f3ca6152901a
Create Date: 2026-03-21 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel

revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'enterprise',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('enterprise_name', sqlmodel.AutoString(), nullable=False),
        sa.Column('line_id', sa.Integer(), nullable=True),
        sa.Column('ser_num', sa.Integer(), nullable=False),
        sa.Column('mf_dev', sa.Integer(), nullable=False),
        sa.Column('type_dev', sa.Integer(), nullable=False),
        sa.Column('ch_num', sa.Integer(), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.ForeignKeyConstraint(['line_id'], ['gas_volume_line.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_enterprise_enterprise_name', 'enterprise', ['enterprise_name'])


def downgrade() -> None:
    op.drop_index('ix_enterprise_enterprise_name', table_name='enterprise')
    op.drop_table('enterprise')
