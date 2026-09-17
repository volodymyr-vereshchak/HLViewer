"""hostlib archive log: which archives were taken in, kept for a day

Revision ID: d8e4b1f07a92
Revises: c7d2e9a51b38
Create Date: 2026-09-17

The poller's memory of the last processed archive moves out of its process:
it survived no restart and nobody could see it. Kept for a day only — the
source folders gain a new snapshot every hour, and processing one twice
inserts nothing new.
"""
from alembic import op
import sqlalchemy as sa

revision = "d8e4b1f07a92"
down_revision = "c7d2e9a51b38"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hostlib_archive_log",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("path", sa.String(length=512), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("file_mtime", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error", sa.String(length=2000), nullable=True),
        sa.Column("processed_at", sa.DateTime(), nullable=False),
    )
    op.create_index("idx_hostlib_archive_log_lookup", "hostlib_archive_log",
                    ["path", "filename", "size"])
    op.create_index("idx_hostlib_archive_log_processed", "hostlib_archive_log",
                    ["processed_at"])


def downgrade() -> None:
    op.drop_index("idx_hostlib_archive_log_processed", table_name="hostlib_archive_log")
    op.drop_index("idx_hostlib_archive_log_lookup", table_name="hostlib_archive_log")
    op.drop_table("hostlib_archive_log")
