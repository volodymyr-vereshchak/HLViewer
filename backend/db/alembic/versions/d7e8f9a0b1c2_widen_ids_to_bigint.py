"""widen all integer id PKs (and FK columns) to bigint

Revision ID: d7e8f9a0b1c2
Revises: e7f8a9b0c1d2
Create Date: 2026-06-22 12:00:00.000000

Background
----------
sys_archive.id_seq reached the int4 ceiling (2,147,483,647) and INSERTs began
failing with "nextval: reached maximum value of sequence". The table only holds
~4.5M real rows — the sequence was burned because the updater re-imports the
overlapping window every cycle and ``INSERT ... ON CONFLICT DO NOTHING`` calls
``nextval()`` for EVERY candidate row (incl. already-present ones) before the
conflict is arbitrated. Same ~450x burn ratio was observed on hourly_archive
(14% of int4 used), edit_archive (5%) and daily_archive.

This migration widens every sequence-backed integer ``id`` PK — and every FK
column that references one — to ``bigint``, giving effectively unlimited
headroom. The companion code fix (``bulk_upsert_via_copy`` -> WHERE NOT EXISTS)
stops the burn so even bigint is not wasted.

Notes
-----
* Big tables (sys_archive, hourly_archive) are rewritten; run during a quiet
  window. All id/FK columns of a table are widened in a single ALTER TABLE so
  each table is rewritten at most once.
* int4 FK columns referencing bigint PKs are valid in PostgreSQL, but we widen
  them too so the schema stays type-consistent and future autogenerate is a
  no-op.
* downgrade() narrows back to int4 and WILL FAIL if any id already exceeds the
  int4 range (sys_archive certainly does) — that is expected; do not downgrade
  a table whose ids have grown past 2.1e9.
"""
from alembic import op


# revision identifiers, used by Alembic.
revision = 'd7e8f9a0b1c2'
down_revision = 'e7f8a9b0c1d2'
branch_labels = None
depends_on = None


# table -> integer columns to widen (PK id first, then FK columns and any other
# *_id columns). Every table has a "<table>_id_seq" sequence owned by its id
# column. Non-FK *_id columns (source-system type codes, telegram user_id) are
# folded into the same ALTER TABLE so big tables are still rewritten only once;
# telegram_users.user_id in particular must be bigint (modern Telegram IDs
# already exceed the int4 range).
TABLE_COLUMNS = {
    'app_user': ['id'],
    'app_user_branch_access': ['id', 'user_id', 'branch_id'],
    'branch_config_mapping': ['id', 'branch_id', 'lumg_id'],
    'branch_data_path': ['id', 'branch_id'],
    'corector_type': ['id', 'manufacturer_id'],
    'daily_archive': ['id', 'line_id'],
    'edit_archive': ['id', 'line_id', 'edit_type_id'],
    'edit_type': ['id', 'edit_type_id', 'gas_volume_calc_type_id'],
    'enterprise': ['id', 'line_id', 'branch_id'],
    'gas_vol_calc_type': ['id', 'type_id'],
    'gas_volume_calc': ['id', 'lumg_id', 'type_id'],
    'gas_volume_line': ['id', 'gas_volume_calc_id'],
    'grmu_branch': ['id'],
    'grmu_branch_device_mapping': ['id', 'branch_id', 'line_id'],
    'grmu_branch_dpd_credential': ['id', 'branch_id'],
    'hourly_archive': ['id', 'line_id'],
    'lumg': ['id', 'branch_id'],
    'lumg_data_path': ['id', 'lumg_id'],
    'lumg_eis_code': ['id', 'lumg_id'],
    'manufacturer': ['id'],
    'params': ['id', 'line_id'],
    'sys_archive': ['id', 'line_id', 'sys_type_id'],
    'sys_type': ['id', 'sys_type_id', 'gas_volume_calc_type_id'],
    'telegram_users': ['id', 'user_id'],
    'update_job': ['id', 'lumg_id'],
    'virtual_line': ['id', 'branch_id', 'lumg_id'],
    'virtual_line_member': ['id', 'line_id', 'virtual_line_id'],
}


def upgrade():
    for table, columns in TABLE_COLUMNS.items():
        sets = ', '.join(
            f'ALTER COLUMN "{col}" TYPE bigint' for col in columns
        )
        op.execute(f'ALTER TABLE "{table}" {sets}')
        # Lift the sequence's own int4 ceiling (this is what actually unblocks
        # nextval on an exhausted sequence like sys_archive_id_seq).
        op.execute(f'ALTER SEQUENCE "{table}_id_seq" AS bigint')


def downgrade():
    for table, columns in TABLE_COLUMNS.items():
        op.execute(f'ALTER SEQUENCE "{table}_id_seq" AS integer')
        sets = ', '.join(
            f'ALTER COLUMN "{col}" TYPE integer' for col in columns
        )
        op.execute(f'ALTER TABLE "{table}" {sets}')
