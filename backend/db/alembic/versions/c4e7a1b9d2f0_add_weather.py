"""Weather for the analytics: points on the map, their lines, daily and hourly rows

Revision ID: c4e7a1b9d2f0
Revises: b8d4f0a2c3e5
Create Date: 2026-11-08
"""
import sqlalchemy as sa
from alembic import op

revision = "c4e7a1b9d2f0"
down_revision = "b8d4f0a2c3e5"
branch_labels = None
depends_on = None


def _point_fk() -> sa.Column:
    return sa.Column(
        "point_id", sa.BigInteger(),
        sa.ForeignKey("weather_point.id", ondelete="CASCADE"), nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        "weather_point",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column(
            "branch_id", sa.BigInteger(),
            sa.ForeignKey("grmu_branch.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.UniqueConstraint("branch_id", "name", name="uq_weather_point_branch_name"),
    )
    op.create_index("ix_weather_point_branch", "weather_point", ["branch_id"])

    op.create_table(
        "weather_point_line",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        _point_fk(),
        sa.Column("line_kind", sa.String(length=16), nullable=False),
        sa.Column("line_id", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("line_id", name="uq_weather_point_line_line"),
    )
    op.create_index("ix_weather_point_line_point", "weather_point_line", ["point_id"])

    op.create_table(
        "weather_daily",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        _point_fk(),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("t_mean", sa.Float(), nullable=True),
        sa.Column("t_min", sa.Float(), nullable=True),
        sa.Column("t_max", sa.Float(), nullable=True),
        sa.Column("precipitation", sa.Float(), nullable=True),
        sa.Column("wind_max", sa.Float(), nullable=True),
        sa.Column("radiation", sa.Float(), nullable=True),
        sa.UniqueConstraint("point_id", "day", name="uq_weather_daily_point_day"),
    )

    op.create_table(
        "weather_hourly",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        _point_fk(),
        sa.Column("stamp", sa.DateTime(), nullable=False),
        sa.Column("temperature", sa.Float(), nullable=True),
        sa.Column("humidity", sa.Float(), nullable=True),
        sa.Column("wind_speed", sa.Float(), nullable=True),
        sa.Column("radiation", sa.Float(), nullable=True),
        sa.Column("precipitation", sa.Float(), nullable=True),
        sa.Column("cloud_cover", sa.Float(), nullable=True),
        sa.UniqueConstraint("point_id", "stamp", name="uq_weather_hourly_point_stamp"),
    )


def downgrade() -> None:
    op.drop_table("weather_hourly")
    op.drop_table("weather_daily")
    op.drop_index("ix_weather_point_line_point", table_name="weather_point_line")
    op.drop_table("weather_point_line")
    op.drop_index("ix_weather_point_branch", table_name="weather_point")
    op.drop_table("weather_point")
