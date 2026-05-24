"""add grmu_branch FK to lumg with backfill

Revision ID: 2b3c4d5e6f7a
Revises: 1a2b3c4d5e6f
Create Date: 2026-03-17 00:00:02.000000

Seeds grmu_branch id=1 as the default branch for all existing LUMG rows.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision = '2b3c4d5e6f7a'
down_revision = '1a2b3c4d5e6f'
branch_labels = None
depends_on = None


def upgrade():
    # 1. Add branch_id column (nullable first, so existing rows don't fail)
    op.add_column(
        'lumg',
        sa.Column('branch_id', sa.Integer(), nullable=True),
    )

    # 3. Backfill: assign all existing lumgs to branch id=1
    op.execute(sa.text("UPDATE lumg SET branch_id = 1"))

    # 4. Set NOT NULL now that all rows are populated
    op.alter_column('lumg', 'branch_id', nullable=False)

    # 5. Add FK constraint
    op.create_foreign_key(
        'fk_lumg_grmu_branch',
        'lumg', 'grmu_branch',
        ['branch_id'], ['id'],
        ondelete='CASCADE',
    )

    # 6. Drop old global unique constraint on name
    op.drop_constraint('lumg_name_key', 'lumg', type_='unique')

    # 7. Add new per-branch unique constraint
    op.create_unique_constraint(
        'uq_lumg_grmu_branch_name',
        'lumg',
        ['branch_id', 'name'],
    )

    # 8. Index on branch_id
    op.create_index('idx_lumg_branch', 'lumg', ['branch_id'], unique=False)


def downgrade():
    op.drop_index('idx_lumg_branch', table_name='lumg')
    op.drop_constraint('uq_lumg_grmu_branch_name', 'lumg', type_='unique')
    op.create_unique_constraint('lumg_name_key', 'lumg', ['name'])
    op.drop_constraint('fk_lumg_grmu_branch', 'lumg', type_='foreignkey')
    op.drop_column('lumg', 'branch_id')
    # The seed row in grmu_branch is intentionally left in place
