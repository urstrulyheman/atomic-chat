"""admin audit logs

Revision ID: 20260429_0016
Revises: 20260429_0015
Create Date: 2026-04-29 00:16:00.000000
"""

from alembic import op
import sqlalchemy as sa

from app.models import GUID, JsonType


revision = "20260429_0016"
down_revision = "20260429_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_audit_logs",
        sa.Column("id", GUID(), nullable=False),
        sa.Column("admin_user_id", GUID(), nullable=False),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("target_type", sa.String(length=50), nullable=False),
        sa.Column("target_id", GUID(), nullable=True),
        sa.Column("metadata", JsonType, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["admin_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_admin_audit_logs_admin_created_at", "admin_audit_logs", ["admin_user_id", "created_at"])
    op.create_index("ix_admin_audit_logs_action_created_at", "admin_audit_logs", ["action", "created_at"])
    op.create_index("ix_admin_audit_logs_target_created_at", "admin_audit_logs", ["target_type", "target_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_admin_audit_logs_target_created_at", table_name="admin_audit_logs")
    op.drop_index("ix_admin_audit_logs_action_created_at", table_name="admin_audit_logs")
    op.drop_index("ix_admin_audit_logs_admin_created_at", table_name="admin_audit_logs")
    op.drop_table("admin_audit_logs")
