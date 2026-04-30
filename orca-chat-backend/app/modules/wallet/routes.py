import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.dependencies import get_current_user
from app.models import LedgerTransaction, User, WalletEntry
from app.modules.chat.service import is_blocked_between
from app.modules.wallet.schemas import TransactionOut, WalletBalance, WalletEntryOut, WalletTransferRequest
from app.utils.ledger import (
    PLATFORM_WALLET,
    create_transaction,
    credit_wallet,
    debit_spendable,
    get_system_wallet,
    lock_wallets,
    money,
    spendable_balance,
)

router = APIRouter(prefix="/wallet", tags=["wallet"])


@router.get("/balance", response_model=WalletBalance)
def balance(current_user: User = Depends(get_current_user)):
    wallet = current_user.wallet
    return {
        "purchased_balance": wallet.purchased_balance,
        "earned_balance": wallet.earned_balance,
        "locked_balance": wallet.locked_balance,
        "spendable_balance": spendable_balance(wallet),
        "gas_paid_total": wallet.gas_paid_total,
        "reward_earned_total": wallet.reward_earned_total,
        "status": wallet.status,
    }


@router.get("/transactions", response_model=list[TransactionOut])
def transactions(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    transaction_type: str | None = Query(default=None, min_length=1, max_length=50),
    direction: Literal["incoming", "outgoing", "internal"] | None = Query(default=None),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    wallet_id = current_user.wallet.id
    start_at, end_before = history_window(start_date, end_date)
    query = db.query(LedgerTransaction).filter(
        or_(LedgerTransaction.from_wallet_id == wallet_id, LedgerTransaction.to_wallet_id == wallet_id)
    )
    if transaction_type:
        query = query.filter(
            LedgerTransaction.transaction_type == normalize_query_filter("transaction_type", transaction_type)
        )
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
    query = apply_created_at_window(query, LedgerTransaction, start_at, end_before)
    rows = query.order_by(LedgerTransaction.created_at.desc()).offset(offset).limit(limit).all()
    return [transaction_out(row, wallet_id) for row in rows]


@router.get("/entries", response_model=list[WalletEntryOut])
def entries(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    entry_type: Literal["DEBIT", "CREDIT"] | None = Query(default=None),
    balance_type: str | None = Query(default=None, min_length=1, max_length=30),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    start_at, end_before = history_window(start_date, end_date)
    query = db.query(WalletEntry).filter(WalletEntry.wallet_id == current_user.wallet.id)
    if entry_type:
        query = query.filter(WalletEntry.entry_type == entry_type)
    if balance_type:
        query = query.filter(WalletEntry.balance_type == normalize_query_filter("balance_type", balance_type))
    query = apply_created_at_window(query, WalletEntry, start_at, end_before)
    rows = query.order_by(WalletEntry.created_at.desc()).offset(offset).limit(limit).all()
    return [entry_out(row) for row in rows]


@router.post("/transfer", response_model=TransactionOut)
def transfer(
    payload: WalletTransferRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    x_idempotency_key: str | None = Header(default=None),
):
    idempotency_key = normalize_idempotency_key(x_idempotency_key)
    if payload.receiver_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot transfer coins to yourself")
    receiver = db.get(User, payload.receiver_id)
    if receiver is None or receiver.wallet is None:
        raise HTTPException(status_code=404, detail="Receiver not found")
    if receiver.status != "active":
        raise HTTPException(status_code=403, detail="Receiver is not active")
    if is_blocked_between(db, current_user.id, receiver.id):
        raise HTTPException(status_code=403, detail="Transfer is blocked")

    amount = money(payload.amount)
    platform_gas = money(amount * settings.p2p_transfer_gas_percent)
    recipient_amount = amount - platform_gas
    if recipient_amount <= 0:
        raise HTTPException(status_code=400, detail="Transfer amount is too small after gas")

    if idempotency_key:
        existing = (
            db.query(LedgerTransaction)
            .filter(
                LedgerTransaction.from_wallet_id == current_user.wallet.id,
                LedgerTransaction.transaction_type == "wallet_transfer",
                LedgerTransaction.idempotency_key == idempotency_key,
            )
            .one_or_none()
        )
        if existing:
            ensure_transfer_idempotency_match(existing, receiver, amount, platform_gas, payload.note)
            return transaction_out(existing, current_user.wallet.id)

    try:
        locked_wallets = lock_wallets(db, [current_user.wallet.id, receiver.wallet.id])
        sender_wallet = locked_wallets[current_user.wallet.id]
        receiver_wallet = locked_wallets[receiver.wallet.id]
        if sender_wallet.status != "active":
            raise HTTPException(status_code=423, detail="Wallet is not active")
        if receiver_wallet.status != "active":
            raise HTTPException(status_code=423, detail="Receiver wallet is not active")

        transaction = create_transaction(
            db,
            transaction_type="wallet_transfer",
            from_wallet_id=sender_wallet.id,
            to_wallet_id=receiver_wallet.id,
            gross_amount=amount,
            platform_gas=platform_gas,
            idempotency_key=idempotency_key,
            metadata={"note": payload.note, "receiver_id": str(receiver.id)},
        )
        debit_spendable(db, sender_wallet, amount, transaction, "Wallet transfer")
        sender_wallet.gas_paid_total = money(sender_wallet.gas_paid_total) + platform_gas
        credit_wallet(db, receiver_wallet, recipient_amount, "purchased", transaction, "Wallet transfer received")
        credit_wallet(db, get_system_wallet(db, PLATFORM_WALLET), platform_gas, "gas", transaction, "P2P transfer gas")
        db.commit()
        db.refresh(transaction)
    except Exception:
        db.rollback()
        raise
    return transaction_out(transaction, current_user.wallet.id)


def normalize_idempotency_key(idempotency_key: str | None) -> str | None:
    if idempotency_key is None:
        return None
    normalized = idempotency_key.strip()
    if not normalized:
        raise HTTPException(status_code=400, detail="Idempotency key cannot be blank")
    if len(normalized) > settings.message_idempotency_key_max_length:
        raise HTTPException(status_code=400, detail="Idempotency key too large")
    if not re.fullmatch(settings.message_idempotency_key_pattern, normalized):
        raise HTTPException(status_code=400, detail="Idempotency key has invalid format")
    return normalized


def normalize_query_filter(field_name: str, value: str) -> str:
    normalized = value.strip().lower()
    if not normalized:
        raise HTTPException(status_code=422, detail=f"{field_name} cannot be blank")
    return normalized


def ensure_transfer_idempotency_match(
    transaction: LedgerTransaction,
    receiver: User,
    amount: Decimal,
    platform_gas: Decimal,
    note: str | None,
) -> None:
    metadata = transaction.transaction_metadata or {}
    if (
        transaction.to_wallet_id != receiver.wallet.id
        or money(transaction.gross_amount) != amount
        or money(transaction.platform_gas) != platform_gas
        or metadata.get("receiver_id") != str(receiver.id)
        or metadata.get("note") != note
    ):
        raise HTTPException(status_code=409, detail="Idempotency key was already used for a different transfer")


def history_window(start_date: date | None, end_date: date | None) -> tuple[datetime | None, datetime | None]:
    if start_date and end_date and start_date > end_date:
        raise HTTPException(status_code=400, detail="start_date must be before or equal to end_date")
    start_at = datetime.combine(start_date, time.min, tzinfo=timezone.utc) if start_date else None
    end_before = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=timezone.utc) if end_date else None
    return start_at, end_before


def apply_created_at_window(query, model, start_at: datetime | None, end_before: datetime | None):
    if start_at:
        query = query.filter(model.created_at >= start_at)
    if end_before:
        query = query.filter(model.created_at < end_before)
    return query


def transaction_out(transaction: LedgerTransaction, wallet_id) -> dict:
    if transaction.from_wallet_id == wallet_id and transaction.to_wallet_id == wallet_id:
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
        "gross_amount": transaction.gross_amount,
        "platform_gas": transaction.platform_gas,
        "receiver_reward": transaction.receiver_reward,
        "reserve_amount": transaction.reserve_amount,
        "status": transaction.status,
        "metadata": transaction.transaction_metadata,
        "created_at": transaction.created_at,
    }


def entry_out(entry: WalletEntry) -> dict:
    direction = "incoming" if entry.entry_type == "CREDIT" else "outgoing"
    signed_amount = Decimal(str(entry.amount))
    if entry.entry_type == "DEBIT":
        signed_amount = -signed_amount

    return {
        "id": entry.id,
        "transaction_id": entry.transaction_id,
        "entry_type": entry.entry_type,
        "direction": direction,
        "amount": entry.amount,
        "signed_amount": signed_amount,
        "balance_type": entry.balance_type,
        "description": entry.description,
        "created_at": entry.created_at,
    }
