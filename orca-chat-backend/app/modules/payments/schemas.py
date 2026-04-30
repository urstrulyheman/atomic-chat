from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class RechargeOrderRequest(BaseModel):
    pack_id: str = Field(min_length=1, max_length=80)

    @field_validator("pack_id", mode="before")
    @classmethod
    def normalize_pack_id(cls, value: str) -> str:
        if not isinstance(value, str):
            return value
        return value.strip().lower()


class RechargePackOut(BaseModel):
    id: str
    amount: Decimal
    currency: str
    coins: Decimal


class RechargeOrderResponse(BaseModel):
    payment_order_id: UUID
    gateway_order_id: str
    amount: Decimal
    currency: str
    coins: Decimal
    razorpay_key_id: str


class DevCaptureRequest(BaseModel):
    gateway_order_id: str = Field(min_length=1, max_length=255)
    gateway_payment_id: str | None = Field(default=None, max_length=255)

    @field_validator("gateway_order_id", mode="before")
    @classmethod
    def normalize_gateway_order_id(cls, value: str) -> str:
        if not isinstance(value, str):
            return value
        return value.strip()

    @field_validator("gateway_payment_id", mode="before")
    @classmethod
    def normalize_gateway_payment_id(cls, value: str | None) -> str | None:
        if value is None or not isinstance(value, str):
            return value
        gateway_payment_id = value.strip()
        return gateway_payment_id or None


class PaymentOrderOut(BaseModel):
    id: UUID
    gateway: str
    gateway_order_id: str | None
    gateway_payment_id: str | None
    amount: Decimal
    currency: str
    coins_to_credit: Decimal
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}
