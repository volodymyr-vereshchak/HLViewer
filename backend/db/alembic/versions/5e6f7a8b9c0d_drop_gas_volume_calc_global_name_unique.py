"""drop global unique on gas_volume_calc.name, add per-lumg unique

Revision ID: 5e6f7a8b9c0d
Revises: 4d5e6f7a8b9c
Create Date: 2026-03-17 00:00:05.000000

Replaces the global UNIQUE(name) on gas_volume_calc with UNIQUE(lumg_id, name),
allowing the same calculator name to exist under different LUMGs (branches).
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '5e6f7a8b9c0d'
down_revision = '4d5e6f7a8b9c'
branch_labels = None
depends_on = None


def upgrade():
    # Drop the old unnamed global unique constraint on name
    # PostgreSQL auto-names it gas_volume_calc_name_key
    op.drop_constraint('gas_volume_calc_name_key', 'gas_volume_calc', type_='unique')

    # Add per-lumg unique constraint
    op.create_unique_constraint(
        'uq_gvc_lumg_name',
        'gas_volume_calc',
        ['lumg_id', 'name'],
    )


def downgrade():
    op.drop_constraint('uq_gvc_lumg_name', 'gas_volume_calc', type_='unique')
    op.create_unique_constraint('gas_volume_calc_name_key', 'gas_volume_calc', ['name'])
