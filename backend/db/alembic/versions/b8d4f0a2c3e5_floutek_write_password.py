"""Floutek write password on the poll card

Ask2's ПК-В and ВР-2 drivers put «Пароль на запис» into every archive
request, in the twelve bytes before the user code. A corrector that has one
set is read with it; for one that has none the field stays empty, which is
what every card holds after this migration.

Revision ID: b8d4f0a2c3e5
Revises: a7c3e9f1b2d4
Create Date: 2026-11-02
"""
import sqlalchemy as sa
from alembic import op

revision = "b8d4f0a2c3e5"
down_revision = "a7c3e9f1b2d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "poll_device",
        sa.Column("write_password", sa.String(length=12), nullable=False,
                  server_default=""),
    )


def downgrade() -> None:
    op.drop_column("poll_device", "write_password")
