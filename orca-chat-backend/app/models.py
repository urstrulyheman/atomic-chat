from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import CHAR, JSON, TypeDecorator

from app.database import Base
from app.utils.time import utc_now


def uuid_pk() -> uuid.UUID:
    return uuid.uuid4()


JsonType = JSON().with_variant(JSONB, "postgresql")


class GUID(TypeDecorator):
    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(UUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if dialect.name == "postgresql":
            return value
        return str(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(value)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        Index("uq_users_email", "email", unique=True),
        Index("ix_users_status_created_at", "status", "created_at"),
        Index("ix_users_role_status", "role", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid_pk)
    phone: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(255))
    name: Mapped[Optional[str]] = mapped_column(String(100))
    username: Mapped[Optional[str]] = mapped_column(String(50), unique=True)
    avatar_url: Mapped[Optional[str]] = mapped_column(Text)
    kyc_status: Mapped[str] = mapped_column(String(30), default="not_started")
    trust_score: Mapped[int] = mapped_column(default=50)
    role: Mapped[str] = mapped_column(String(30), default="user")
    status: Mapped[str] = mapped_column(String(30), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    wallet: Mapped["Wallet"] = relationship(back_populates="user", uselist=False)


class OtpChallenge(Base):
    __tablename__ = "otp_challenges"
    __table_args__ = (
        Index("ix_otp_challenges_phone_created_at", "phone", "created_at"),
        Index("ix_otp_challenges_phone_status", "phone", "status"),
        Index("ix_otp_challenges_ip_created_at", "ip_address", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid_pk)
    phone: Mapped[str] = mapped_column(String(20), index=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(80))
    otp_code: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(30), default="pending")
    attempts: Mapped[int] = mapped_column(default=0)
    send_count: Mapped[int] = mapped_column(default=1)
    last_sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    __table_args__ = (
        Index("ix_auth_sessions_user_status_last_seen", "user_id", "status", "last_seen_at"),
        Index("ix_auth_sessions_ip_created_at", "ip_address", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid_pk)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"))
    jti: Mapped[str] = mapped_column(String(80), unique=True)
    device_label: Mapped[Optional[str]] = mapped_column(String(120))
    user_agent: Mapped[Optional[str]] = mapped_column(Text)
    ip_address: Mapped[Optional[str]] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(30), default="active")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Wallet(Base):
    __tablename__ = "wallets"
    __table_args__ = (
        CheckConstraint("purchased_balance >= 0", name="ck_wallets_purchased_balance_non_negative"),
        CheckConstraint("earned_balance >= 0", name="ck_wallets_earned_balance_non_negative"),
        CheckConstraint("locked_balance >= 0", name="ck_wallets_locked_balance_non_negative"),
        CheckConstraint("gas_paid_total >= 0", name="ck_wallets_gas_paid_total_non_negative"),
        CheckConstraint("reward_earned_total >= 0", name="ck_wallets_reward_earned_total_non_negative"),
        Index("ix_wallets_wallet_type", "wallet_type"),
        Index("ix_wallets_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid_pk)
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(GUID(), ForeignKey("users.id"), unique=True)
    wallet_type: Mapped[str] = mapped_column(String(30), default="user")
    purchased_balance: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    earned_balance: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    locked_balance: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    gas_paid_total: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    reward_earned_total: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    status: Mapped[str] = mapped_column(String(30), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    user: Mapped[Optional[User]] = relationship(back_populates="wallet")


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_created_at", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid_pk)
    conversation_type: Mapped[str] = mapped_column(String(30), default="direct")
    created_by: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ConversationMember(Base):
    __tablename__ = "conversation_members"
    __table_args__ = (
        UniqueConstraint("conversation_id", "user_id"),
        Index("ix_conversation_members_user_joined", "user_id", "joined_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid_pk)
    conversation_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("conversations.id"))
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"))
    role: Mapped[str] = mapped_column(String(30), default="member")
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("sender_id", "idempotency_key"),
        CheckConstraint("coin_cost >= 0", name="ck_messages_coin_cost_non_negative"),
        Index("ix_messages_conversation_created_at", "conversation_id", "created_at"),
        Index("ix_messages_sender_created_at", "sender_id", "created_at"),
        Index("ix_messages_receiver_created_at", "receiver_id", "created_at"),
        Index("ix_messages_transaction_id", "transaction_id"),
        Index("ix_messages_sender_receiver_content_hash_created_at", "sender_id", "receiver_id", "content_hash", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid_pk)
    conversation_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("conversations.id"))
    sender_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"))
    receiver_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"))
    message_type: Mapped[str] = mapped_column(String(30), default="text")
    encrypted_content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[Optional[str]] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default="sent")
    coin_cost: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    transaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(GUID())
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(120))
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class LedgerTransaction(Base):
    __tablename__ = "ledger_transactions"
    __table_args__ = (
        CheckConstraint("gross_amount > 0", name="ck_ledger_transactions_gross_amount_positive"),
        CheckConstraint("platform_gas >= 0", name="ck_ledger_transactions_platform_gas_non_negative"),
        CheckConstraint("receiver_reward >= 0", name="ck_ledger_transactions_receiver_reward_non_negative"),
        CheckConstraint("reserve_amount >= 0", name="ck_ledger_transactions_reserve_amount_non_negative"),
        Index("ix_ledger_transactions_created_at", "created_at"),
        Index("ix_ledger_transactions_from_created_at", "from_wallet_id", "created_at"),
        Index("ix_ledger_transactions_to_created_at", "to_wallet_id", "created_at"),
        Index("ix_ledger_transactions_type_created_at", "transaction_type", "created_at"),
        Index("uq_ledger_transactions_from_type_idempotency_key", "from_wallet_id", "transaction_type", "idempotency_key", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid_pk)
    transaction_type: Mapped[str] = mapped_column(String(50), nullable=False)
    reference_id: Mapped[Optional[uuid.UUID]] = mapped_column(GUID())
    from_wallet_id: Mapped[Optional[uuid.UUID]] = mapped_column(GUID())
    to_wallet_id: Mapped[Optional[uuid.UUID]] = mapped_column(GUID())
    gross_amount: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    platform_gas: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    receiver_reward: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    reserve_amount: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    status: Mapped[str] = mapped_column(String(30), default="settled")
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(120))
    transaction_metadata: Mapped[Optional[dict]] = mapped_column("metadata", JsonType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class WalletEntry(Base):
    __tablename__ = "wallet_entries"
    __table_args__ = (
        CheckConstraint("entry_type IN ('DEBIT', 'CREDIT')", name="ck_wallet_entries_entry_type_valid"),
        CheckConstraint("amount > 0", name="ck_wallet_entries_amount_positive"),
        Index("ix_wallet_entries_wallet_created_at", "wallet_id", "created_at"),
        Index("ix_wallet_entries_transaction_type", "transaction_id", "entry_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid_pk)
    transaction_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("ledger_transactions.id"))
    wallet_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("wallets.id"))
    entry_type: Mapped[str] = mapped_column(String(20), nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    balance_type: Mapped[Optional[str]] = mapped_column(String(30))
    description: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class PaymentOrder(Base):
    __tablename__ = "payment_orders"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_payment_orders_amount_positive"),
        CheckConstraint("coins_to_credit > 0", name="ck_payment_orders_coins_to_credit_positive"),
        Index("ix_payment_orders_user_created_at", "user_id", "created_at"),
        Index("ix_payment_orders_gateway_order_id", "gateway_order_id"),
        Index("uq_payment_orders_gateway_order_id", "gateway_order_id", unique=True),
        Index("ix_payment_orders_status_created_at", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid_pk)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"))
    gateway: Mapped[str] = mapped_column(String(50), default="razorpay")
    gateway_order_id: Mapped[Optional[str]] = mapped_column(String(255))
    gateway_payment_id: Mapped[Optional[str]] = mapped_column(String(255), unique=True)
    amount: Mapped[float] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(String(10), default="INR")
    coins_to_credit: Mapped[float] = mapped_column(Numeric(18, 6))
    status: Mapped[str] = mapped_column(String(30), default="created")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class RewardEvent(Base):
    __tablename__ = "reward_events"
    __table_args__ = (
        CheckConstraint("base_reward >= 0", name="ck_reward_events_base_reward_non_negative"),
        CheckConstraint("final_reward >= 0", name="ck_reward_events_final_reward_non_negative"),
        CheckConstraint("trust_multiplier >= 0", name="ck_reward_events_trust_multiplier_non_negative"),
        CheckConstraint("fraud_multiplier >= 0", name="ck_reward_events_fraud_multiplier_non_negative"),
        Index("ix_reward_events_status_lock_until", "status", "lock_until"),
        Index("ix_reward_events_user_status", "user_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid_pk)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"))
    source: Mapped[Optional[str]] = mapped_column(String(50))
    reference_id: Mapped[Optional[uuid.UUID]] = mapped_column(GUID())
    base_reward: Mapped[float] = mapped_column(Numeric(18, 6))
    final_reward: Mapped[float] = mapped_column(Numeric(18, 6))
    trust_multiplier: Mapped[float] = mapped_column(Numeric(8, 4))
    fraud_multiplier: Mapped[float] = mapped_column(Numeric(8, 4))
    lock_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(30), default="locked")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class FraudEvent(Base):
    __tablename__ = "fraud_events"
    __table_args__ = (
        Index("ix_fraud_events_status_created_at", "status", "created_at"),
        Index("ix_fraud_events_user_created_at", "user_id", "created_at"),
        Index("ix_fraud_events_type_created_at", "event_type", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid_pk)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"))
    event_type: Mapped[str] = mapped_column(String(100))
    severity: Mapped[str] = mapped_column(String(30))
    event_metadata: Mapped[Optional[dict]] = mapped_column("metadata", JsonType)
    status: Mapped[str] = mapped_column(String(30), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class UserBlock(Base):
    __tablename__ = "user_blocks"
    __table_args__ = (
        UniqueConstraint("blocker_id", "blocked_id"),
        CheckConstraint("blocker_id <> blocked_id", name="ck_user_blocks_no_self_block"),
        Index("ix_user_blocks_blocker_status", "blocker_id", "status"),
        Index("ix_user_blocks_blocked_status", "blocked_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid_pk)
    blocker_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"))
    blocked_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(30), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class MessageReport(Base):
    __tablename__ = "message_reports"
    __table_args__ = (
        UniqueConstraint("message_id", "reporter_id"),
        Index("ix_message_reports_status_created_at", "status", "created_at"),
        Index("ix_message_reports_reported_user_created_at", "reported_user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid_pk)
    message_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("messages.id"))
    reporter_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"))
    reported_user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"))
    reason: Mapped[str] = mapped_column(String(100))
    description: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class SettlementHash(Base):
    __tablename__ = "settlement_hashes"
    __table_args__ = (
        Index("ix_settlement_hashes_status_created_at", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid_pk)
    settlement_date: Mapped[date] = mapped_column(Date, unique=True)
    ledger_hash: Mapped[str] = mapped_column(String(64))
    transaction_count: Mapped[int] = mapped_column(default=0)
    entry_count: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String(30), default="generated")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class AdminAuditLog(Base):
    __tablename__ = "admin_audit_logs"
    __table_args__ = (
        Index("ix_admin_audit_logs_admin_created_at", "admin_user_id", "created_at"),
        Index("ix_admin_audit_logs_action_created_at", "action", "created_at"),
        Index("ix_admin_audit_logs_target_created_at", "target_type", "target_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid_pk)
    admin_user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(100))
    target_type: Mapped[str] = mapped_column(String(50))
    target_id: Mapped[Optional[uuid.UUID]] = mapped_column(GUID())
    audit_metadata: Mapped[Optional[dict]] = mapped_column("metadata", JsonType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
