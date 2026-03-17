"""add virtual_line and virtual_line_member tables

Revision ID: 4d5e6f7a8b9c
Revises: 3c4d5e6f7a8b
Create Date: 2026-03-17 00:00:04.000000

Replaces virtual_lines.json with proper DB tables.
Use scripts/import_virtual_lines.py to populate initial data.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision = '4d5e6f7a8b9c'
down_revision = '3c4d5e6f7a8b'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'virtual_line',
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('name', sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
        sa.Column('description', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('branch_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ['branch_id'], ['grmu_branch.id'],
            name='fk_virtual_line_branch',
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('branch_id', 'name', name='uq_virtual_line_branch_name'),
    )
    op.create_index('idx_virtual_line_branch', 'virtual_line', ['branch_id'], unique=False)
    op.create_index('idx_virtual_line_active', 'virtual_line', ['active'], unique=False)

    op.create_table(
        'virtual_line_member',
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('virtual_line_id', sa.Integer(), nullable=False),
        sa.Column('line_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ['virtual_line_id'], ['virtual_line.id'],
            name='fk_vlm_virtual_line',
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['line_id'], ['gas_volume_line.id'],
            name='fk_vlm_line',
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('virtual_line_id', 'line_id', name='uq_vlm_virtual_line_line'),
    )
    op.create_index('idx_vlm_virtual_line', 'virtual_line_member', ['virtual_line_id'], unique=False)
    op.create_index('idx_vlm_line', 'virtual_line_member', ['line_id'], unique=False)


def downgrade():
    op.drop_index('idx_vlm_line', table_name='virtual_line_member')
    op.drop_index('idx_vlm_virtual_line', table_name='virtual_line_member')
    op.drop_table('virtual_line_member')
    op.drop_index('idx_virtual_line_active', table_name='virtual_line')
    op.drop_index('idx_virtual_line_branch', table_name='virtual_line')
    op.drop_table('virtual_line')
