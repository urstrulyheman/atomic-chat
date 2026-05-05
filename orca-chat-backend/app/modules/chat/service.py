import re
from hashlib import sha256
from datetime import timedelta
from decimal import Decimal
from math import ceil
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Conversation, ConversationMember, FraudEvent, Message, MessageReport, RewardEvent, User, UserBlock
from app.modules.fraud.service import is_fraud_risk, record_fraud_event
from app.modules.rewards.service import cap_reward_to_daily_limit, reward_lock_until
from app.utils.encryption import decrypt_message, encrypt_message
from app.utils.ledger import (
    PLATFORM_WALLET,
    RESERVE_WALLET,
    create_transaction,
    credit_wallet,
    debit_spendable,
    get_system_wallet,
    lock_wallets,
    money,
    spendable_balance,
)
from app.utils.time import utc_now


def get_or_create_direct_conversation(db: Session, sender: User, receiver_id: UUID) -> Conversation:
    if sender.id == receiver_id:
        raise HTTPException(status_code=400, detail="Cannot create a chat with yourself")
    receiver = db.get(User, receiver_id)
    if receiver is None:
        raise HTTPException(status_code=404, detail="Receiver not found")
    ensure_receiver_can_chat(receiver)
    if is_blocked_between(db, sender.id, receiver.id):
        raise HTTPException(status_code=403, detail="Communication is blocked")

    sender_conversation_ids = select(ConversationMember.conversation_id).where(
        ConversationMember.user_id == sender.id
    )
    conversation = (
        db.query(Conversation)
        .join(ConversationMember, ConversationMember.conversation_id == Conversation.id)
        .filter(
            Conversation.conversation_type == "direct",
            Conversation.id.in_(sender_conversation_ids),
            ConversationMember.user_id == receiver.id,
        )
        .order_by(Conversation.created_at.asc())
        .first()
    )
    if conversation:
        return conversation

    conversation = Conversation(created_by=sender.id)
    db.add(conversation)
    db.flush()
    db.add_all([
        ConversationMember(conversation_id=conversation.id, user_id=sender.id),
        ConversationMember(conversation_id=conversation.id, user_id=receiver.id),
    ])
    db.flush()
    return conversation


def send_paid_message(
    db: Session,
    sender: User,
    conversation_id: UUID,
    receiver_id: UUID,
    content: str,
    idempotency_key: str | None = None,
) -> Message:
    return send_message(db, sender, conversation_id, receiver_id, content, idempotency_key, use_free_quota=False)


def send_message(
    db: Session,
    sender: User,
    conversation_id: UUID,
    receiver_id: UUID,
    content: str,
    idempotency_key: str | None = None,
    use_free_quota: bool = False,
) -> Message:
    try:
        message = _send_message(db, sender, conversation_id, receiver_id, content, idempotency_key, use_free_quota)
        db.commit()
        db.refresh(message)
        return message
    except Exception:
        db.rollback()
        raise


