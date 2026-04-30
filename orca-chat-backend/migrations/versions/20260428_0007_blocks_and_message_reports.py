"""blocks and message reports

Revision ID: 20260428_0007
Revises: 20260428_0006
Create Date: 2026-04-28
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260428_0007"
down_revision: Union[str, Sequence[str], None] = "20260428_0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

uuid_type = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "user_blocks",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("blocker_id", uuid_type, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("blocked_id", uuid_type, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("blocker_id", "blocked_id"),
        sa.CheckConstraint("blocker_id <> blocked_id", name="ck_user_blocks_no_self_block"),
    )
    op.create_table(
        "message_reports",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("message_id", uuid_type, sa.ForeignKey("messages.id"), nullable=False),
        sa.Column("reporter_id", uuid_type, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("reported_user_id", uuid_type, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("reason", sa.String(100), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("status", sa.String(30), nullable=False, server_default="open"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("message_id", "reporter_id"),
    )


def downgrade() -> None:
    op.drop_table("message_reports")
    op.drop_table("user_blocks")
