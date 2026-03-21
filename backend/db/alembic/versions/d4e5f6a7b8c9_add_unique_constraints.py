"""add unique constraints to enterprise, manufacturer, corector_type

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-03-21

"""
from alembic import op

revision = 'e6f7a8b9c0d1'
down_revision = 'c3d4e5f6a7b8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # enterprise: device is uniquely identified by ser_num + mf_dev + type_dev + ch_num
    op.create_unique_constraint(
        'uq_enterprise_device', 'enterprise',
        ['ser_num', 'mf_dev', 'type_dev', 'ch_num']
    )

    # manufacturer: each field is separately unique
    op.create_unique_constraint('uq_manufacturer_short_name', 'manufacturer', ['short_name'])
    op.create_unique_constraint('uq_manufacturer_full_name',  'manufacturer', ['full_name'])
    op.create_unique_constraint('uq_manufacturer_mf_dev',     'manufacturer', ['mf_dev'])

    # corector_type: one model name per manufacturer
    op.create_unique_constraint('uq_corector_type_mfr_model', 'corector_type', ['manufacturer_id', 'model_name'])


def downgrade() -> None:
    op.drop_constraint('uq_corector_type_mfr_model', 'corector_type', type_='unique')
    op.drop_constraint('uq_manufacturer_mf_dev',        'manufacturer',  type_='unique')
    op.drop_constraint('uq_manufacturer_full_name',     'manufacturer',  type_='unique')
    op.drop_constraint('uq_manufacturer_short_name',    'manufacturer',  type_='unique')
    op.drop_constraint('uq_enterprise_device',          'enterprise',    type_='unique')
