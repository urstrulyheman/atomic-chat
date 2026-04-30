"""otp ip rate limit

Revision ID: 20260429_0012
Revises: 20260429_0011
Create Date: 2026-04-29
"""

from alembic import op
import sqlalchemy as sa


revision = "20260429_0012"
down_revision = "20260429_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("otp_challenges", sa.Column("ip_address", sa.String(length=80), nullable=True))
    op.create_index("ix_otp_challenges_ip_created_at", "otp_challenges", ["ip_address", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_otp_challenges_ip_created_at", table_name="otp_challenges")
    op.drop_column("otp_challenges", "ip_address")
