"""message receipt timestamps

Revision ID: 20260429_0015
Revises: 20260429_0014
Create Date: 2026-04-29 00:15:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260429_0015"
down_revision = "20260429_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("messages", sa.Column("read_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "read_at")
    op.drop_column("messages", "delivered_at")
