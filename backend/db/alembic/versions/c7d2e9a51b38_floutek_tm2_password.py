"""Which corrector answered last time, and the password a Floutek ТМ-2 wants

Revision ID: c7d2e9a51b38
Revises: b6f3a8d40c27
Create Date: 2026-09-16

**The protocol is found on the call, not taken from the catalogue.** Every
family this agent reads names itself on the first question — a КПЛГ, a ВЕГА
and a Floutek alike — and none of them mistakes another's question for its
own; both were checked on live calls. So the corrector says what it is, and
the card remembers what it said: `detected_protocol` decides which question
the next call asks first, and `detected_model` is what the corrector called
itself, which is not always what the catalogue calls it (a card for a
"ТМ-2-3-6" answered "TM-2-3-4").

The catalogue's `protocol_id` becomes a hint for the very first call and
nothing more. A new model needs no driver typed in anywhere before it can be
polled.

**The password.** Every ТМ-2 archive request carries one. The fleet uses the
vendor's default "11", but it is a setting of the corrector and can be
changed, so it lives on the card with that default. Other models ignore it.
"""
from alembic import op
import sqlalchemy as sa

revision = "c7d2e9a51b38"
down_revision = "b6f3a8d40c27"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "poll_device",
        sa.Column("device_password", sa.String(length=16),
                  nullable=False, server_default="11"),
    )
    op.add_column(
        "poll_device", sa.Column("detected_protocol", sa.Integer(), nullable=True)
    )
    op.add_column(
        "poll_device", sa.Column("detected_model", sa.String(length=64), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("poll_device", "detected_model")
    op.drop_column("poll_device", "detected_protocol")
    op.drop_column("poll_device", "device_password")
