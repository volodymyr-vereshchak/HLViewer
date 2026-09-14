"""Drop poll_agent.kind

«АРМ оператора» or «Виділена машина» — stored, shown in a select, and read by
nothing. A field that decides nothing still has to be understood by everybody
who meets it, and the first question it got was what the difference was. There
is none: the machine is identified by its name.

Revision ID: b4d1e6f80c73
Revises: a7c2e9f4b615
"""
import sqlalchemy as sa
from alembic import op

revision = "b4d1e6f80c73"
down_revision = "a7c2e9f4b615"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("poll_agent", "kind")


def downgrade() -> None:
    op.add_column(
        "poll_agent",
        sa.Column("kind", sa.String(length=16), nullable=False,
                  server_default="workstation"),
    )
