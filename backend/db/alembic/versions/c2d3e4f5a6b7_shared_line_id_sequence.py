"""shared line id sequence for virtual and physical lines

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-03-18 13:00:00.000000

Virtual and physical lines now share a single ID sequence so their IDs
never collide. Any existing virtual lines whose ID conflicts with a
physical line are reassigned to a new non-conflicting ID before the
shared sequence is created.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c2d3e4f5a6b7'
down_revision: Union[str, None] = 'b1c2d3e4f5a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    # ── 1. Fix existing ID conflicts ──────────────────────────────────────────
    # Find virtual line IDs that collide with physical line IDs
    conflicts = conn.execute(sa.text("""
        SELECT vl.id
        FROM virtual_line vl
        INNER JOIN gas_volume_line gl ON gl.id = vl.id
        ORDER BY vl.id
    """)).fetchall()

    if conflicts:
        # Current max across both tables
        max_id = conn.execute(sa.text("""
            SELECT GREATEST(
                COALESCE((SELECT MAX(id) FROM gas_volume_line), 0),
                COALESCE((SELECT MAX(id) FROM virtual_line), 0)
            )
        """)).scalar()

        # Drop FK so we can update PKs freely
        conn.execute(sa.text(
            "ALTER TABLE virtual_line_member DROP CONSTRAINT IF EXISTS fk_vlm_virtual_line"
        ))

        for (conflict_id,) in conflicts:
            max_id += 1
            new_id = max_id
            # Update the virtual line PK first
            conn.execute(sa.text(
                "UPDATE virtual_line SET id = :new WHERE id = :old"
            ), {"new": new_id, "old": conflict_id})
            # Then update members
            conn.execute(sa.text(
                "UPDATE virtual_line_member SET virtual_line_id = :new WHERE virtual_line_id = :old"
            ), {"new": new_id, "old": conflict_id})

        # Re-add FK constraint
        conn.execute(sa.text("""
            ALTER TABLE virtual_line_member
            ADD CONSTRAINT fk_vlm_virtual_line
            FOREIGN KEY (virtual_line_id) REFERENCES virtual_line(id)
            ON DELETE CASCADE
        """))

    # ── 2. Create shared sequence ──────────────────────────────────────────────
    op.execute("CREATE SEQUENCE IF NOT EXISTS shared_line_id_seq")

    # Initialise to max of all existing IDs (nextval will return this + 1)
    op.execute("""
        SELECT setval('shared_line_id_seq', GREATEST(
            COALESCE((SELECT MAX(id) FROM gas_volume_line), 0),
            COALESCE((SELECT MAX(id) FROM virtual_line), 0)
        ))
    """)

    # ── 3. Point both tables to the shared sequence ────────────────────────────
    op.execute(
        "ALTER TABLE gas_volume_line "
        "ALTER COLUMN id SET DEFAULT nextval('shared_line_id_seq')"
    )
    op.execute(
        "ALTER TABLE virtual_line "
        "ALTER COLUMN id SET DEFAULT nextval('shared_line_id_seq')"
    )


def downgrade() -> None:
    # Restore original per-table sequences
    op.execute(
        "ALTER TABLE gas_volume_line "
        "ALTER COLUMN id SET DEFAULT nextval('gas_volume_line_id_seq')"
    )
    op.execute(
        "ALTER TABLE virtual_line "
        "ALTER COLUMN id SET DEFAULT nextval('virtual_line_id_seq')"
    )
    op.execute("DROP SEQUENCE IF EXISTS shared_line_id_seq")
