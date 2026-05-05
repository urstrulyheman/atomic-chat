from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import LedgerTransaction, Wallet, WalletEntry

PLATFORM_WALLET = "platform"
RESERVE_WALLET = "reserve"


def money(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.000001"))


def get_system_wallet(db: Session, wallet_type: str) -> Wallet:
    wallet = db.query(Wallet).filter(Wallet.wallet_type == wallet_type).one_or_none()
    if wallet:
        return wallet
    wallet = Wallet(wallet_type=wallet_type, status="active")
    db.add(wallet)
    db.flush()
    return wallet


def lock_wallets(db: Session, wallet_ids: list[UUID]) -> dict[UUID, Wallet]:
    """Lock wallet rows in stable order so concurrent spends cannot race on PostgreSQL."""
    unique_ids = sorted(set(wallet_ids), key=str)
    wallets = (
        db.query(Wallet)
        .filter(Wallet.id.in_(unique_ids))
        .order_by(Wallet.id)
        .with_for_update()
        .all()
    )
    wallet_map = {wallet.id: wallet for wallet in wallets}
    missing = set(unique_ids) - set(wallet_map)
    if missing:
        raise HTTPException(status_code=404, detail="Wallet not found")
    return wallet_map


def spendable_balance(wallet: Wallet) -> Decimal:
    return money(wallet.purchased_balance) + money(wallet.earned_balance)


def credit_wallet(
    db: Session,
    wallet: Wallet,
    amount: Decimal,
    balance_type: str,
    transaction: LedgerTransaction,
    description: str,
) -> None:
    amount = money(amount)
    if balance_type == "purchased":
        wallet.purchased_balance = money(wallet.purchased_balance) + amount
    elif balance_type == "earned":
        wallet.earned_balance = money(wallet.earned_balance) + amount
        wallet.reward_earned_total = money(wallet.reward_earned_total) + amount
    elif balance_type == "locked":
        wallet.locked_balance = money(wallet.locked_balance) + amount
        wallet.reward_earned_total = money(wallet.reward_earned_total) + amount
    elif balance_type == "gas":
        wallet.purchased_balance = money(wallet.purchased_balance) + amount
    elif balance_type == "reserve":
        wallet.purchased_balance = money(wallet.purchased_balance) + amount
    else:
        raise ValueError(f"Unsupported balance type: {balance_type}")
    db.add(WalletEntry(
        transaction_id=transaction.id,
        wallet_id=wallet.id,
        entry_type="CREDIT",
        amount=amount,
        balance_type=balance_type,
        description=description,
    ))


def debit_spendable(
    db: Session,
    wallet: Wallet,
    amount: Decimal,
    transaction: LedgerTransaction,
    description: str,
) -> None:
    amount = money(amount)
    if wallet.status != "active":
        raise HTTPException(status_code=423, detail="Wallet is not active")
    current_spendable = spendable_balance(wallet)
    if current_spendable < amount:
        raise HTTPException(
            status_code=402,
            detail={
                "code": "insufficient_balance",
                "message": "Please recharge Orca Coins",
                "required_amount": str(amount),
                "spendable_balance": str(current_spendable),
                "shortfall": str(money(amount - current_spendable)),
            },
        )

    purchased_debit = min(money(wallet.purchased_balance), amount)
    earned_debit = amount - purchased_debit

    if purchased_debit:
        wallet.purchased_balance = money(wallet.purchased_balance) - purchased_debit
        db.add(WalletEntry(
            transaction_id=transaction.id,
            wallet_id=wallet.id,
            entry_type="DEBIT",
            amount=purchased_debit,
            balance_type="purchased",
            description=description,
        ))
    if earned_debit:
        wallet.earned_balance = money(wallet.earned_balance) - earned_debit
        db.add(WalletEntry(
            transaction_id=transaction.id,
            wallet_id=wallet.id,
            entry_type="DEBIT",
            amount=earned_debit,
            balance_type="earned",
            description=description,
        ))


def create_transaction(
    db: Session,
    transaction_type: str,
    gross_amount: Decimal,
    from_wallet_id: UUID | None = None,
    to_wallet_id: UUID | None = None,
    reference_id: UUID | None = None,
    platform_gas: Decimal = Decimal("0"),
    receiver_reward: Decimal = Decimal("0"),
    reserve_amount: Decimal = Decimal("0"),
    idempotency_key: str | None = None,
    metadata: dict | None = None,
) -> LedgerTransaction:
    transaction = LedgerTransaction(
        transaction_type=transaction_type,
        reference_id=reference_id,
        from_wallet_id=from_wallet_id,
        to_wallet_id=to_wallet_id,
        gross_amount=money(gross_amount),
        platform_gas=money(platform_gas),
        receiver_reward=money(receiver_reward),
        reserve_amount=money(reserve_amount),
        idempotency_key=idempotency_key,
        transaction_metadata=metadata or {},
    )
    db.add(transaction)
    db.flush()
    return transaction


def transaction_entry_totals(db: Session, transaction_id: UUID) -> dict[str, Decimal]:
    debit_total = (
        db.query(func.coalesce(func.sum(WalletEntry.amount), 0))
        .filter(WalletEntry.transaction_id == transaction_id, WalletEntry.entry_type == "DEBIT")
        .scalar()
    )
    credit_total = (
        db.query(func.coalesce(func.sum(WalletEntry.amount), 0))
        .filter(WalletEntry.transaction_id == transaction_id, WalletEntry.entry_type == "CREDIT")
        .scalar()
    )
    return {"debit_total": money(debit_total), "credit_total": money(credit_total)}


def expected_credit_total(transaction: LedgerTransaction) -> Decimal:
    if transaction.transaction_type == "message_send":
        return money(transaction.receiver_reward) + money(transaction.platform_gas) + money(transaction.reserve_amount)
    return money(transaction.gross_amount)


def audit_transaction(db: Session, transaction: LedgerTransaction) -> dict:
    totals = transaction_entry_totals(db, transaction.id)
    expected_debit = money(transaction.gross_amount) if transaction.from_wallet_id else Decimal("0.000000")
    expected_credit = expected_credit_total(transaction)
    is_balanced = totals["debit_total"] == expected_debit and totals["credit_total"] == expected_credit
    return {
        "transaction_id": transaction.id,
        "transaction_type": transaction.transaction_type,
        "is_balanced": is_balanced,
        "expected_debit": expected_debit,
        "actual_debit": totals["debit_total"],
        "expected_credit": expected_credit,
        "actual_credit": totals["credit_total"],
    }


def audit_recent_transactions(db: Session, limit: int = 100) -> dict:
    transactions = (
        db.query(LedgerTransaction)
        .order_by(LedgerTransaction.created_at.desc())
        .limit(limit)
        .all()
    )
    results = [audit_transaction(db, transaction) for transaction in transactions]
    imbalanced = [result for result in results if not result["is_balanced"]]
    return {
        "checked": len(results),
        "imbalanced_count": len(imbalanced),
        "imbalanced": imbalanced,
    }
