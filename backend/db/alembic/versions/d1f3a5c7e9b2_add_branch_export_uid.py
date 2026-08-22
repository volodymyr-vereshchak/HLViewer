"""Cross-installation identity of a branch: grmu_branch.export_uid.

A branch's configuration is carried to the central server as a JSON bundle
(services/branch_config_transfer.py). The bundle needs a key that means "this
same branch" on a different database, and `name` cannot be it: it is unique per
installation, so a rename at the source would arrive as a SECOND branch, while
two genuinely different branches sharing a name would merge into one.

The uid is minted once here (gen_random_uuid() is built into Postgres 13+) and
travels with every export. Import matches uid → name → create, and adopts the
file's uid onto a row it matched by name, so the next transfer matches directly.

Revision ID: d1f3a5c7e9b2
Revises: c3e5a7b9d1f2
Create Date: 2026-08-22

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d1f3a5c7e9b2"
down_revision: Union[str, None] = "c3e5a7b9d1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default gives every existing branch a uid in the same statement;
    # it stays on the column so a row inserted by raw SQL also gets one.
    op.add_column(
        "grmu_branch",
        sa.Column(
            "export_uid",
            sa.Uuid(),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
    )
    op.create_unique_constraint(
        "uq_grmu_branch_export_uid", "grmu_branch", ["export_uid"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_grmu_branch_export_uid", "grmu_branch", type_="unique")
    op.drop_column("grmu_branch", "export_uid")
