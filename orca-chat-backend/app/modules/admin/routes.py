import re
from uuid import UUID
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Literal

from sqlalchemy import func, or_
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AdminAuditLog, AuthSession, FraudEvent, LedgerTransaction, Message, MessageReport, OtpChallenge, PaymentOrder, RewardEvent, SettlementHash, User, Wallet, WalletEntry
from app.modules.admin import schemas
from app.modules.admin.dependencies import require_admin
from app.modules.rewards.service import unlock_due_rewards
from app.modules.settlements.service import generate_daily_settlement_hash
from app.utils.ledger import audit_recent_transactions, spendable_balance
from app.utils.time import utc_now

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.get("/metrics")
def metrics(
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    db: Session = Depends(get_db),
):
    start_at, end_before = metrics_window(start_date, end_date)

    total_users = apply_created_window(db.query(func.count(User.id)), User, start_at, end_before).scalar() or 0
    blocked_users = (
        apply_created_window(db.query(func.count(User.id)).filter(User.status == "blocked"), User, start_at, end_before)
        .scalar()
        or 0
    )
    total_messages = apply_created_window(db.query(func.count(Message.id)), Message, start_at, end_before).scalar() or 0
    paid_messages = (
        apply_created_window(db.query(func.count(Message.id)).filter(Message.coin_cost > 0), Message, start_at, end_before)
        .scalar()
        or 0
    )
    free_messages = (
        apply_created_window(db.query(func.count(Message.id)).filter(Message.coin_cost == 0), Message, start_at, end_before)
        .scalar()
        or 0
    )
    spam_reports = (
        apply_created_window(
            db.query(func.count(MessageReport.id)).filter(MessageReport.status == "open"),
            MessageReport,
            start_at,
            end_before,
        )
        .scalar()
        or 0
    )
    total_revenue = (
        db.query(func.coalesce(func.sum(PaymentOrder.amount), 0))
        .filter(PaymentOrder.status == "success")
        .filter(*created_window_filters(PaymentOrder, start_at, end_before))
        .scalar()
    )
    gas_collected = (
        db.query(func.coalesce(func.sum(LedgerTransaction.platform_gas), 0))
        .filter(*created_window_filters(LedgerTransaction, start_at, end_before))
        .scalar()
    )
    locked_coins = db.query(func.coalesce(func.sum(Wallet.locked_balance), 0)).scalar()
    suspicious_accounts = (
        apply_created_window(
            db.query(func.count(FraudEvent.id)).filter(FraudEvent.status == "open"),
            FraudEvent,
            start_at,
            end_before,
        )
        .scalar()
        or 0
    )
    return {
        "window": {
            "start_date": start_date.isoformat() if start_date else None,
            "end_date": end_date.isoformat() if end_date else None,
        },
        "users": {"total": total_users, "blocked": blocked_users},
        "chat": {
            "total_messages": total_messages,
            "paid_messages": paid_messages,
            "free_messages": free_messages,
            "spam_reports": spam_reports,
        },
        "wallet": {"total_gas_collected": gas_collected, "total_locked_coins": locked_coins},
        "payments": {"recharge_revenue": total_revenue},
        "fraud": {"open_events": suspicious_accounts},
    }


@router.get("/users")
def users(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    q: str | None = Query(default=None, min_length=1, max_length=100),
    status: Literal["active", "blocked"] | None = Query(default=None),
    role: Literal["user", "admin"] | None = Query(default=None),
    kyc_status: Literal["not_started", "pending", "verified", "approved", "rejected", "premium"] | None = Query(default=None),
    min_trust_score: int | None = Query(default=None, ge=0, le=100),
    max_trust_score: int | None = Query(default=None, ge=0, le=100),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    db: Session = Depends(get_db),
):
    if min_trust_score is not None and max_trust_score is not None and min_trust_score > max_trust_score:
        raise HTTPException(status_code=400, detail="min_trust_score must be less than or equal to max_trust_score")
    start_at, end_before = metrics_window(start_date, end_date)

    query = db.query(User)
    if q:
        normalized_q = normalize_query_filter("q", q).lower()
        pattern = f"%{normalized_q}%"
        query = query.filter(
            or_(
                func.lower(User.phone).like(pattern),
                func.lower(User.email).like(pattern),
                func.lower(User.name).like(pattern),
                func.lower(User.username).like(pattern),
            )
        )
    if status:
        query = query.filter(User.status == status)
    if role:
        query = query.filter(User.role == role)
    if kyc_status:
        query = query.filter(User.kyc_status == kyc_status)
    if min_trust_score is not None:
        query = query.filter(User.trust_score >= min_trust_score)
    if max_trust_score is not None:
        query = query.filter(User.trust_score <= max_trust_score)
    query = apply_created_window(query, User, start_at, end_before)
    return query.order_by(User.created_at.desc()).offset(offset).limit(limit).all()


