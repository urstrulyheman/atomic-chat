"""auth session device metadata

Revision ID: 20260428_0009
Revises: 20260428_0008
Create Date: 2026-04-28
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260428_0009"
down_revision: Union[str, Sequence[str], None] = "20260428_0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("auth_sessions", sa.Column("device_label", sa.String(120), nullable=True))
    op.add_column("auth_sessions", sa.Column("user_agent", sa.Text(), nullable=True))
    op.add_column("auth_sessions", sa.Column("ip_address", sa.String(80), nullable=True))
    op.add_column("auth_sessions", sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE auth_sessions SET last_seen_at = created_at WHERE last_seen_at IS NULL")
    if op.get_bind().dialect.name == "postgresql":
        op.alter_column("auth_sessions", "last_seen_at", nullable=False)


def downgrade() -> None:
    op.drop_column("auth_sessions", "last_seen_at")
    op.drop_column("auth_sessions", "ip_address")
    op.drop_column("auth_sessions", "user_agent")
    op.drop_column("auth_sessions", "device_label")
