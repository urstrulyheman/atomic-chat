"""unique user email

Revision ID: 20260429_0013
Revises: 20260429_0012
Create Date: 2026-04-29 00:13:00.000000
"""

from alembic import op


revision = "20260429_0013"
down_revision = "20260429_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("uq_users_email", "users", ["email"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_users_email", table_name="users")
