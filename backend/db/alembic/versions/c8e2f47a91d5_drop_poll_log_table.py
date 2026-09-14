"""Drop poll_log

The same account of the same phone call was written twice: into this table for
the screen that follows the call, and into a file per site for the question
asked afterwards. The table was also wiped at the start of every session, so
five minutes later it held nothing at all. The screen now reads the file,
numbering its lines by position — the same "everything after line N" contract
it asked the table for.

Revision ID: c8e2f47a91d5
Revises: b4d1e6f80c73
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c8e2f47a91d5"
down_revision = "b4d1e6f80c73"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("idx_poll_log_device_seq", table_name="poll_log")
    op.drop_table("poll_log")


def downgrade() -> None:
    op.create_table(
        "poll_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("poll_device_id", sa.BigInteger(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ts", postgresql.TIMESTAMP(), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("level", sa.String(length=8), nullable=False,
                  server_default="info"),
        sa.Column("message", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["poll_device_id"], ["poll_device.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_poll_log_device_seq", "poll_log",
                    ["poll_device_id", "seq"])
