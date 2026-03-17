"""add grmu_branch and dpd_credential

Revision ID: 1a2b3c4d5e6f
Revises: d3579290ea96
Create Date: 2026-03-17 00:00:01.000000

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision = '1a2b3c4d5e6f'
down_revision = 'd3579290ea96'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'grmu_branch',
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('name', sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
        sa.Column('short_name', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True),
        sa.Column('region', sqlmodel.sql.sqltypes.AutoString(length=255), nullable=True),
        sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('id', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name', name='uq_grmu_branch_name'),
    )
    op.create_index('idx_grmu_branch_name', 'grmu_branch', ['name'], unique=False)
    op.create_index('idx_grmu_branch_active', 'grmu_branch', ['active'], unique=False)

    op.create_table(
        'grmu_branch_dpd_credential',
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('api_base_url', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('auth_url', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('username', sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
        sa.Column('password', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('timeout_sec', sa.Integer(), nullable=False, server_default=sa.text('30')),
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('branch_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ['branch_id'], ['grmu_branch.id'],
            name='fk_dpd_credential_branch',
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('branch_id', name='uq_dpd_credential_branch'),
    )


def downgrade():
    op.drop_table('grmu_branch_dpd_credential')
    op.drop_index('idx_grmu_branch_active', table_name='grmu_branch')
    op.drop_index('idx_grmu_branch_name', table_name='grmu_branch')
    op.drop_table('grmu_branch')
