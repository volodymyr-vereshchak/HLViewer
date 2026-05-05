"""fix gas_vol_calc_type: unique on type_id instead of type_name, deduplicate

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-05-05

"""
import sqlalchemy as sa
from alembic import op

revision = "f2a3b4c5d6e7"
down_revision = "e1f2a3b4c5d6"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()

    # 1. For each duplicate type_id: re-point GVC rows to the record with a
    #    non-numeric name, then delete the phantom (numeric-name) record.
    duplicates = conn.execute(sa.text("""
        SELECT type_id
        FROM gas_vol_calc_type
        GROUP BY type_id
        HAVING COUNT(*) > 1
    """)).fetchall()

    for (tid,) in duplicates:
        # Pick the "real" record: prefer non-numeric type_name
        real_id = conn.execute(sa.text("""
            SELECT id FROM gas_vol_calc_type
            WHERE type_id = :tid
              AND type_name ~ '[^0-9]'
            ORDER BY id
            LIMIT 1
        """), {"tid": tid}).scalar()

        phantom_ids = conn.execute(sa.text("""
            SELECT id FROM gas_vol_calc_type
            WHERE type_id = :tid AND id != :real_id
        """), {"tid": tid, "real_id": real_id}).scalars().fetchall()

        for pid in phantom_ids:
            # Re-point any GasVolumeCalc rows referencing the phantom
            conn.execute(sa.text("""
                UPDATE gas_volume_calc SET type_id = :real_id
                WHERE type_id = :pid
            """), {"real_id": real_id, "pid": pid})
            # Delete phantom
            conn.execute(sa.text(
                "DELETE FROM gas_vol_calc_type WHERE id = :pid"
            ), {"pid": pid})

    # 2. Drop old unique constraint on type_name
    op.drop_constraint("gas_vol_calc_type_type_name_key", "gas_vol_calc_type", type_="unique")

    # 3. Add unique constraint on type_id
    op.create_unique_constraint("uq_gas_vol_calc_type_type_id", "gas_vol_calc_type", ["type_id"])


def downgrade():
    op.drop_constraint("uq_gas_vol_calc_type_type_id", "gas_vol_calc_type", type_="unique")
    op.create_unique_constraint("gas_vol_calc_type_type_name_key", "gas_vol_calc_type", ["type_name"])
