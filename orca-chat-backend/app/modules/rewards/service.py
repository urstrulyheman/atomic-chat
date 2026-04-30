from datetime import datetime, timedelta
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import RewardEvent, User, Wallet, WalletEntry
from app.utils.ledger import create_transaction, money
from app.utils.time import as_utc, utc_now


def unlock_due_rewards(db: Session, limit: int = 100, commit: bool = True) -> dict:
    now = utc_now()
    reward_events = (
        db.query(RewardEvent)
        .filter(RewardEvent.status == "locked", RewardEvent.lock_until <= now)
        .order_by(RewardEvent.lock_until.asc())
        .limit(limit)
        .all()
    )

    unlocked = []
    try:
        for reward_event in reward_events:
            unlock_reward_event(db, reward_event)
            unlocked.append(str(reward_event.id))
        if commit:
            db.commit()
    except Exception:
        db.rollback()
        raise

    return {"checked": len(reward_events), "unlocked_count": len(unlocked), "unlocked_reward_event_ids": unlocked}


def unlock_reward_event(db: Session, reward_event: RewardEvent) -> None:
    if reward_event.status != "locked":
        return
    if as_utc(reward_event.lock_until) > utc_now():
        return

    wallet = (
        db.query(Wallet)
        .filter(Wallet.user_id == reward_event.user_id)
        .with_for_update()
        .one_or_none()
    )
    if wallet is None:
        raise HTTPException(status_code=404, detail="Reward wallet not found")

    amount = money(reward_event.final_reward)
    if money(wallet.locked_balance) < amount:
        raise HTTPException(status_code=409, detail="Locked reward balance mismatch")

    transaction = create_transaction(
        db,
        transaction_type="reward_unlock",
        from_wallet_id=wallet.id,
        to_wallet_id=wallet.id,
        reference_id=reward_event.id,
        gross_amount=amount,
        metadata={"reward_event_id": str(reward_event.id)},
    )

    wallet.locked_balance = money(wallet.locked_balance) - amount
    wallet.earned_balance = money(wallet.earned_balance) + amount
    reward_event.status = "unlocked"
    db.add_all([
        WalletEntry(
            transaction_id=transaction.id,
            wallet_id=wallet.id,
            entry_type="DEBIT",
            amount=amount,
            balance_type="locked",
            description="Unlock reward",
        ),
        WalletEntry(
            transaction_id=transaction.id,
            wallet_id=wallet.id,
            entry_type="CREDIT",
            amount=amount,
            balance_type="earned",
            description="Unlocked reward",
        ),
    ])


def reward_lock_until() -> datetime:
    from app.config import settings

    return utc_now() + timedelta(days=settings.reward_lock_days)


def daily_reward_cap(user: User) -> Decimal:
    from app.config import settings

    if user.kyc_status in {"verified", "approved", "premium"}:
        return money(settings.verified_user_daily_reward_cap)
    return money(settings.new_user_daily_reward_cap)


def remaining_daily_reward_capacity(db: Session, user: User) -> Decimal:
    today_start = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
    earned_today = (
        db.query(func.coalesce(func.sum(RewardEvent.final_reward), 0))
        .filter(RewardEvent.user_id == user.id, RewardEvent.created_at >= today_start)
        .scalar()
    )
    remaining = daily_reward_cap(user) - money(earned_today)
    return max(remaining, Decimal("0.000000"))


def cap_reward_to_daily_limit(db: Session, user: User, requested_reward: Decimal) -> Decimal:
    return min(money(requested_reward), remaining_daily_reward_capacity(db, user))
