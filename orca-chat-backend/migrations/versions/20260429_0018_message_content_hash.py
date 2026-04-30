"""message content hash

Revision ID: 20260429_0018
Revises: 20260429_0017
Create Date: 2026-04-29 00:18:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260429_0018"
down_revision = "20260429_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("content_hash", sa.String(length=64), nullable=True))
    op.create_index(
        "ix_messages_sender_receiver_content_hash_created_at",
        "messages",
        ["sender_id", "receiver_id", "content_hash", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_messages_sender_receiver_content_hash_created_at", table_name="messages")
    op.drop_column("messages", "content_hash")
