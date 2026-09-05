"""Restore NOT NULL on the timestamp columns four tables lost

Every model built on HlBaseModel declares created_at/updated_at as
nullable=False, but the migrations that created these four tables emitted them
without the constraint. The columns have been populated by the default_factory
ever since, so there is nothing to backfill — this only makes the database say
what the models have always claimed.

Verified before writing: zero NULLs across all eight columns.

Revision ID: c8b1d4e7f209
Revises: d1f3a5c7e9b2
Create Date: 2026-09-05
"""
from alembic import op
import sqlalchemy as sa

revision = "c8b1d4e7f209"
down_revision = "d1f3a5c7e9b2"
branch_labels = None
depends_on = None

TABLES = (
    "branch_config_mapping",
    "branch_data_path",
    "lumg_data_path",
    "lumg_eis_code",
)
COLUMNS = ("created_at", "updated_at")


def upgrade() -> None:
    # A row predating the default_factory would block the ALTER, and these are
    # configuration tables where losing one is worse than leaving it nullable —
    # so fill rather than fail. On the checked databases this updates nothing.
    for table in TABLES:
        for column in COLUMNS:
            op.execute(
                f"UPDATE {table} SET {column} = now() AT TIME ZONE 'Europe/Kyiv' "
                f"WHERE {column} IS NULL"
            )
            op.alter_column(
                table, column,
                existing_type=sa.DateTime(),
                nullable=False,
            )


def downgrade() -> None:
    for table in TABLES:
        for column in COLUMNS:
            op.alter_column(
                table, column,
                existing_type=sa.DateTime(),
                nullable=True,
            )
