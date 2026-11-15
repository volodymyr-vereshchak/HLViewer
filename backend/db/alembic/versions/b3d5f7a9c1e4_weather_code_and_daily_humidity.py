"""Weather code (WMO) by day and hour, daily mean humidity

What the forecast pictures on the weather page are drawn from: the code says
clear, cloudy, drizzle, shower, snow or thunderstorm. Rows downloaded before
have none; the page then derives the picture from cloud, rain and temperature.

Revision ID: b3d5f7a9c1e4
Revises: a2c4e6f8b0d1
Create Date: 2026-11-15
"""
import sqlalchemy as sa
from alembic import op

revision = "b3d5f7a9c1e4"
down_revision = "a2c4e6f8b0d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("weather_daily", sa.Column("humidity", sa.Float(), nullable=True))
    op.add_column("weather_daily", sa.Column("weather_code", sa.SmallInteger(), nullable=True))
    op.add_column("weather_hourly", sa.Column("weather_code", sa.SmallInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("weather_hourly", "weather_code")
    op.drop_column("weather_daily", "weather_code")
    op.drop_column("weather_daily", "humidity")
