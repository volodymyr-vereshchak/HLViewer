"""Weather rows say whether they are history or forecast, and when they were written

Revision ID: e7a9c3d1f4b2
Revises: d5f8b2c0e3a1
Create Date: 2026-11-08
"""
import sqlalchemy as sa
from alembic import op

revision = "e7a9c3d1f4b2"
down_revision = "d5f8b2c0e3a1"
branch_labels = None
depends_on = None

TABLES = ("weather_daily", "weather_hourly")


def upgrade() -> None:
    for t in TABLES:
        # Everything stored so far came from the archive.
        op.add_column(t, sa.Column("source", sa.String(length=8), nullable=False,
                                   server_default="archive"))
        op.add_column(t, sa.Column("created_at", sa.DateTime(), nullable=True))
        op.add_column(t, sa.Column("updated_at", sa.DateTime(), nullable=True))
        # Kyiv wall clock, the convention of every stamp in this schema
        # (Postgres itself runs on UTC).
        op.execute(
            f"UPDATE {t} SET created_at = now() AT TIME ZONE 'Europe/Kyiv', "
            f"updated_at = now() AT TIME ZONE 'Europe/Kyiv'"
        )


def downgrade() -> None:
    for t in TABLES:
        # A forecast would read as history once the column is gone.
        op.execute(f"DELETE FROM {t} WHERE source = 'forecast'")
        op.drop_column(t, "updated_at")
        op.drop_column(t, "created_at")
        op.drop_column(t, "source")
