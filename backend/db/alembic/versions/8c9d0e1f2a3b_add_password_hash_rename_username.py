"""rename windows_username to username, add password_hash

Revision ID: 8c9d0e1f2a3b
Revises: 7a8b9c0d1e2f
Create Date: 2026-03-21 00:00:00.000000

Switches from Windows Auth to JWT login/password auth:
- renames app_user.windows_username → username
- adds app_user.password_hash (nullable, VARCHAR 255)
"""
from alembic import op
import sqlalchemy as sa


revision = '8c9d0e1f2a3b'
down_revision = 'f7a8b9c0d1e2'
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column('app_user', 'windows_username', new_column_name='username')
    op.add_column('app_user', sa.Column('password_hash', sa.String(255), nullable=True))


def downgrade():
    op.drop_column('app_user', 'password_hash')
    op.alter_column('app_user', 'username', new_column_name='windows_username')
