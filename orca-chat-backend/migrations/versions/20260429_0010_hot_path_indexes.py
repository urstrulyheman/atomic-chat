"""hot path indexes

Revision ID: 20260429_0010
Revises: 20260428_0009
Create Date: 2026-04-29
"""

from typing import Sequence, Union

from alembic import op

revision: str = "20260429_0010"
down_revision: Union[str, Sequence[str], None] = "20260428_0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


indexes = [
    ("ix_users_status_created_at", "users", ["status", "created_at"]),
    ("ix_users_role_status", "users", ["role", "status"]),
    ("ix_otp_challenges_phone_status", "otp_challenges", ["phone", "status"]),
    ("ix_auth_sessions_user_status_last_seen", "auth_sessions", ["user_id", "status", "last_seen_at"]),
    ("ix_auth_sessions_ip_created_at", "auth_sessions", ["ip_address", "created_at"]),
    ("ix_wallets_wallet_type", "wallets", ["wallet_type"]),
    ("ix_wallets_status", "wallets", ["status"]),
    ("ix_conversations_created_at", "conversations", ["created_at"]),
    ("ix_conversation_members_user_joined", "conversation_members", ["user_id", "joined_at"]),
    ("ix_messages_conversation_created_at", "messages", ["conversation_id", "created_at"]),
    ("ix_messages_sender_created_at", "messages", ["sender_id", "created_at"]),
    ("ix_messages_receiver_created_at", "messages", ["receiver_id", "created_at"]),
    ("ix_messages_transaction_id", "messages", ["transaction_id"]),
    ("ix_ledger_transactions_created_at", "ledger_transactions", ["created_at"]),
    ("ix_ledger_transactions_from_created_at", "ledger_transactions", ["from_wallet_id", "created_at"]),
    ("ix_ledger_transactions_to_created_at", "ledger_transactions", ["to_wallet_id", "created_at"]),
    ("ix_ledger_transactions_type_created_at", "ledger_transactions", ["transaction_type", "created_at"]),
    ("ix_wallet_entries_wallet_created_at", "wallet_entries", ["wallet_id", "created_at"]),
    ("ix_wallet_entries_transaction_type", "wallet_entries", ["transaction_id", "entry_type"]),
    ("ix_payment_orders_user_created_at", "payment_orders", ["user_id", "created_at"]),
    ("ix_payment_orders_gateway_order_id", "payment_orders", ["gateway_order_id"]),
    ("ix_payment_orders_status_created_at", "payment_orders", ["status", "created_at"]),
    ("ix_reward_events_status_lock_until", "reward_events", ["status", "lock_until"]),
    ("ix_reward_events_user_status", "reward_events", ["user_id", "status"]),
    ("ix_fraud_events_status_created_at", "fraud_events", ["status", "created_at"]),
    ("ix_fraud_events_user_created_at", "fraud_events", ["user_id", "created_at"]),
    ("ix_fraud_events_type_created_at", "fraud_events", ["event_type", "created_at"]),
    ("ix_user_blocks_blocker_status", "user_blocks", ["blocker_id", "status"]),
    ("ix_user_blocks_blocked_status", "user_blocks", ["blocked_id", "status"]),
    ("ix_message_reports_status_created_at", "message_reports", ["status", "created_at"]),
    ("ix_message_reports_reported_user_created_at", "message_reports", ["reported_user_id", "created_at"]),
]


def upgrade() -> None:
    for index_name, table_name, columns in indexes:
        op.create_index(index_name, table_name, columns)


def downgrade() -> None:
    for index_name, table_name, _columns in reversed(indexes):
        op.drop_index(index_name, table_name=table_name)
