"""GRS consumption models: stations, uploaded volumes, covered days, uploads, fits

The tables behind Аналітика → «Модель споживання ГРС»: what an operator
uploads from 1С (the daily inflow by GRS and the daily non-household
consumption by category) and the models trained on it. See
backend/db/models/grs_model_model.py.

Revision ID: a2c4e6f8b0d1
Revises: f1b3d5a7c9e2
Create Date: 2026-11-14
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a2c4e6f8b0d1"
down_revision = "f1b3d5a7c9e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "grs_model_station",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("branch_id", sa.BigInteger(),
                  sa.ForeignKey("grmu_branch.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("weather_point_id", sa.BigInteger(),
                  sa.ForeignKey("weather_point.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("branch_id", "name", name="uq_grs_model_station_branch_name"),
    )
    op.create_table(
        "grs_model_volume",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("station_id", sa.BigInteger(),
                  sa.ForeignKey("grs_model_station.id", ondelete="CASCADE"), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("volume", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("station_id", "day", "category", name="uq_grs_model_volume_key"),
    )
    op.create_index("ix_grs_model_volume_station_day", "grs_model_volume", ["station_id", "day"])
    op.create_table(
        "grs_model_day",
        sa.Column("branch_id", sa.BigInteger(),
                  sa.ForeignKey("grmu_branch.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.PrimaryKeyConstraint("branch_id", "kind", "day"),
    )
    op.create_table(
        "grs_model_upload",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("branch_id", sa.BigInteger(),
                  sa.ForeignKey("grmu_branch.id", ondelete="CASCADE"), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("layout", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("first_day", sa.Date(), nullable=False),
        sa.Column("last_day", sa.Date(), nullable=False),
        sa.Column("days", sa.Integer(), nullable=False),
        sa.Column("stations", sa.Integer(), nullable=False),
        sa.Column("added", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unchanged", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("uploaded_at", sa.DateTime(), nullable=False),
        sa.Column("uploaded_by", sa.String(length=255), nullable=True),
    )
    op.create_index("ix_grs_model_upload_branch", "grs_model_upload", ["branch_id", "uploaded_at"])
    op.create_table(
        "grs_model_fit",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("branch_id", sa.BigInteger(),
                  sa.ForeignKey("grmu_branch.id", ondelete="CASCADE"), nullable=False),
        sa.Column("trained_at", sa.DateTime(), nullable=False),
        sa.Column("trained_by", sa.String(length=255), nullable=True),
        sa.Column("first_day", sa.Date(), nullable=False),
        sa.Column("last_day", sa.Date(), nullable=False),
        sa.Column("summary", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default="{}"),
    )
    op.create_index("ix_grs_model_fit_branch", "grs_model_fit", ["branch_id", "trained_at"])


def downgrade() -> None:
    for table in ("grs_model_fit", "grs_model_upload", "grs_model_day",
                  "grs_model_volume", "grs_model_station"):
        op.drop_table(table)
