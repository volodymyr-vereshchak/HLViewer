"""add device catalog (manufacturer + corector_type)

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-03-21 01:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel

revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'manufacturer',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('short_name', sqlmodel.AutoString(), nullable=False),
        sa.Column('full_name', sqlmodel.AutoString(), nullable=False),
        sa.Column('mf_dev', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_manufacturer_short_name', 'manufacturer', ['short_name'])
    op.create_index('ix_manufacturer_mf_dev', 'manufacturer', ['mf_dev'])

    op.create_table(
        'corector_type',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('manufacturer_id', sa.Integer(), nullable=False),
        sa.Column('model_name', sqlmodel.AutoString(), nullable=False),
        sa.Column('type_dev', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['manufacturer_id'], ['manufacturer.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_corector_type_manufacturer_id', 'corector_type', ['manufacturer_id'])
    op.create_index('ix_corector_type_model_name', 'corector_type', ['model_name'])


def downgrade() -> None:
    op.drop_index('ix_corector_type_model_name', table_name='corector_type')
    op.drop_index('ix_corector_type_manufacturer_id', table_name='corector_type')
    op.drop_table('corector_type')
    op.drop_index('ix_manufacturer_mf_dev', table_name='manufacturer')
    op.drop_index('ix_manufacturer_short_name', table_name='manufacturer')
    op.drop_table('manufacturer')
