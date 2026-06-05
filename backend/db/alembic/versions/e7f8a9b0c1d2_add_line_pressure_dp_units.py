"""add pressure_unit and dp_unit to gas_volume_line

Revision ID: e7f8a9b0c1d2
Revises: c4d5e6f7a8b9
Create Date: 2026-06-05 00:00:00.000000

Adds per-line measurement-unit labels for pressure and differential pressure
to gas_volume_line. Values are display-only labels (no value conversion);
defaults match the previously hardcoded units (кгс/см² / кгс/м²).
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e7f8a9b0c1d2'
down_revision = 'c4d5e6f7a8b9'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'gas_volume_line',
        sa.Column('pressure_unit', sa.String(length=16), nullable=False,
                  server_default='кгс/см²'),
    )
    op.add_column(
        'gas_volume_line',
        sa.Column('dp_unit', sa.String(length=16), nullable=False,
                  server_default='кгс/м²'),
    )


def downgrade():
    op.drop_column('gas_volume_line', 'dp_unit')
    op.drop_column('gas_volume_line', 'pressure_unit')