def _send_message(
    db: Session,
    sender: User,
    conversation_id: UUID,
    receiver_id: UUID,
    content: str,
    idempotency_key: str | None,
    use_free_quota: bool,
) -> Message:
    if not isinstance(content, str) or not content.strip():
        raise HTTPException(status_code=400, detail="Message content is required")
    content = content.strip()
    if len(content) > settings.message_max_content_length:
        raise HTTPException(status_code=413, detail="Message content too large")
    if sender.id == receiver_id:
        raise HTTPException(status_code=400, detail="Cannot send a message to yourself")
    if idempotency_key is not None:
        if not idempotency_key.strip():
            raise HTTPException(status_code=400, detail="Idempotency key cannot be blank")
        if len(idempotency_key) > settings.message_idempotency_key_max_length:
            raise HTTPException(status_code=400, detail="Idempotency key too large")
        if not re.fullmatch(settings.message_idempotency_key_pattern, idempotency_key):
            raise HTTPException(status_code=400, detail="Idempotency key has invalid format")

    if idempotency_key:
        existing = (
            db.query(Message)
            .filter(Message.sender_id == sender.id, Message.idempotency_key == idempotency_key)
            .one_or_none()
        )
        if existing:
            return existing

    conversation = db.get(Conversation, conversation_id)
    receiver = db.get(User, receiver_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if receiver is None or receiver.wallet is None:
        raise HTTPException(status_code=404, detail="Receiver not found")
    ensure_receiver_can_chat(receiver)
    if is_blocked_between(db, sender.id, receiver.id):
        raise HTTPException(status_code=403, detail="Communication is blocked")

    is_member = (
        db.query(ConversationMember)
        .filter(ConversationMember.conversation_id == conversation_id, ConversationMember.user_id == sender.id)
        .first()
    )
    receiver_member = (
        db.query(ConversationMember)
        .filter(ConversationMember.conversation_id == conversation_id, ConversationMember.user_id == receiver.id)
        .first()
    )
    if not is_member or not receiver_member:
        raise HTTPException(status_code=403, detail="Conversation access denied")
    enforce_message_velocity(db, sender)

    locked_wallets = lock_wallets(db, [sender.wallet.id, receiver.wallet.id])
    sender_wallet = locked_wallets[sender.wallet.id]
    receiver_wallet = locked_wallets[receiver.wallet.id]
    if sender_wallet.status != "active":
        raise HTTPException(status_code=423, detail="Wallet is not active")

    encrypted_content = encrypt_message(content)
    content_hash = message_content_hash(content)
    if use_free_quota and has_free_message_quota(db, sender):
        message = Message(
            conversation_id=conversation.id,
            sender_id=sender.id,
            receiver_id=receiver.id,
            encrypted_content=encrypted_content,
            content_hash=content_hash,
            coin_cost=Decimal("0.000000"),
            idempotency_key=idempotency_key,
        )
        db.add(message)
        db.flush()
        return message

    risky, reason = is_fraud_risk(db, sender, receiver, content_hash)

    pricing = calculate_message_pricing(content)
    message_cost = pricing["message_cost"]
    receiver_reward = pricing["receiver_reward"]
    platform_gas = pricing["platform_gas"]
    reserve_reward = pricing["reserve_reward"]
    reward_cap_applied = False
    if risky:
        receiver_reward = Decimal("0.000000")
        reserve_reward = message_cost - platform_gas
        record_fraud_event(db, sender, reason or "message_risk", metadata={"receiver_id": str(receiver.id)})
    else:
        capped_receiver_reward = cap_reward_to_daily_limit(db, receiver, receiver_reward)
        reward_cap_applied = capped_receiver_reward < receiver_reward
        receiver_reward = capped_receiver_reward
        reserve_reward = message_cost - platform_gas - receiver_reward

    transaction = create_transaction(
        db,
        transaction_type="message_send",
        from_wallet_id=sender_wallet.id,
        to_wallet_id=receiver_wallet.id,
        gross_amount=message_cost,
        platform_gas=platform_gas,
        receiver_reward=receiver_reward,
        reserve_amount=reserve_reward,
        metadata={
            "fraud_risk": risky,
            "fraud_reason": reason,
            "reward_cap_applied": reward_cap_applied,
            "pricing_model": "token_units",
            "billing_token_count": pricing["token_count"],
            "billing_units": pricing["billing_units"],
            "tokens_per_unit": settings.message_billing_tokens_per_unit,
        },
    )

    debit_spendable(db, sender_wallet, message_cost, transaction, "Paid message")
    sender_wallet.gas_paid_total = money(sender_wallet.gas_paid_total) + platform_gas

    if receiver_reward > 0:
        credit_wallet(db, receiver_wallet, receiver_reward, "locked", transaction, "Paid message reward")
        db.add(RewardEvent(
            user_id=receiver.id,
            source="message",
            reference_id=transaction.id,
            base_reward=pricing["receiver_reward"],
            final_reward=receiver_reward,
            trust_multiplier=Decimal("1.0000"),
            fraud_multiplier=Decimal("1.0000"),
            lock_until=reward_lock_until(),
        ))

    platform_wallet = get_system_wallet(db, PLATFORM_WALLET)
    reserve_wallet = get_system_wallet(db, RESERVE_WALLET)
    credit_wallet(db, platform_wallet, platform_gas, "gas", transaction, "Platform gas")
    credit_wallet(db, reserve_wallet, reserve_reward, "reserve", transaction, "Reserve allocation")

    message = Message(
        conversation_id=conversation.id,
        sender_id=sender.id,
        receiver_id=receiver.id,
        encrypted_content=encrypted_content,
        content_hash=content_hash,
        coin_cost=message_cost,
        transaction_id=transaction.id,
        idempotency_key=idempotency_key,
    )
    db.add(message)
    db.flush()
    return message


def estimate_message_tokens(content: str) -> int:
    normalized = " ".join(content.strip().split())
    if not normalized:
        return 0
    return max(1, ceil(len(normalized) / 4))


def calculate_message_pricing(content: str) -> dict:
    token_count = estimate_message_tokens(content)
    tokens_per_unit = max(1, settings.message_billing_tokens_per_unit)
    billing_units = max(1, ceil(token_count / tokens_per_unit))
    message_cost = money(settings.message_default_cost * Decimal(billing_units))
    platform_gas = money(message_cost * settings.message_platform_gas_percent)
    receiver_reward = money(message_cost * settings.message_receiver_reward_percent)
    reserve_reward = money(message_cost - platform_gas - receiver_reward)
    return {
        "token_count": token_count,
        "billing_units": billing_units,
        "message_cost": message_cost,
        "platform_gas": platform_gas,
        "receiver_reward": receiver_reward,
        "reserve_reward": reserve_reward,
    }


def message_price_quote(content: str, sender: User) -> dict:
    pricing = calculate_message_pricing(content)
    sender_spendable = spendable_balance(sender.wallet)
    return {
        "pricing_model": "token_units",
        "token_count": pricing["token_count"],
        "tokens_per_unit": settings.message_billing_tokens_per_unit,
        "billing_units": pricing["billing_units"],
        "message_cost": pricing["message_cost"],
        "receiver_reward": pricing["receiver_reward"],
        "platform_gas": pricing["platform_gas"],
        "reserve_reward": pricing["reserve_reward"],
        "spendable_balance": sender_spendable,
        "can_afford": sender_spendable >= pricing["message_cost"],
    }


def daily_free_message_limit(user: User) -> int:
    if user.kyc_status == "premium":
        return settings.premium_user_daily_free_messages
    if user.kyc_status in {"verified", "approved"}:
        return settings.verified_user_daily_free_messages
    return settings.new_user_daily_free_messages


def has_free_message_quota(db: Session, user: User) -> bool:
    today_start = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
    used = (
        db.query(Message)
        .filter(
            Message.sender_id == user.id,
            Message.coin_cost == 0,
            Message.created_at >= today_start,
        )
        .count()
    )
    return used < daily_free_message_limit(user)


def message_content_hash(content: str) -> str:
    normalized = " ".join(content.strip().lower().split())
    return sha256(normalized.encode("utf-8")).hexdigest()


def enforce_message_velocity(db: Session, sender: User) -> None:
    one_minute_ago = utc_now() - timedelta(minutes=1)
    recent_count = (
        db.query(Message)
        .filter(Message.sender_id == sender.id, Message.created_at >= one_minute_ago)
        .count()
    )
    if recent_count >= settings.message_max_sends_per_minute:
        raise HTTPException(status_code=429, detail="Message rate limit exceeded")


def ensure_receiver_can_chat(receiver: User) -> None:
    if receiver.status != "active":
        raise HTTPException(status_code=403, detail="Receiver is not active")


def is_blocked_between(db: Session, user_a_id: UUID, user_b_id: UUID) -> bool:
    return (
        db.query(UserBlock)
        .filter(
            UserBlock.status == "active",
            or_(
                and_(UserBlock.blocker_id == user_a_id, UserBlock.blocked_id == user_b_id),
                and_(UserBlock.blocker_id == user_b_id, UserBlock.blocked_id == user_a_id),
            ),
        )
        .first()
        is not None
    )


def conversation_detail(db: Session, current_user: User, conversation_id: UUID) -> dict:
    conversation = db.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    member = (
        db.query(ConversationMember)
        .filter(ConversationMember.conversation_id == conversation_id, ConversationMember.user_id == current_user.id)
        .first()
    )
    if member is None:
        raise HTTPException(status_code=403, detail="Conversation access denied")

    participants = (
        db.query(User)
        .join(ConversationMember, ConversationMember.user_id == User.id)
        .filter(ConversationMember.conversation_id == conversation_id)
        .order_by(ConversationMember.joined_at.asc())
        .all()
    )
    other_participants = [participant for participant in participants if participant.id != current_user.id]
    blocked_by_me = any(
        db.query(UserBlock)
        .filter(
            UserBlock.blocker_id == current_user.id,
            UserBlock.blocked_id == participant.id,
            UserBlock.status == "active",
        )
        .first()
        is not None
        for participant in other_participants
    )
    blocked_me = any(
        db.query(UserBlock)
        .filter(
            UserBlock.blocker_id == participant.id,
            UserBlock.blocked_id == current_user.id,
            UserBlock.status == "active",
        )
        .first()
        is not None
        for participant in other_participants
    )
    can_send = bool(other_participants) and not blocked_by_me and not blocked_me and all(
        participant.status == "active" for participant in other_participants
    )

    return {
        "id": conversation.id,
        "conversation_type": conversation.conversation_type,
        "created_by": conversation.created_by,
        "created_at": conversation.created_at,
        "participants": [
            {
                "id": participant.id,
                "name": participant.name,
                "username": participant.username,
                "avatar_url": participant.avatar_url,
                "trust_score": participant.trust_score,
                "status": participant.status,
            }
            for participant in participants
        ],
        "blocked_by_me": blocked_by_me,
        "blocked_me": blocked_me,
        "can_send": can_send,
    }


def conversation_list_items(db: Session, current_user: User, conversations: list[Conversation]) -> list[dict]:
    conversation_ids = [conversation.id for conversation in conversations]
    if not conversation_ids:
        return []

    latest_created_at = (
        db.query(
            Message.conversation_id.label("conversation_id"),
            func.max(Message.created_at).label("created_at"),
        )
        .filter(Message.conversation_id.in_(conversation_ids))
        .group_by(Message.conversation_id)
        .subquery()
    )
    last_messages = (
        db.query(Message)
        .join(
            latest_created_at,
            and_(
                Message.conversation_id == latest_created_at.c.conversation_id,
                Message.created_at == latest_created_at.c.created_at,
            ),
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .all()
    )
    last_message_by_conversation = {}
    for message in last_messages:
        last_message_by_conversation.setdefault(message.conversation_id, message)

    unread_counts = (
        db.query(Message.conversation_id, func.count(Message.id))
        .filter(
            Message.conversation_id.in_(conversation_ids),
            Message.receiver_id == current_user.id,
            Message.read_at.is_(None),
        )
        .group_by(Message.conversation_id)
        .all()
    )
    unread_count_by_conversation = {
        conversation_id: int(count)
        for conversation_id, count in unread_counts
    }

    return [
        conversation_list_item(
            conversation,
            last_message_by_conversation.get(conversation.id),
            unread_count_by_conversation.get(conversation.id, 0),
        )
        for conversation in conversations
    ]


def conversation_list_item(conversation: Conversation, last_message: Message | None, unread_count: int) -> dict:
    return {
        "id": conversation.id,
        "conversation_type": conversation.conversation_type,
        "created_by": conversation.created_by,
        "created_at": conversation.created_at,
        "last_message": None if last_message is None else {
            "id": last_message.id,
            "sender_id": last_message.sender_id,
            "receiver_id": last_message.receiver_id,
            "content": decrypt_message(last_message.encrypted_content),
            "status": last_message.status,
            "created_at": last_message.created_at,
        },
        "unread_count": unread_count,
    }


def block_user(db: Session, blocker: User, blocked_id: UUID) -> UserBlock:
    if blocker.id == blocked_id:
        raise HTTPException(status_code=400, detail="Cannot block yourself")
    blocked = db.get(User, blocked_id)
    if blocked is None:
        raise HTTPException(status_code=404, detail="User not found")

    existing = (
        db.query(UserBlock)
        .filter(UserBlock.blocker_id == blocker.id, UserBlock.blocked_id == blocked_id)
        .one_or_none()
    )
    if existing:
        existing.status = "active"
        db.commit()
        db.refresh(existing)
        return existing

    block = UserBlock(blocker_id=blocker.id, blocked_id=blocked_id)
    db.add(block)
    db.commit()
    db.refresh(block)
    return block


def unblock_user(db: Session, blocker: User, blocked_id: UUID) -> dict:
    block = (
        db.query(UserBlock)
        .filter(UserBlock.blocker_id == blocker.id, UserBlock.blocked_id == blocked_id)
        .one_or_none()
    )
    if block:
        block.status = "inactive"
        db.commit()
    return {"status": "ok"}


def report_message(db: Session, reporter: User, message_id: UUID, reason: str, description: str | None) -> MessageReport:
    message = db.get(Message, message_id)
    if message is None:
        raise HTTPException(status_code=404, detail="Message not found")
    if reporter.id not in {message.sender_id, message.receiver_id}:
        raise HTTPException(status_code=403, detail="Message access denied")

    reported_user_id = message.sender_id if reporter.id == message.receiver_id else message.receiver_id
    existing = (
        db.query(MessageReport)
        .filter(MessageReport.message_id == message.id, MessageReport.reporter_id == reporter.id)
        .one_or_none()
    )
    if existing:
        return existing

    report = MessageReport(
        message_id=message.id,
        reporter_id=reporter.id,
        reported_user_id=reported_user_id,
        reason=reason,
        description=description,
    )
    db.add(report)
    db.add(FraudEvent(
        user_id=reported_user_id,
        event_type="message_report",
        severity="MEDIUM",
        event_metadata={"message_id": str(message.id), "reporter_id": str(reporter.id), "reason": reason},
    ))
    db.commit()
    db.refresh(report)
    return report


def mark_message_delivered(db: Session, receiver: User, message_id: UUID) -> Message:
    message = db.get(Message, message_id)
    if message is None:
        raise HTTPException(status_code=404, detail="Message not found")
    if message.receiver_id != receiver.id:
        raise HTTPException(status_code=403, detail="Only the receiver can mark this message as delivered")
    if message.delivered_at is None:
        message.delivered_at = utc_now()
    if message.status == "sent":
        message.status = "delivered"
    db.commit()
    db.refresh(message)
    return message


def mark_message_read(db: Session, receiver: User, message_id: UUID) -> Message:
    message = db.get(Message, message_id)
    if message is None:
        raise HTTPException(status_code=404, detail="Message not found")
    if message.receiver_id != receiver.id:
        raise HTTPException(status_code=403, detail="Only the receiver can mark this message as read")
    now = utc_now()
    if message.delivered_at is None:
        message.delivered_at = now
    if message.read_at is None:
        message.read_at = now
    message.status = "read"
    db.commit()
    db.refresh(message)
    return message


def mark_conversation_read(db: Session, receiver: User, conversation_id: UUID) -> dict:
    member = (
        db.query(ConversationMember)
        .filter(ConversationMember.conversation_id == conversation_id, ConversationMember.user_id == receiver.id)
        .first()
    )
    if member is None:
        raise HTTPException(status_code=403, detail="Conversation access denied")

    messages = (
        db.query(Message)
        .filter(
            Message.conversation_id == conversation_id,
            Message.receiver_id == receiver.id,
            Message.read_at.is_(None),
        )
        .all()
    )
    if not messages:
        return {"status": "ok", "read_count": 0, "read_at": None}

    now = utc_now()
    for message in messages:
        if message.delivered_at is None:
            message.delivered_at = now
        message.read_at = now
        message.status = "read"
    db.commit()
    return {"status": "ok", "read_count": len(messages), "read_at": now}


def message_out(message: Message) -> dict:
    return {
        "id": message.id,
        "conversation_id": message.conversation_id,
        "sender_id": message.sender_id,
        "receiver_id": message.receiver_id,
        "content": decrypt_message(message.encrypted_content),
        "status": message.status,
        "coin_cost": message.coin_cost,
        "transaction_id": message.transaction_id,
        "delivered_at": message.delivered_at,
        "read_at": message.read_at,
        "created_at": message.created_at,
    }
