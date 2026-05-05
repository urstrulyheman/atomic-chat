from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.config import settings


class CreateConversationRequest(BaseModel):
    receiver_id: UUID


class ConversationOut(BaseModel):
    id: UUID
    conversation_type: str
    created_by: UUID
    created_at: datetime

    model_config = {"from_attributes": True}


class ConversationLastMessageOut(BaseModel):
    id: UUID
    sender_id: UUID
    receiver_id: UUID
    content: str
    status: str
    created_at: datetime


class ConversationListOut(ConversationOut):
    last_message: ConversationLastMessageOut | None
    unread_count: int


class ChatParticipantOut(BaseModel):
    id: UUID
    name: str | None
    username: str | None
    avatar_url: str | None
    trust_score: int
    status: str


class ConversationDetailOut(ConversationOut):
    participants: list[ChatParticipantOut]
    blocked_by_me: bool
    blocked_me: bool
    can_send: bool


class SendMessageRequest(BaseModel):
    receiver_id: UUID
    content: str = Field(min_length=1, max_length=settings.message_max_content_length)
    use_free_quota: bool = False

    @field_validator("content", mode="before")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        if not isinstance(value, str):
            return value
        return value.strip()


class MessagePriceQuoteRequest(BaseModel):
    content: str = Field(min_length=1, max_length=settings.message_max_content_length)

    @field_validator("content", mode="before")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        if not isinstance(value, str):
            return value
        return value.strip()


class MessagePriceQuoteOut(BaseModel):
    pricing_model: str
    token_count: int
    tokens_per_unit: int
    billing_units: int
    message_cost: Decimal
    receiver_reward: Decimal
    platform_gas: Decimal
    reserve_reward: Decimal
    spendable_balance: Decimal
    can_afford: bool


class MessageOut(BaseModel):
    id: UUID
    conversation_id: UUID
    sender_id: UUID
    receiver_id: UUID
    content: str
    status: str
    coin_cost: Decimal
    transaction_id: UUID | None
    delivered_at: datetime | None
    read_at: datetime | None
    created_at: datetime


class ReadReceiptResponse(BaseModel):
    status: str
    delivered_at: datetime | None = None
    read_at: datetime | None = None


class ConversationReadResponse(BaseModel):
    status: str
    read_count: int
    read_at: datetime | None = None


class ReportMessageRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=100)
    description: str | None = Field(default=None, max_length=1000)

    @field_validator("reason", mode="before")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        if not isinstance(value, str):
            return value
        return value.strip().lower()

    @field_validator("description", mode="before")
    @classmethod
    def normalize_description(cls, value: str | None) -> str | None:
        if value is None or not isinstance(value, str):
            return value
        description = value.strip()
        return description or None