@router.get("/sessions")
def auth_sessions(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    status: Literal["active", "revoked"] | None = Query(default=None),
    user_id: UUID | None = Query(default=None),
    device_label: str | None = Query(default=None, min_length=1, max_length=120),
    ip_address: str | None = Query(default=None, min_length=1, max_length=80),
    jti: str | None = Query(default=None, min_length=1, max_length=80),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    last_seen_start_date: date | None = Query(default=None),
    last_seen_end_date: date | None = Query(default=None),
    expires_start_date: date | None = Query(default=None),
    expires_end_date: date | None = Query(default=None),
    db: Session = Depends(get_db),
):
    start_at, end_before = metrics_window(start_date, end_date)
    last_seen_start_at, last_seen_end_before = metrics_window(last_seen_start_date, last_seen_end_date)
    expires_start_at, expires_end_before = metrics_window(expires_start_date, expires_end_date)

    query = db.query(AuthSession)
    if status:
        query = query.filter(AuthSession.status == status)
    if user_id:
        query = query.filter(AuthSession.user_id == user_id)
    if device_label:
        device_label = normalize_query_filter("device_label", device_label)
        query = query.filter(AuthSession.device_label == device_label)
    if ip_address:
        ip_address = normalize_query_filter("ip_address", ip_address)
        query = query.filter(AuthSession.ip_address == ip_address)
    if jti:
        jti = normalize_query_filter("jti", jti)
        query = query.filter(AuthSession.jti == jti)
    query = apply_created_window(query, AuthSession, start_at, end_before)
    query = apply_datetime_window(query, AuthSession.last_seen_at, last_seen_start_at, last_seen_end_before)
    query = apply_datetime_window(query, AuthSession.expires_at, expires_start_at, expires_end_before)

    rows = query.order_by(AuthSession.last_seen_at.desc()).offset(offset).limit(limit).all()
    users_by_id = load_users_by_id(db, {row.user_id for row in rows})
    return [auth_session_out(row, users_by_id.get(row.user_id)) for row in rows]


