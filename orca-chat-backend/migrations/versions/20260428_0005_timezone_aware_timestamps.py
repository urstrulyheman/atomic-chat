"""timezone aware timestamps

Revision ID: 20260428_0005
Revises: 20260428_0004
Create Date: 2026-04-28
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260428_0005"
down_revision: Union[str, Sequence[str], None] = "20260428_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


timestamp_columns = {
    "users": ["created_at", "updated_at"],
    "otp_challenges": ["created_at", "last_sent_at", "expires_at"],
    "wallets": ["created_at", "updated_at"],
    "conversations": ["created_at"],
    "conversation_members": ["joined_at"],
    "messages": ["created_at"],
    "ledger_transactions": ["created_at"],
    "wallet_entries": ["created_at"],
    "payment_orders": ["created_at", "updated_at"],
    "reward_events": ["created_at", "lock_until"],
    "fraud_events": ["created_at"],
}


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    for table_name, columns in timestamp_columns.items():
        for column_name in columns:
            op.alter_column(
                table_name,
                column_name,
                existing_type=sa.DateTime(),
                type_=sa.DateTime(timezone=True),
                postgresql_using=f"{column_name} AT TIME ZONE 'UTC'",
            )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    for table_name, columns in timestamp_columns.items():
        for column_name in columns:
            op.alter_column(
                table_name,
                column_name,
                existing_type=sa.DateTime(timezone=True),
                type_=sa.DateTime(),
                postgresql_using=f"{column_name} AT TIME ZONE 'UTC'",
            )
