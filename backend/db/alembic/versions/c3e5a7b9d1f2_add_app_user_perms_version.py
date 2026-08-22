"""Session invalidation on a rights change: app_user.perms_version.

The JWT carries the user's role and allowed branches so per-request auth does
not have to hit the database. That made a rights change invisible to a session
already logged in: a token issued before the change keeps its old claims for up
to 30 days (remember-me), so a demoted admin kept write access at the central
guard and a promoted viewer was shown the admin panel but rejected on every
save.

perms_version is stamped into the token at login and re-checked per request.
Bumping it on the change invalidates every session the account already has.

Revision ID: c3e5a7b9d1f2
Revises: b2d4f6a8c0e1
Create Date: 2026-08-22

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3e5a7b9d1f2"
down_revision: Union[str, None] = "b2d4f6a8c0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default so existing rows get 1 — the same value a token without the
    # claim is read as, which keeps everyone logged in across the deploy.
    op.add_column(
        "app_user",
        sa.Column("perms_version", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_column("app_user", "perms_version")
