"""otp guardrails

Revision ID: 20260428_0003
Revises: 20260428_0002
Create Date: 2026-04-28
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260428_0003"
down_revision: Union[str, Sequence[str], None] = "20260428_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("otp_challenges", sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("otp_challenges", sa.Column("send_count", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("otp_challenges", sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("otp_challenges", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_otp_challenges_phone_created_at", "otp_challenges", ["phone", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_otp_challenges_phone_created_at", table_name="otp_challenges")
    op.drop_column("otp_challenges", "expires_at")
    op.drop_column("otp_challenges", "last_sent_at")
    op.drop_column("otp_challenges", "send_count")
    op.drop_column("otp_challenges", "attempts")