@router.post("/sessions/{session_id}/revoke")
def revoke_auth_session(
    session_id: UUID,
    current_admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    session = db.get(AuthSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    previous_status = session.status
    if session.status != "revoked":
        session.status = "revoked"
        session.revoked_at = utc_now()
    record_admin_action(
        db,
        current_admin,
        "auth_session.revoke",
        "auth_session",
        session.id,
        {
            "user_id": str(session.user_id),
            "previous_status": previous_status,
            "status": session.status,
            "device_label": session.device_label,
            "ip_address": session.ip_address,
        },
    )
    db.commit()
    return auth_session_out(session, db.get(User, session.user_id))


@router.get("/otp-challenges")
def otp_challenges(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    phone: str | None = Query(default=None, min_length=1, max_length=20),
    ip_address: str | None = Query(default=None, min_length=1, max_length=80),
    status: Literal["pending", "verified", "expired", "blocked"] | None = Query(default=None),
    min_attempts: int | None = Query(default=None, ge=0),
    max_attempts: int | None = Query(default=None, ge=0),
    min_send_count: int | None = Query(default=None, ge=0),
    max_send_count: int | None = Query(default=None, ge=0),
    expires_start_date: date | None = Query(default=None),
    expires_end_date: date | None = Query(default=None),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    db: Session = Depends(get_db),
):
    if min_attempts is not None and max_attempts is not None and min_attempts > max_attempts:
        raise HTTPException(status_code=400, detail="min_attempts must be less than or equal to max_attempts")
    if min_send_count is not None and max_send_count is not None and min_send_count > max_send_count:
        raise HTTPException(status_code=400, detail="min_send_count must be less than or equal to max_send_count")
    start_at, end_before = metrics_window(start_date, end_date)
    expires_start_at, expires_end_before = metrics_window(expires_start_date, expires_end_date)

    query = db.query(OtpChallenge)
    if phone:
        phone = normalize_query_filter("phone", phone)
        query = query.filter(OtpChallenge.phone == phone)
    if ip_address:
        ip_address = normalize_query_filter("ip_address", ip_address)
        query = query.filter(OtpChallenge.ip_address == ip_address)
    if status:
        query = query.filter(OtpChallenge.status == status)
    if min_attempts is not None:
        query = query.filter(OtpChallenge.attempts >= min_attempts)
    if max_attempts is not None:
        query = query.filter(OtpChallenge.attempts <= max_attempts)
    if min_send_count is not None:
        query = query.filter(OtpChallenge.send_count >= min_send_count)
    if max_send_count is not None:
        query = query.filter(OtpChallenge.send_count <= max_send_count)
    query = apply_created_window(query, OtpChallenge, start_at, end_before)
    query = apply_datetime_window(query, OtpChallenge.expires_at, expires_start_at, expires_end_before)
    rows = query.order_by(OtpChallenge.created_at.desc()).offset(offset).limit(limit).all()
    return [otp_challenge_out(row) for row in rows]


@router.post("/otp-challenges/{challenge_id}/expire")
def expire_otp_challenge(
    challenge_id: UUID,
    current_admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    challenge = db.get(OtpChallenge, challenge_id)
    if challenge is None:
        raise HTTPException(status_code=404, detail="OTP challenge not found")
    previous_status = challenge.status
    if challenge.status == "pending":
        challenge.status = "expired"
        challenge.expires_at = utc_now()
    record_admin_action(
        db,
        current_admin,
        "otp_challenge.expire",
        "otp_challenge",
        challenge.id,
        {
            "phone": challenge.phone,
            "ip_address": challenge.ip_address,
            "previous_status": previous_status,
            "status": challenge.status,
        },
    )
    db.commit()
    db.refresh(challenge)
    return otp_challenge_out(challenge)


@router.get("/users/{user_id}")
def user_detail(
    user_id: UUID,
    recent_limit: int = Query(default=10, ge=1, le=50),
    db: Session = Depends(get_db),
):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    wallet = user.wallet
    recent_transactions = []
    if wallet is not None:
        rows = (
            db.query(LedgerTransaction)
            .filter(or_(LedgerTransaction.from_wallet_id == wallet.id, LedgerTransaction.to_wallet_id == wallet.id))
            .order_by(LedgerTransaction.created_at.desc())
            .limit(recent_limit)
            .all()
        )
        recent_transactions = [admin_transaction_out(row, wallet.id) for row in rows]

    return {
        "user": user,
        "wallet": wallet_out(wallet),
        "activity": {
            "active_session_count": (
                db.query(func.count(AuthSession.id))
                .filter(AuthSession.user_id == user.id, AuthSession.status == "active")
                .scalar()
                or 0
            ),
            "open_report_count": (
                db.query(func.count(MessageReport.id))
                .filter(MessageReport.reported_user_id == user.id, MessageReport.status == "open")
                .scalar()
                or 0
            ),
            "open_fraud_count": (
                db.query(func.count(FraudEvent.id))
                .filter(FraudEvent.user_id == user.id, FraudEvent.status == "open")
                .scalar()
                or 0
            ),
        },
        "recent_transactions": recent_transactions,
    }


@router.get("/payments")
def payments(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    status: Literal["created", "success", "failed"] | None = Query(default=None),
    gateway: str | None = Query(default=None, min_length=1, max_length=50),
    user_id: UUID | None = Query(default=None),
    gateway_order_id: str | None = Query(default=None, min_length=1, max_length=255),
    gateway_payment_id: str | None = Query(default=None, min_length=1, max_length=255),
    currency: str | None = Query(default=None, min_length=3, max_length=10),
    min_amount: Decimal | None = Query(default=None, ge=0),
    max_amount: Decimal | None = Query(default=None, ge=0),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    db: Session = Depends(get_db),
):
    if min_amount is not None and max_amount is not None and min_amount > max_amount:
        raise HTTPException(status_code=400, detail="min_amount must be less than or equal to max_amount")
    start_at, end_before = metrics_window(start_date, end_date)

    query = db.query(PaymentOrder)
    if status:
        query = query.filter(PaymentOrder.status == status)
    if gateway:
        gateway = normalize_query_filter("gateway", gateway).lower()
        query = query.filter(PaymentOrder.gateway == gateway)
    if user_id:
        query = query.filter(PaymentOrder.user_id == user_id)
    if gateway_order_id:
        gateway_order_id = normalize_query_filter("gateway_order_id", gateway_order_id)
        query = query.filter(PaymentOrder.gateway_order_id == gateway_order_id)
    if gateway_payment_id:
        gateway_payment_id = normalize_query_filter("gateway_payment_id", gateway_payment_id)
        query = query.filter(PaymentOrder.gateway_payment_id == gateway_payment_id)
    if currency:
        currency = normalize_query_filter("currency", currency).upper()
        query = query.filter(func.upper(PaymentOrder.currency) == currency)
    if min_amount is not None:
        query = query.filter(PaymentOrder.amount >= min_amount)
    if max_amount is not None:
        query = query.filter(PaymentOrder.amount <= max_amount)
    query = apply_created_window(query, PaymentOrder, start_at, end_before)
    rows = query.order_by(PaymentOrder.created_at.desc()).offset(offset).limit(limit).all()
    users_by_id = load_users_by_id(db, {row.user_id for row in rows})
    return [payment_order_out(row, users_by_id.get(row.user_id)) for row in rows]


def normalize_query_filter(name: str, value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise HTTPException(status_code=422, detail=f"{name} cannot be blank")
    return normalized


@router.post("/payments/{payment_order_id}/fail")
def fail_payment_order(
    payment_order_id: UUID,
    current_admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    payment_order = db.get(PaymentOrder, payment_order_id)
    if payment_order is None:
        raise HTTPException(status_code=404, detail="Payment order not found")
    if payment_order.status == "success":
        raise HTTPException(status_code=409, detail="Successful payment orders cannot be marked failed")
    previous_status = payment_order.status
    if payment_order.status != "failed":
        payment_order.status = "failed"
        payment_order.updated_at = utc_now()
    record_admin_action(
        db,
        current_admin,
        "payment.fail",
        "payment_order",
        payment_order.id,
        {
            "user_id": str(payment_order.user_id),
            "gateway": payment_order.gateway,
            "gateway_order_id": payment_order.gateway_order_id,
            "previous_status": previous_status,
            "status": payment_order.status,
        },
    )
    db.commit()
    db.refresh(payment_order)
    return payment_order_out(payment_order, db.get(User, payment_order.user_id))


@router.get("/wallets")
def wallets(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    status: Literal["active", "frozen"] | None = Query(default=None),
    wallet_type: str | None = Query(default=None, min_length=1, max_length=30),
    user_id: UUID | None = Query(default=None),
    min_spendable_balance: Decimal | None = Query(default=None, ge=0),
    max_spendable_balance: Decimal | None = Query(default=None, ge=0),
    min_locked_balance: Decimal | None = Query(default=None, ge=0),
    min_gas_paid_total: Decimal | None = Query(default=None, ge=0),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    db: Session = Depends(get_db),
):
    if (
        min_spendable_balance is not None
        and max_spendable_balance is not None
        and min_spendable_balance > max_spendable_balance
    ):
        raise HTTPException(status_code=400, detail="min_spendable_balance must be less than or equal to max_spendable_balance")
    start_at, end_before = metrics_window(start_date, end_date)

    spendable_expression = Wallet.purchased_balance + Wallet.earned_balance
    query = db.query(Wallet)
    if status:
        query = query.filter(Wallet.status == status)
    if wallet_type:
        wallet_type = normalize_query_filter("wallet_type", wallet_type).lower()
        query = query.filter(Wallet.wallet_type == wallet_type)
    if user_id:
        query = query.filter(Wallet.user_id == user_id)
    if min_spendable_balance is not None:
        query = query.filter(spendable_expression >= min_spendable_balance)
    if max_spendable_balance is not None:
        query = query.filter(spendable_expression <= max_spendable_balance)
    if min_locked_balance is not None:
        query = query.filter(Wallet.locked_balance >= min_locked_balance)
    if min_gas_paid_total is not None:
        query = query.filter(Wallet.gas_paid_total >= min_gas_paid_total)
    query = apply_created_window(query, Wallet, start_at, end_before)
    rows = query.order_by(Wallet.updated_at.desc()).offset(offset).limit(limit).all()
    return [wallet_inventory_out(row) for row in rows]


@router.get("/ledger/transactions")
def ledger_transactions(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    transaction_type: str | None = Query(default=None, min_length=1, max_length=50),
    status: Literal["settled", "pending", "failed"] | None = Query(default=None),
    wallet_id: UUID | None = Query(default=None),
    direction: Literal["incoming", "outgoing", "internal"] | None = Query(default=None),
    from_wallet_id: UUID | None = Query(default=None),
    to_wallet_id: UUID | None = Query(default=None),
    reference_id: UUID | None = Query(default=None),
    idempotency_key: str | None = Query(default=None, min_length=1, max_length=120),
    min_gross_amount: Decimal | None = Query(default=None, ge=0),
    max_gross_amount: Decimal | None = Query(default=None, ge=0),
    min_platform_gas: Decimal | None = Query(default=None, ge=0),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    db: Session = Depends(get_db),
):
    if min_gross_amount is not None and max_gross_amount is not None and min_gross_amount > max_gross_amount:
        raise HTTPException(status_code=400, detail="min_gross_amount must be less than or equal to max_gross_amount")
    if direction and wallet_id is None:
        raise HTTPException(status_code=400, detail="wallet_id is required when filtering by direction")
    start_at, end_before = metrics_window(start_date, end_date)

    query = db.query(LedgerTransaction)
    if transaction_type:
        transaction_type = normalize_query_filter("transaction_type", transaction_type).lower()
        query = query.filter(LedgerTransaction.transaction_type == transaction_type)
    if status:
        query = query.filter(LedgerTransaction.status == status)
    if wallet_id:
        query = query.filter(or_(LedgerTransaction.from_wallet_id == wallet_id, LedgerTransaction.to_wallet_id == wallet_id))
        if direction == "incoming":
            query = query.filter(
                LedgerTransaction.to_wallet_id == wallet_id,
                or_(LedgerTransaction.from_wallet_id.is_(None), LedgerTransaction.from_wallet_id != wallet_id),
            )
        elif direction == "outgoing":
            query = query.filter(
                LedgerTransaction.from_wallet_id == wallet_id,
                or_(LedgerTransaction.to_wallet_id.is_(None), LedgerTransaction.to_wallet_id != wallet_id),
            )
        elif direction == "internal":
            query = query.filter(LedgerTransaction.from_wallet_id == wallet_id, LedgerTransaction.to_wallet_id == wallet_id)
    if from_wallet_id:
        query = query.filter(LedgerTransaction.from_wallet_id == from_wallet_id)
    if to_wallet_id:
        query = query.filter(LedgerTransaction.to_wallet_id == to_wallet_id)
    if reference_id:
        query = query.filter(LedgerTransaction.reference_id == reference_id)
    if idempotency_key:
        idempotency_key = normalize_query_filter("idempotency_key", idempotency_key)
        query = query.filter(LedgerTransaction.idempotency_key == idempotency_key)
    if min_gross_amount is not None:
        query = query.filter(LedgerTransaction.gross_amount >= min_gross_amount)
    if max_gross_amount is not None:
        query = query.filter(LedgerTransaction.gross_amount <= max_gross_amount)
    if min_platform_gas is not None:
        query = query.filter(LedgerTransaction.platform_gas >= min_platform_gas)
    query = apply_created_window(query, LedgerTransaction, start_at, end_before)
    rows = query.order_by(LedgerTransaction.created_at.desc()).offset(offset).limit(limit).all()
    return [admin_transaction_out(row, wallet_id) for row in rows]


@router.get("/ledger/entries")
def ledger_entries(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    transaction_id: UUID | None = Query(default=None),
    wallet_id: UUID | None = Query(default=None),
    entry_type: Literal["DEBIT", "CREDIT"] | None = Query(default=None),
    balance_type: str | None = Query(default=None, min_length=1, max_length=30),
    min_amount: Decimal | None = Query(default=None, ge=0),
    max_amount: Decimal | None = Query(default=None, ge=0),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    db: Session = Depends(get_db),
):
    if min_amount is not None and max_amount is not None and min_amount > max_amount:
        raise HTTPException(status_code=400, detail="min_amount must be less than or equal to max_amount")
    start_at, end_before = metrics_window(start_date, end_date)

    query = db.query(WalletEntry)
    if transaction_id:
        query = query.filter(WalletEntry.transaction_id == transaction_id)
    if wallet_id:
        query = query.filter(WalletEntry.wallet_id == wallet_id)
    if entry_type:
        query = query.filter(WalletEntry.entry_type == entry_type)
    if balance_type:
        balance_type = normalize_query_filter("balance_type", balance_type).lower()
        query = query.filter(WalletEntry.balance_type == balance_type)
    if min_amount is not None:
        query = query.filter(WalletEntry.amount >= min_amount)
    if max_amount is not None:
        query = query.filter(WalletEntry.amount <= max_amount)
    query = apply_created_window(query, WalletEntry, start_at, end_before)
    rows = query.order_by(WalletEntry.created_at.desc()).offset(offset).limit(limit).all()
    wallets_by_id = load_wallets_by_id(db, {row.wallet_id for row in rows})
    users_by_id = load_users_by_id(db, {wallet.user_id for wallet in wallets_by_id.values() if wallet.user_id is not None})
    return [admin_wallet_entry_out(row, wallets_by_id.get(row.wallet_id), users_by_id) for row in rows]


@router.get("/rewards")
def rewards(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    status: Literal["locked", "unlocked"] | None = Query(default=None),
    user_id: UUID | None = Query(default=None),
    source: str | None = Query(default=None, min_length=1, max_length=50),
    reference_id: UUID | None = Query(default=None),
    min_final_reward: Decimal | None = Query(default=None, ge=0),
    max_final_reward: Decimal | None = Query(default=None, ge=0),
    lock_start_date: date | None = Query(default=None),
    lock_end_date: date | None = Query(default=None),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    db: Session = Depends(get_db),
):
    if min_final_reward is not None and max_final_reward is not None and min_final_reward > max_final_reward:
        raise HTTPException(status_code=400, detail="min_final_reward must be less than or equal to max_final_reward")
    start_at, end_before = metrics_window(start_date, end_date)
    lock_start_at, lock_end_before = metrics_window(lock_start_date, lock_end_date)

    query = db.query(RewardEvent)
    if status:
        query = query.filter(RewardEvent.status == status)
    if user_id:
        query = query.filter(RewardEvent.user_id == user_id)
    if source:
        source = normalize_query_filter("source", source).lower()
        query = query.filter(RewardEvent.source == source)
    if reference_id:
        query = query.filter(RewardEvent.reference_id == reference_id)
    if min_final_reward is not None:
        query = query.filter(RewardEvent.final_reward >= min_final_reward)
    if max_final_reward is not None:
        query = query.filter(RewardEvent.final_reward <= max_final_reward)
    if lock_start_at:
        query = query.filter(RewardEvent.lock_until >= lock_start_at)
    if lock_end_before:
        query = query.filter(RewardEvent.lock_until < lock_end_before)
    query = apply_created_window(query, RewardEvent, start_at, end_before)
    rows = query.order_by(RewardEvent.created_at.desc()).offset(offset).limit(limit).all()
    users_by_id = load_users_by_id(db, {row.user_id for row in rows})
    return [reward_event_out(row, users_by_id.get(row.user_id)) for row in rows]


@router.get("/audit-logs")
def audit_logs(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    action: str | None = Query(default=None, max_length=100),
    target_type: str | None = Query(default=None, max_length=50),
    admin_user_id: UUID | None = Query(default=None),
    target_id: UUID | None = Query(default=None),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    db: Session = Depends(get_db),
):
    start_at, end_before = metrics_window(start_date, end_date)
    query = db.query(AdminAuditLog)
    if action:
        action = normalize_query_filter("action", action).lower()
        query = query.filter(AdminAuditLog.action == action)
    if target_type:
        target_type = normalize_query_filter("target_type", target_type).lower()
        query = query.filter(AdminAuditLog.target_type == target_type)
    if admin_user_id:
        query = query.filter(AdminAuditLog.admin_user_id == admin_user_id)
    if target_id:
        query = query.filter(AdminAuditLog.target_id == target_id)
    query = apply_created_window(query, AdminAuditLog, start_at, end_before)
    rows = query.order_by(AdminAuditLog.created_at.desc()).offset(offset).limit(limit).all()
    return [audit_log_out(row) for row in rows]


@router.patch("/users/{user_id}")
def update_user(
    user_id: UUID,
    payload: schemas.AdminUserUpdateRequest,
    current_admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if current_admin.id == user_id:
        if payload.status is not None and payload.status != "active":
            raise HTTPException(status_code=400, detail="Cannot block your own account")
        if payload.role is not None and payload.role != "admin":
            raise HTTPException(status_code=400, detail="Cannot remove your own admin role")

    if payload.status is not None:
        user.status = payload.status
        if payload.status == "blocked":
            revoke_active_sessions(db, user.id)
    if payload.role is not None:
        user.role = payload.role
    if payload.kyc_status is not None:
        user.kyc_status = payload.kyc_status
    if payload.trust_score is not None:
        user.trust_score = payload.trust_score

    record_admin_action(
        db,
        current_admin,
        "user.update",
        "user",
        user.id,
        {
            "status": payload.status,
            "role": payload.role,
            "kyc_status": payload.kyc_status,
            "trust_score": payload.trust_score,
        },
    )
    db.commit()
    db.refresh(user)
    return user


@router.get("/fraud")
def fraud(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    status: Literal["open", "resolved", "dismissed"] | None = Query(default=None),
    severity: Literal["LOW", "MEDIUM", "HIGH", "BLOCKED"] | None = Query(default=None),
    event_type: str | None = Query(default=None, min_length=1, max_length=100),
    user_id: UUID | None = Query(default=None),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    db: Session = Depends(get_db),
):
    start_at, end_before = metrics_window(start_date, end_date)
    query = db.query(FraudEvent)
    if status:
        query = query.filter(FraudEvent.status == status)
    if severity:
        query = query.filter(FraudEvent.severity == severity)
    if event_type:
        event_type = normalize_query_filter("event_type", event_type)
        query = query.filter(FraudEvent.event_type == event_type)
    if user_id:
        query = query.filter(FraudEvent.user_id == user_id)
    query = apply_created_window(query, FraudEvent, start_at, end_before)
    return query.order_by(FraudEvent.created_at.desc()).offset(offset).limit(limit).all()


@router.get("/messages")
def messages(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    conversation_id: UUID | None = Query(default=None),
    sender_id: UUID | None = Query(default=None),
    receiver_id: UUID | None = Query(default=None),
    message_type: str | None = Query(default=None, min_length=1, max_length=30),
    status: Literal["sent", "delivered", "read"] | None = Query(default=None),
    transaction_id: UUID | None = Query(default=None),
    content_hash: str | None = Query(default=None, min_length=1, max_length=100),
    min_coin_cost: Decimal | None = Query(default=None, ge=0),
    max_coin_cost: Decimal | None = Query(default=None, ge=0),
    has_transaction: bool | None = Query(default=None),
    delivered: bool | None = Query(default=None),
    read: bool | None = Query(default=None),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    db: Session = Depends(get_db),
):
    if min_coin_cost is not None and max_coin_cost is not None and min_coin_cost > max_coin_cost:
        raise HTTPException(status_code=400, detail="min_coin_cost must be less than or equal to max_coin_cost")
    start_at, end_before = metrics_window(start_date, end_date)

    query = db.query(Message)
    if conversation_id:
        query = query.filter(Message.conversation_id == conversation_id)
    if sender_id:
        query = query.filter(Message.sender_id == sender_id)
    if receiver_id:
        query = query.filter(Message.receiver_id == receiver_id)
    if message_type:
        message_type = normalize_query_filter("message_type", message_type).lower()
        query = query.filter(Message.message_type == message_type)
    if status:
        query = query.filter(Message.status == status)
    if transaction_id:
        query = query.filter(Message.transaction_id == transaction_id)
    if content_hash:
        content_hash = normalize_query_filter("content_hash", content_hash).lower()
        if len(content_hash) != 64:
            raise HTTPException(status_code=422, detail="content_hash must be 64 characters")
        query = query.filter(Message.content_hash == content_hash)
    if min_coin_cost is not None:
        query = query.filter(Message.coin_cost >= min_coin_cost)
    if max_coin_cost is not None:
        query = query.filter(Message.coin_cost <= max_coin_cost)
    if has_transaction is True:
        query = query.filter(Message.transaction_id.is_not(None))
    elif has_transaction is False:
        query = query.filter(Message.transaction_id.is_(None))
    if delivered is True:
        query = query.filter(Message.delivered_at.is_not(None))
    elif delivered is False:
        query = query.filter(Message.delivered_at.is_(None))
    if read is True:
        query = query.filter(Message.read_at.is_not(None))
    elif read is False:
        query = query.filter(Message.read_at.is_(None))
    query = apply_created_window(query, Message, start_at, end_before)
    rows = query.order_by(Message.created_at.desc()).offset(offset).limit(limit).all()
    users_by_id = load_users_by_id(db, {row.sender_id for row in rows} | {row.receiver_id for row in rows})
    return [message_out(row, users_by_id) for row in rows]


@router.get("/reports")
def reports(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    status: Literal["open", "resolved", "dismissed"] | None = Query(default=None),
    reason: str | None = Query(default=None, min_length=1, max_length=100),
    reporter_id: UUID | None = Query(default=None),
    reported_user_id: UUID | None = Query(default=None),
    message_id: UUID | None = Query(default=None),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    db: Session = Depends(get_db),
):
    start_at, end_before = metrics_window(start_date, end_date)
    query = db.query(MessageReport)
    if status:
        query = query.filter(MessageReport.status == status)
    if reason:
        reason = normalize_query_filter("reason", reason).lower()
        query = query.filter(MessageReport.reason == reason)
    if reporter_id:
        query = query.filter(MessageReport.reporter_id == reporter_id)
    if reported_user_id:
        query = query.filter(MessageReport.reported_user_id == reported_user_id)
    if message_id:
        query = query.filter(MessageReport.message_id == message_id)
    query = apply_created_window(query, MessageReport, start_at, end_before)
    return query.order_by(MessageReport.created_at.desc()).offset(offset).limit(limit).all()


@router.patch("/reports/{report_id}")
def update_report_status(
    report_id: UUID,
    payload: schemas.StatusUpdateRequest,
    current_admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    report = db.get(MessageReport, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    previous_status = report.status
    report.status = payload.status
    record_admin_action(
        db,
        current_admin,
        "report.status_update",
        "message_report",
        report.id,
        {"previous_status": previous_status, "status": payload.status},
    )
    db.commit()
    db.refresh(report)
    return report


@router.patch("/fraud/{event_id}")
def update_fraud_status(
    event_id: UUID,
    payload: schemas.StatusUpdateRequest,
    current_admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    event = db.get(FraudEvent, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Fraud event not found")
    previous_status = event.status
    event.status = payload.status
    record_admin_action(
        db,
        current_admin,
        "fraud.status_update",
        "fraud_event",
        event.id,
        {"previous_status": previous_status, "status": payload.status},
    )
    db.commit()
    db.refresh(event)
    return event


@router.get("/ledger/audit")
def ledger_audit(limit: int = Query(default=100, ge=1, le=500), db: Session = Depends(get_db)):
    return audit_recent_transactions(db, limit=limit)


@router.post("/rewards/unlock")
def unlock_rewards(
    limit: int = Query(default=100, ge=1, le=500),
    current_admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    result = unlock_due_rewards(db, limit=limit, commit=False)
    record_admin_action(db, current_admin, "rewards.unlock", "reward_event", None, result)
    db.commit()
    return result


@router.post("/settlements/{settlement_date}")
def generate_settlement(
    settlement_date: date,
    current_admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    settlement = generate_daily_settlement_hash(db, settlement_date, commit=False)
    record_admin_action(
        db,
        current_admin,
        "settlement.generate",
        "settlement_hash",
        settlement.id,
        {"settlement_date": settlement_date.isoformat(), "ledger_hash": settlement.ledger_hash},
    )
    db.commit()
    db.refresh(settlement)
    return settlement


@router.get("/settlements")
def settlements(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    status: Literal["generated", "verified", "failed"] | None = Query(default=None),
    ledger_hash: str | None = Query(default=None, min_length=1, max_length=80),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    min_transaction_count: int | None = Query(default=None, ge=0),
    min_entry_count: int | None = Query(default=None, ge=0),
    db: Session = Depends(get_db),
):
    if start_date and end_date and start_date > end_date:
        raise HTTPException(status_code=400, detail="start_date must be before or equal to end_date")
    query = db.query(SettlementHash)
    if status:
        query = query.filter(SettlementHash.status == status)
    if ledger_hash:
        query = query.filter(SettlementHash.ledger_hash == normalize_ledger_hash_filter(ledger_hash))
    if start_date:
        query = query.filter(SettlementHash.settlement_date >= start_date)
    if end_date:
        query = query.filter(SettlementHash.settlement_date <= end_date)
    if min_transaction_count is not None:
        query = query.filter(SettlementHash.transaction_count >= min_transaction_count)
    if min_entry_count is not None:
        query = query.filter(SettlementHash.entry_count >= min_entry_count)
    return query.order_by(SettlementHash.settlement_date.desc()).offset(offset).limit(limit).all()


def normalize_ledger_hash_filter(value: str) -> str:
    normalized = normalize_query_filter("ledger_hash", value).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized):
        raise HTTPException(status_code=422, detail="ledger_hash must be a 64-character hex string")
    return normalized


@router.post("/users/{user_id}/wallet/freeze")
def freeze_wallet(user_id: UUID, current_admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    ensure_not_self_action(current_admin, user_id, "freeze your own wallet")
    user = db.get(User, user_id)
    if user is None or user.wallet is None:
        raise HTTPException(status_code=404, detail="User wallet not found")
    previous_status = user.wallet.status
    user.wallet.status = "frozen"
    record_admin_action(
        db,
        current_admin,
        "wallet.freeze",
        "wallet",
        user.wallet.id,
        {"user_id": str(user.id), "previous_status": previous_status, "status": user.wallet.status},
    )
    db.commit()
    return {"user_id": user.id, "wallet_id": user.wallet.id, "status": user.wallet.status}


@router.post("/users/{user_id}/block")
def block_user_account(user_id: UUID, current_admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    ensure_not_self_action(current_admin, user_id, "block your own account")
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    previous_status = user.status
    user.status = "blocked"
    revoke_active_sessions(db, user_id)
    record_admin_action(
        db,
        current_admin,
        "user.block",
        "user",
        user.id,
        {"previous_status": previous_status, "status": user.status},
    )
    db.commit()
    return {"user_id": user.id, "status": user.status}


@router.post("/users/{user_id}/unblock")
def unblock_user_account(
    user_id: UUID,
    current_admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    previous_status = user.status
    user.status = "active"
    record_admin_action(
        db,
        current_admin,
        "user.unblock",
        "user",
        user.id,
        {"previous_status": previous_status, "status": user.status},
    )
    db.commit()
    return {"user_id": user.id, "status": user.status}


@router.post("/users/{user_id}/wallet/unfreeze")
def unfreeze_wallet(
    user_id: UUID,
    current_admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    user = db.get(User, user_id)
    if user is None or user.wallet is None:
        raise HTTPException(status_code=404, detail="User wallet not found")
    previous_status = user.wallet.status
    user.wallet.status = "active"
    record_admin_action(
        db,
        current_admin,
        "wallet.unfreeze",
        "wallet",
        user.wallet.id,
        {"user_id": str(user.id), "previous_status": previous_status, "status": user.wallet.status},
    )
    db.commit()
    return {"user_id": user.id, "wallet_id": user.wallet.id, "status": user.wallet.status}


def ensure_not_self_action(current_admin: User, target_user_id: UUID, action: str) -> None:
    if current_admin.id == target_user_id:
        raise HTTPException(status_code=400, detail=f"Cannot {action}")


def revoke_active_sessions(db: Session, user_id: UUID) -> None:
    (
        db.query(AuthSession)
        .filter(AuthSession.user_id == user_id, AuthSession.status == "active")
        .update({"status": "revoked", "revoked_at": func.now()}, synchronize_session=False)
    )


def record_admin_action(
    db: Session,
    admin: User,
    action: str,
    target_type: str,
    target_id: UUID | None,
    metadata: dict | None = None,
) -> None:
    db.add(AdminAuditLog(
        admin_user_id=admin.id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        audit_metadata=metadata or {},
    ))


def audit_log_out(log: AdminAuditLog) -> dict:
    return {
        "id": log.id,
        "admin_user_id": log.admin_user_id,
        "action": log.action,
        "target_type": log.target_type,
        "target_id": log.target_id,
        "metadata": log.audit_metadata,
        "created_at": log.created_at,
    }


def auth_session_out(session: AuthSession, user: User | None) -> dict:
    return {
        "id": session.id,
        "user_id": session.user_id,
        "user": user_summary(user),
        "jti": session.jti,
        "device_label": session.device_label,
        "user_agent": session.user_agent,
        "ip_address": session.ip_address,
        "status": session.status,
        "expires_at": session.expires_at,
        "revoked_at": session.revoked_at,
        "last_seen_at": session.last_seen_at,
        "created_at": session.created_at,
    }


def otp_challenge_out(challenge: OtpChallenge) -> dict:
    return {
        "id": challenge.id,
        "phone": challenge.phone,
        "ip_address": challenge.ip_address,
        "status": challenge.status,
        "attempts": challenge.attempts,
        "send_count": challenge.send_count,
        "last_sent_at": challenge.last_sent_at,
        "expires_at": challenge.expires_at,
        "created_at": challenge.created_at,
    }


def wallet_out(wallet: Wallet | None) -> dict | None:
    if wallet is None:
        return None
    return {
        "id": wallet.id,
        "status": wallet.status,
        "wallet_type": wallet.wallet_type,
        "purchased_balance": decimal_string(wallet.purchased_balance),
        "earned_balance": decimal_string(wallet.earned_balance),
        "locked_balance": decimal_string(wallet.locked_balance),
        "spendable_balance": decimal_string(spendable_balance(wallet)),
        "gas_paid_total": decimal_string(wallet.gas_paid_total),
        "reward_earned_total": decimal_string(wallet.reward_earned_total),
        "created_at": wallet.created_at,
        "updated_at": wallet.updated_at,
    }


def wallet_inventory_out(wallet: Wallet) -> dict:
    row = wallet_out(wallet)
    row["user"] = user_summary(wallet.user)
    return row


def payment_order_out(payment_order: PaymentOrder, user: User | None) -> dict:
    return {
        "id": payment_order.id,
        "user_id": payment_order.user_id,
        "user": user_summary(user),
        "gateway": payment_order.gateway,
        "gateway_order_id": payment_order.gateway_order_id,
        "gateway_payment_id": payment_order.gateway_payment_id,
        "amount": f"{Decimal(str(payment_order.amount)):.2f}",
        "currency": payment_order.currency,
        "coins_to_credit": decimal_string(payment_order.coins_to_credit),
        "status": payment_order.status,
        "created_at": payment_order.created_at,
        "updated_at": payment_order.updated_at,
    }


def reward_event_out(reward_event: RewardEvent, user: User | None) -> dict:
    return {
        "id": reward_event.id,
        "user_id": reward_event.user_id,
        "user": user_summary(user),
        "source": reward_event.source,
        "reference_id": reward_event.reference_id,
        "base_reward": decimal_string(reward_event.base_reward),
        "final_reward": decimal_string(reward_event.final_reward),
        "trust_multiplier": f"{Decimal(str(reward_event.trust_multiplier)):.4f}",
        "fraud_multiplier": f"{Decimal(str(reward_event.fraud_multiplier)):.4f}",
        "lock_until": reward_event.lock_until,
        "status": reward_event.status,
        "created_at": reward_event.created_at,
    }


def message_out(message: Message, users_by_id: dict[UUID, User]) -> dict:
    return {
        "id": message.id,
        "conversation_id": message.conversation_id,
        "sender_id": message.sender_id,
        "sender": user_summary(users_by_id.get(message.sender_id)),
        "receiver_id": message.receiver_id,
        "receiver": user_summary(users_by_id.get(message.receiver_id)),
        "message_type": message.message_type,
        "status": message.status,
        "coin_cost": decimal_string(message.coin_cost),
        "transaction_id": message.transaction_id,
        "idempotency_key": message.idempotency_key,
        "content_hash": message.content_hash,
        "delivered_at": message.delivered_at,
        "read_at": message.read_at,
        "created_at": message.created_at,
    }


def admin_wallet_entry_out(entry: WalletEntry, wallet: Wallet | None, users_by_id: dict[UUID, User]) -> dict:
    amount = Decimal(str(entry.amount))
    signed_amount = -amount if entry.entry_type == "DEBIT" else amount
    return {
        "id": entry.id,
        "transaction_id": entry.transaction_id,
        "wallet_id": entry.wallet_id,
        "wallet": wallet_summary(wallet, users_by_id),
        "entry_type": entry.entry_type,
        "direction": "debit" if entry.entry_type == "DEBIT" else "credit",
        "amount": decimal_string(amount),
        "signed_amount": decimal_string(signed_amount),
        "balance_type": entry.balance_type,
        "description": entry.description,
        "created_at": entry.created_at,
    }


def wallet_summary(wallet: Wallet | None, users_by_id: dict[UUID, User]) -> dict | None:
    if wallet is None:
        return None
    return {
        "id": wallet.id,
        "user_id": wallet.user_id,
        "user": user_summary(users_by_id.get(wallet.user_id)) if wallet.user_id else None,
        "wallet_type": wallet.wallet_type,
        "status": wallet.status,
    }


def load_wallets_by_id(db: Session, wallet_ids: set[UUID]) -> dict[UUID, Wallet]:
    if not wallet_ids:
        return {}
    rows = db.query(Wallet).filter(Wallet.id.in_(wallet_ids)).all()
    return {row.id: row for row in rows}


def load_users_by_id(db: Session, user_ids: set[UUID]) -> dict[UUID, User]:
    if not user_ids:
        return {}
    rows = db.query(User).filter(User.id.in_(user_ids)).all()
    return {row.id: row for row in rows}


def user_summary(user: User | None) -> dict | None:
    if user is None:
        return None
    return {
        "id": user.id,
        "phone": user.phone,
        "name": user.name,
        "username": user.username,
        "status": user.status,
        "role": user.role,
        "kyc_status": user.kyc_status,
        "trust_score": user.trust_score,
    }


def admin_transaction_out(transaction: LedgerTransaction, wallet_id: UUID | None) -> dict:
    if wallet_id is None:
        direction = None
    elif transaction.from_wallet_id == wallet_id and transaction.to_wallet_id == wallet_id:
        direction = "internal"
    elif transaction.from_wallet_id == wallet_id:
        direction = "outgoing"
    elif transaction.to_wallet_id == wallet_id:
        direction = "incoming"
    else:
        direction = "related"

    return {
        "id": transaction.id,
        "transaction_type": transaction.transaction_type,
        "reference_id": transaction.reference_id,
        "from_wallet_id": transaction.from_wallet_id,
        "to_wallet_id": transaction.to_wallet_id,
        "direction": direction,
        "gross_amount": decimal_string(transaction.gross_amount),
        "platform_gas": decimal_string(transaction.platform_gas),
        "receiver_reward": decimal_string(transaction.receiver_reward),
        "reserve_amount": decimal_string(transaction.reserve_amount),
        "status": transaction.status,
        "metadata": transaction.transaction_metadata,
        "created_at": transaction.created_at,
    }


def decimal_string(value) -> str:
    return f"{Decimal(str(value)):.6f}"


def metrics_window(
    start_date: date | None,
    end_date: date | None,
) -> tuple[datetime | None, datetime | None]:
    if start_date and end_date and start_date > end_date:
        raise HTTPException(status_code=400, detail="start_date must be before or equal to end_date")
    start_at = datetime.combine(start_date, time.min, tzinfo=timezone.utc) if start_date else None
    end_before = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=timezone.utc) if end_date else None
    return start_at, end_before


def created_window_filters(model, start_at: datetime | None, end_before: datetime | None) -> list:
    filters = []
    if start_at is not None:
        filters.append(model.created_at >= start_at)
    if end_before is not None:
        filters.append(model.created_at < end_before)
    return filters


def apply_created_window(query, model, start_at: datetime | None, end_before: datetime | None):
    return query.filter(*created_window_filters(model, start_at, end_before))


def apply_datetime_window(query, field, start_at: datetime | None, end_before: datetime | None):
    if start_at is not None:
        query = query.filter(field >= start_at)
    if end_before is not None:
        query = query.filter(field < end_before)
    return query
