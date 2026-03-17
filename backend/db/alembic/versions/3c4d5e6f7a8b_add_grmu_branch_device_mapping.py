"""add grmu_branch_device_mapping table

Revision ID: 3c4d5e6f7a8b
Revises: 2b3c4d5e6f7a
Create Date: 2026-03-17 00:00:03.000000

Replaces the enterprise.xlsx / line_id.xlsx Excel files with a proper DB table.
Use scripts/import_grmu_branch_mappings.py to populate initial data.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision = '3c4d5e6f7a8b'
down_revision = '2b3c4d5e6f7a'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'grmu_branch_device_mapping',
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('ser_num', sa.Integer(), nullable=False),
        sa.Column('mf_dev', sa.Integer(), nullable=False),
        sa.Column('type_dev', sa.Integer(), nullable=False),
        sa.Column('ch_num', sa.Integer(), nullable=False),
        sa.Column('grmu_branch_name', sqlmodel.sql.sqltypes.AutoString(length=255), nullable=True),
        sa.Column('counterpart', sqlmodel.sql.sqltypes.AutoString(length=255), nullable=True),
        sa.Column('sector', sqlmodel.sql.sqltypes.AutoString(length=255), nullable=True),
        sa.Column('status', sqlmodel.sql.sqltypes.AutoString(length=32), nullable=True),
        sa.Column('status_changed_at', sa.DateTime(), nullable=True),
        sa.Column('device_type', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('manufacturer', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('active', sa.Boolean(), nullable=True),
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('branch_id', sa.Integer(), nullable=False),
        sa.Column('line_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ['branch_id'], ['grmu_branch.id'],
            name='fk_device_mapping_branch',
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['line_id'], ['gas_volume_line.id'],
            name='fk_device_mapping_line',
            ondelete='SET NULL',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'branch_id', 'ser_num', 'mf_dev', 'type_dev', 'ch_num',
            name='uq_branch_device',
        ),
    )
    op.create_index('idx_branch_device_mapping_branch', 'grmu_branch_device_mapping', ['branch_id'], unique=False)
    op.create_index('idx_branch_device_mapping_line', 'grmu_branch_device_mapping', ['line_id'], unique=False)
    op.create_index(
        'idx_branch_device_mapping_device',
        'grmu_branch_device_mapping',
        ['ser_num', 'mf_dev', 'type_dev', 'ch_num'],
        unique=False,
    )


def downgrade():
    op.drop_index('idx_branch_device_mapping_device', table_name='grmu_branch_device_mapping')
    op.drop_index('idx_branch_device_mapping_line', table_name='grmu_branch_device_mapping')
    op.drop_index('idx_branch_device_mapping_branch', table_name='grmu_branch_device_mapping')
    op.drop_table('grmu_branch_device_mapping')
