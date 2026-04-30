import hashlib
import json
from datetime import date, datetime, time, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import LedgerTransaction, SettlementHash, WalletEntry


def generate_daily_settlement_hash(db: Session, settlement_date: date, commit: bool = True) -> SettlementHash:
    start_at = datetime.combine(settlement_date, time.min, tzinfo=timezone.utc)
    end_at = datetime.combine(settlement_date, time.max, tzinfo=timezone.utc)
    transactions = (
        db.query(LedgerTransaction)
        .filter(LedgerTransaction.created_at >= start_at, LedgerTransaction.created_at <= end_at)
        .order_by(LedgerTransaction.created_at.asc(), LedgerTransaction.id.asc())
        .all()
    )
    transaction_ids = [transaction.id for transaction in transactions]
    entries = []
    if transaction_ids:
        entries = (
            db.query(WalletEntry)
            .filter(WalletEntry.transaction_id.in_(transaction_ids))
            .order_by(WalletEntry.created_at.asc(), WalletEntry.id.asc())
            .all()
        )

    payload = {
        "settlement_date": settlement_date.isoformat(),
        "transactions": [_transaction_payload(transaction) for transaction in transactions],
        "entries": [_entry_payload(entry) for entry in entries],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()

    settlement = (
        db.query(SettlementHash)
        .filter(SettlementHash.settlement_date == settlement_date)
        .one_or_none()
    )
    if settlement is None:
        settlement = SettlementHash(settlement_date=settlement_date)
        db.add(settlement)

    settlement.ledger_hash = digest
    settlement.transaction_count = len(transactions)
    settlement.entry_count = len(entries)
    settlement.status = "generated"
    if commit:
        db.commit()
        db.refresh(settlement)
    else:
        db.flush()
    return settlement


def _transaction_payload(transaction: LedgerTransaction) -> dict:
    return {
        "id": str(transaction.id),
        "transaction_type": transaction.transaction_type,
        "reference_id": _string_or_none(transaction.reference_id),
        "from_wallet_id": _string_or_none(transaction.from_wallet_id),
        "to_wallet_id": _string_or_none(transaction.to_wallet_id),
        "gross_amount": _decimal_string(transaction.gross_amount),
        "platform_gas": _decimal_string(transaction.platform_gas),
        "receiver_reward": _decimal_string(transaction.receiver_reward),
        "reserve_amount": _decimal_string(transaction.reserve_amount),
        "status": transaction.status,
        "metadata": transaction.transaction_metadata or {},
        "created_at": transaction.created_at.isoformat(),
    }


def _entry_payload(entry: WalletEntry) -> dict:
    return {
        "id": str(entry.id),
        "transaction_id": str(entry.transaction_id),
        "wallet_id": str(entry.wallet_id),
        "entry_type": entry.entry_type,
        "amount": _decimal_string(entry.amount),
        "balance_type": entry.balance_type,
        "description": entry.description,
        "created_at": entry.created_at.isoformat(),
    }


def _decimal_string(value) -> str:
    return str(Decimal(str(value)).quantize(Decimal("0.000001")))


def _string_or_none(value) -> str | None:
    if value is None:
        return None
    return str(value)
