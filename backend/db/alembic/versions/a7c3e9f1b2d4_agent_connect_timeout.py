"""Connect timeout per agent

How long a dialled call may take to connect depends on the calling side — the
modem and the operator of its SIM — so it is set on the agent. NULL means the
server's default (DEFAULT_CONNECT_TIMEOUT_SEC in polling_agent_ep); the card's
own value can only lengthen it.

Revision ID: a7c3e9f1b2d4
Revises: 331122169b34
Create Date: 2026-11-02
"""
import sqlalchemy as sa
from alembic import op

revision = "a7c3e9f1b2d4"
down_revision = "331122169b34"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "poll_agent",
        sa.Column("modem_connect_timeout_sec", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("poll_agent", "modem_connect_timeout_sec")
