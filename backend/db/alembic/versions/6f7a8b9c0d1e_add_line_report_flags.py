"""add include_in_report and is_high_pressure flags to line tables

Revision ID: 6f7a8b9c0d1e
Revises: 5e6f7a8b9c0d
Create Date: 2026-03-17 00:00:06.000000

Adds include_in_report / is_high_pressure flags to gas_volume_line and
virtual_line, replacing the hardcoded LINES_IDS / HIGH_P_LINES_IDS in
settings.py. Backfills existing physical lines from current settings values.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '6f7a8b9c0d1e'
down_revision = '5e6f7a8b9c0d'
branch_labels = None
depends_on = None


def upgrade():
    # ── gas_volume_line ────────────────────────────────────────────────────────
    op.add_column(
        'gas_volume_line',
        sa.Column('include_in_report', sa.Boolean(), nullable=False, server_default='false'),
    )
    op.add_column(
        'gas_volume_line',
        sa.Column('is_high_pressure', sa.Boolean(), nullable=False, server_default='false'),
    )
    op.create_index('idx_line_include_in_report', 'gas_volume_line', ['include_in_report'])
    op.create_index('idx_line_is_high_pressure', 'gas_volume_line', ['is_high_pressure'])

    # Backfill from current settings values
    op.execute(
        "UPDATE gas_volume_line SET include_in_report = TRUE "
        "WHERE id IN (6, 11, 16, 17, 18, 19, 20, 21)"
    )
    op.execute(
        "UPDATE gas_volume_line SET is_high_pressure = TRUE "
        "WHERE id = 6"
    )

    # ── virtual_line ───────────────────────────────────────────────────────────
    op.add_column(
        'virtual_line',
        sa.Column('include_in_report', sa.Boolean(), nullable=False, server_default='false'),
    )
    op.add_column(
        'virtual_line',
        sa.Column('is_high_pressure', sa.Boolean(), nullable=False, server_default='false'),
    )
    op.create_index(
        'idx_virtual_line_include_in_report', 'virtual_line', ['include_in_report']
    )
    # Note: virtual_line flags are NOT backfilled here because virtual lines
    # have not been imported yet. After running import_virtual_lines.py, set
    # the flags manually via PATCH /grmu_branch/{id}/virtual_lines/{id} or
    # directly in the DB.


def downgrade():
    op.drop_index('idx_virtual_line_include_in_report', table_name='virtual_line')
    op.drop_column('virtual_line', 'is_high_pressure')
    op.drop_column('virtual_line', 'include_in_report')

    op.drop_index('idx_line_is_high_pressure', table_name='gas_volume_line')
    op.drop_index('idx_line_include_in_report', table_name='gas_volume_line')
    op.drop_column('gas_volume_line', 'is_high_pressure')
    op.drop_column('gas_volume_line', 'include_in_report')
