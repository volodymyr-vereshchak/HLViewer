"""How far the running poll has got

A month of hourly records is seven hundred requests down a phone line, and
one record to a frame. Without a count the screen says "дзвоню" for ten
minutes, which reads as a screen that has hung.

Revision ID: a7c2e9f4b615
Revises: f5b3c8d1a204
"""
import sqlalchemy as sa
from alembic import op

revision = "a7c2e9f4b615"
down_revision = "f5b3c8d1a204"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("poll_device", sa.Column("progress_done", sa.Integer(), nullable=True))
    op.add_column("poll_device", sa.Column("progress_total", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("poll_device", "progress_total")
    op.drop_column("poll_device", "progress_done")
