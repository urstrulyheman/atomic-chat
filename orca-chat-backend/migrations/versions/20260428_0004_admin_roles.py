"""admin roles

Revision ID: 20260428_0004
Revises: 20260428_0003
Create Date: 2026-04-28
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260428_0004"
down_revision: Union[str, Sequence[str], None] = "20260428_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("role", sa.String(30), nullable=False, server_default="user"))


def downgrade() -> None:
    op.drop_column("users", "role")
