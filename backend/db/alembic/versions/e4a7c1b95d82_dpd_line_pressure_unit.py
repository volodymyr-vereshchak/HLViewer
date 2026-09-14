"""add pressure_unit to dpd_line

Revision ID: e4a7c1b95d82
Revises: d3f9a5b02e41
Create Date: 2026-09-13 16:20:00.000000

Which unit a DPD line's pressure is READ in — not what the archive holds. The
archive holds whatever each corrector reported, and this fleet reports both:
161k rows in МПа, 157k in кгс/см², and twelve devices report both within one
history, because a corrector swapped for one set differently leaves the
archive of two minds. The screen therefore converts, and this says to what.

NOT NULL with a default, like gas_volume_line.pressure_unit: every line has a
unit, so no reader — and no line form — has to carry a case for "not set".
кгс/см² because that is what the operators here read pressure in.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e4a7c1b95d82'
down_revision = 'd3f9a5b02e41'
branch_labels = None
depends_on = None


def upgrade():
    # server_default fills the existing rows in the same statement.
    op.add_column(
        'dpd_line',
        sa.Column('pressure_unit', sa.String(length=16), nullable=False,
                  server_default='кгс/см²'),
    )


def downgrade():
    op.drop_column('dpd_line', 'pressure_unit')
