from datetime import timedelta

from sqlalchemy.orm import Session

from app.config import settings
from app.models import FraudEvent, Message, User
from app.utils.time import utc_now


def is_fraud_risk(db: Session, sender: User, receiver: User, content_hash: str) -> tuple[bool, str | None]:
    if sender.id == receiver.id:
        return True, "self_messaging"

    one_minute_ago = utc_now() - timedelta(minutes=1)
    recent_count = (
        db.query(Message)
        .filter(Message.sender_id == sender.id, Message.created_at >= one_minute_ago)
        .count()
    )
    if recent_count >= settings.message_max_sends_per_minute:
        return True, "message_velocity"

    duplicate_count = (
        db.query(Message)
        .filter(
            Message.sender_id == sender.id,
            Message.receiver_id == receiver.id,
            Message.content_hash == content_hash,
            Message.created_at >= utc_now() - timedelta(hours=24),
        )
        .count()
    )
    if duplicate_count >= 3:
        return True, "duplicate_content"

    if receiver.status != "active":
        return True, "receiver_inactive"
    if receiver.wallet is None or receiver.wallet.status != "active":
        return True, "receiver_wallet_inactive"

    return False, None


def record_fraud_event(db: Session, user: User, event_type: str, severity: str = "MEDIUM", metadata: dict | None = None) -> None:
    db.add(FraudEvent(user_id=user.id, event_type=event_type, severity=severity, event_metadata=metadata or {}))
