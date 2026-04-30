"""initial schema

Revision ID: 20260428_0001
Revises:
Create Date: 2026-04-28
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260428_0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


uuid_type = postgresql.UUID(as_uuid=True)
json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("phone", sa.String(20), nullable=False, unique=True),
        sa.Column("email", sa.String(255)),
        sa.Column("name", sa.String(100)),
        sa.Column("username", sa.String(50), unique=True),
        sa.Column("avatar_url", sa.Text()),
        sa.Column("kyc_status", sa.String(30), nullable=False, server_default="not_started"),
        sa.Column("trust_score", sa.Integer(), nullable=False, server_default="50"),
        sa.Column("status", sa.String(30), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "otp_challenges",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("phone", sa.String(20), nullable=False, index=True),
        sa.Column("otp_code", sa.String(10), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "wallets",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("user_id", uuid_type, sa.ForeignKey("users.id"), unique=True),
        sa.Column("wallet_type", sa.String(30), nullable=False, server_default="user"),
        sa.Column("purchased_balance", sa.Numeric(18, 6), nullable=False, server_default="0"),
        sa.Column("earned_balance", sa.Numeric(18, 6), nullable=False, server_default="0"),
        sa.Column("locked_balance", sa.Numeric(18, 6), nullable=False, server_default="0"),
        sa.Column("gas_paid_total", sa.Numeric(18, 6), nullable=False, server_default="0"),
        sa.Column("reward_earned_total", sa.Numeric(18, 6), nullable=False, server_default="0"),
        sa.Column("status", sa.String(30), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "conversations",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("conversation_type", sa.String(30), nullable=False, server_default="direct"),
        sa.Column("created_by", uuid_type, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "conversation_members",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("conversation_id", uuid_type, sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("user_id", uuid_type, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("role", sa.String(30), nullable=False, server_default="member"),
        sa.Column("joined_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("conversation_id", "user_id"),
    )
    op.create_table(
        "messages",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("conversation_id", uuid_type, sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("sender_id", uuid_type, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("receiver_id", uuid_type, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("message_type", sa.String(30), nullable=False, server_default="text"),
        sa.Column("encrypted_content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="sent"),
        sa.Column("coin_cost", sa.Numeric(18, 6), nullable=False, server_default="0"),
        sa.Column("transaction_id", uuid_type),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "ledger_transactions",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("transaction_type", sa.String(50), nullable=False),
        sa.Column("reference_id", uuid_type),
        sa.Column("from_wallet_id", uuid_type),
        sa.Column("to_wallet_id", uuid_type),
        sa.Column("gross_amount", sa.Numeric(18, 6), nullable=False),
        sa.Column("platform_gas", sa.Numeric(18, 6), nullable=False, server_default="0"),
        sa.Column("receiver_reward", sa.Numeric(18, 6), nullable=False, server_default="0"),
        sa.Column("reserve_amount", sa.Numeric(18, 6), nullable=False, server_default="0"),
        sa.Column("status", sa.String(30), nullable=False, server_default="settled"),
        sa.Column("metadata", json_type),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "wallet_entries",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("transaction_id", uuid_type, sa.ForeignKey("ledger_transactions.id"), nullable=False),
        sa.Column("wallet_id", uuid_type, sa.ForeignKey("wallets.id"), nullable=False),
        sa.Column("entry_type", sa.String(20), nullable=False),
        sa.Column("amount", sa.Numeric(18, 6), nullable=False),
        sa.Column("balance_type", sa.String(30)),
        sa.Column("description", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "payment_orders",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("user_id", uuid_type, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("gateway", sa.String(50), nullable=False, server_default="razorpay"),
        sa.Column("gateway_order_id", sa.String(255)),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("currency", sa.String(10), nullable=False, server_default="INR"),
        sa.Column("coins_to_credit", sa.Numeric(18, 6), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="created"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "reward_events",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("user_id", uuid_type, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("source", sa.String(50)),
        sa.Column("reference_id", uuid_type),
        sa.Column("base_reward", sa.Numeric(18, 6), nullable=False),
        sa.Column("final_reward", sa.Numeric(18, 6), nullable=False),
        sa.Column("trust_multiplier", sa.Numeric(8, 4), nullable=False),
        sa.Column("fraud_multiplier", sa.Numeric(8, 4), nullable=False),
        sa.Column("lock_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="locked"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "fraud_events",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("user_id", uuid_type, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("severity", sa.String(30), nullable=False),
        sa.Column("metadata", json_type),
        sa.Column("status", sa.String(30), nullable=False, server_default="open"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("fraud_events")
    op.drop_table("reward_events")
    op.drop_table("payment_orders")
    op.drop_table("wallet_entries")
    op.drop_table("ledger_transactions")
    op.drop_table("messages")
    op.drop_table("conversation_members")
    op.drop_table("conversations")
    op.drop_table("wallets")
    op.drop_table("otp_challenges")
    op.drop_table("users")
