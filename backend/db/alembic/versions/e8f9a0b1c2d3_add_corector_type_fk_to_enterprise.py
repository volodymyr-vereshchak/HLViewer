"""add corector_type FK to enterprise with backfill

Revision ID: e8f9a0b1c2d3
Revises: d7e8f9a0b1c2
Create Date: 2026-06-27 00:00:00.000000

Normalizes enterprise device identity: enterprise -> corector_type -> manufacturer.
Backfills corector_type_id by matching the existing (mf_dev, type_dev) pair to a
catalog entry. Rows with no catalog match are left NULL (reported in the migration
log) and keep working via the legacy mf_dev/type_dev columns, which become nullable.
The unique device constraint moves from (ser_num, mf_dev, type_dev, ch_num) to
(ser_num, corector_type_id, ch_num).
"""
import logging

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e8f9a0b1c2d3'
down_revision = 'd7e8f9a0b1c2'
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade():
    # 1. Add nullable FK column + index
    op.add_column('enterprise', sa.Column('corector_type_id', sa.BigInteger(), nullable=True))
    op.create_index('idx_enterprise_corector_type', 'enterprise', ['corector_type_id'], unique=False)
    op.create_foreign_key(
        'fk_enterprise_corector_type',
        'enterprise', 'corector_type',
        ['corector_type_id'], ['id'],
        ondelete='RESTRICT',
    )

    # 2. Backfill from the device catalog by matching (mf_dev, type_dev)
    op.execute(sa.text("""
        UPDATE enterprise e
        SET corector_type_id = ct.id
        FROM corector_type ct
        JOIN manufacturer m ON ct.manufacturer_id = m.id
        WHERE m.mf_dev = e.mf_dev AND ct.type_dev = e.type_dev
    """))

    # 3. Report rows that couldn't be linked (kept on legacy mf_dev/type_dev)
    bind = op.get_bind()
    unmatched = bind.execute(sa.text("""
        SELECT id, enterprise_name, ser_num, mf_dev, type_dev, ch_num
        FROM enterprise
        WHERE corector_type_id IS NULL
        ORDER BY id
    """)).fetchall()
    if unmatched:
        logger.warning(
            "[corector_type backfill] %d enterprise row(s) have no matching "
            "corector_type and were left unlinked (legacy mf_dev/type_dev kept):",
            len(unmatched),
        )
        for row in unmatched:
            logger.warning(
                "  unlinked enterprise id=%s name=%r ser_num=%s mf_dev=%s type_dev=%s ch_num=%s",
                row[0], row[1], row[2], row[3], row[4], row[5],
            )
    else:
        logger.info("[corector_type backfill] all enterprise rows linked to a corector_type.")

    # 4. Legacy device-code columns become nullable (FK is now source of truth)
    op.alter_column('enterprise', 'mf_dev', existing_type=sa.Integer(), nullable=True)
    op.alter_column('enterprise', 'type_dev', existing_type=sa.Integer(), nullable=True)

    # 5. Move the unique device identity onto the FK
    op.drop_constraint('uq_enterprise_device', 'enterprise', type_='unique')
    op.create_unique_constraint(
        'uq_enterprise_device_ct', 'enterprise',
        ['ser_num', 'corector_type_id', 'ch_num'],
    )


def downgrade():
    op.drop_constraint('uq_enterprise_device_ct', 'enterprise', type_='unique')
    op.create_unique_constraint(
        'uq_enterprise_device', 'enterprise',
        ['ser_num', 'mf_dev', 'type_dev', 'ch_num'],
    )
    # Re-tighten the legacy columns (only safe if no NULLs remain)
    op.alter_column('enterprise', 'type_dev', existing_type=sa.Integer(), nullable=False)
    op.alter_column('enterprise', 'mf_dev', existing_type=sa.Integer(), nullable=False)
    op.drop_constraint('fk_enterprise_corector_type', 'enterprise', type_='foreignkey')
    op.drop_index('idx_enterprise_corector_type', table_name='enterprise')
    op.drop_column('enterprise', 'corector_type_id')
