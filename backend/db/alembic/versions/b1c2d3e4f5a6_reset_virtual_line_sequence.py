"""reset virtual line sequence to normal auto-increment

Revision ID: b1c2d3e4f5a6
Revises: 9a8b7c6d5e4f
Create Date: 2026-03-18 12:00:00.000000

Remove the artificial >= 1000 offset on the virtual_line sequence.
is_virtual / DB-lookup is now used instead of numeric ID convention.
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'b1c2d3e4f5a6'
down_revision: Union[str, None] = '9a8b7c6d5e4f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Reset sequence to max(id)+1 — normal auto-increment, no artificial offset
    op.execute(
        "SELECT setval(pg_get_serial_sequence('virtual_line','id'), "
        "GREATEST(1, (SELECT COALESCE(MAX(id), 0) FROM virtual_line)))"
    )


def downgrade() -> None:
    # Restore the >= 1000 offset (undo)
    op.execute(
        "SELECT setval(pg_get_serial_sequence('virtual_line','id'), "
        "GREATEST(1000, (SELECT COALESCE(MAX(id), 0) FROM virtual_line)))"
    )
