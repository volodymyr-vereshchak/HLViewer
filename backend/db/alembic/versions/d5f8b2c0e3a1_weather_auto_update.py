"""Automatic weather update: settings, lock and status of the daily download

Revision ID: d5f8b2c0e3a1
Revises: c4e7a1b9d2f0
Create Date: 2026-11-08
"""
import sqlalchemy as sa
from alembic import op

revision = "d5f8b2c0e3a1"
down_revision = "c4e7a1b9d2f0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "weather_job",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("auto_enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("refresh_time", sa.String(length=5), nullable=False, server_default="12:00"),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("result", sa.String(length=255), nullable=True),
    )
    # Off until an admin switches it on: an offline server must not start
    # failing a download every day just because it was updated.
    op.execute("INSERT INTO weather_job (id, status) VALUES (1, 'idle')")


def downgrade() -> None:
    op.drop_table("weather_job")
