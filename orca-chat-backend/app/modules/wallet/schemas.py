from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class WalletBalance(BaseModel):
    purchased_balance: Decimal
    earned_balance: Decimal
    locked_balance: Decimal
    spendable_balance: Decimal
    gas_paid_total: Decimal
    reward_earned_total: Decimal
    status: str


class TransactionOut(BaseModel):
    id: UUID
    transaction_type: str
    reference_id: UUID | None
    from_wallet_id: UUID | None
    to_wallet_id: UUID | None
    direction: str
    gross_amount: Decimal
    platform_gas: Decimal
    receiver_reward: Decimal
    reserve_amount: Decimal
    status: str
    metadata: dict | None
    created_at: datetime

    model_config = {"from_attributes": True}


class WalletEntryOut(BaseModel):
    id: UUID
    transaction_id: UUID
    entry_type: str
    direction: str
    amount: Decimal
    signed_amount: Decimal
    balance_type: str | None
    description: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class WalletTransferRequest(BaseModel):
    receiver_id: UUID
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=6)
    note: str | None = Field(default=None, max_length=250)

    @field_validator("note")
    @classmethod
    def normalize_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None
