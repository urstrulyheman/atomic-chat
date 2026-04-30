from datetime import datetime
import re
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


PHONE_PATTERN = re.compile(r"^\+[1-9]\d{7,14}$")
OTP_PATTERN = re.compile(r"^\d{6}$")


def normalize_phone(value: str) -> str:
    normalized = value.strip()
    if not PHONE_PATTERN.fullmatch(normalized):
        raise ValueError("Phone must be in E.164 format, for example +919876543210")
    return normalized


def validate_otp(value: str) -> str:
    if not OTP_PATTERN.fullmatch(value):
        raise ValueError("OTP must be a 6 digit code")
    return value


class SendOtpRequest(BaseModel):
    phone: str = Field(min_length=8, max_length=20)

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        return normalize_phone(value)


class SendOtpResponse(BaseModel):
    challenge_id: UUID
    dev_otp: str


class VerifyOtpRequest(BaseModel):
    phone: str = Field(min_length=8, max_length=20)
    otp: str = Field(min_length=6, max_length=6)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    username: str | None = Field(default=None, min_length=3, max_length=50, pattern=r"^[A-Za-z0-9_]+$")
    device_label: str | None = Field(default=None, max_length=120)

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        return normalize_phone(value)

    @field_validator("otp")
    @classmethod
    def validate_otp_code(cls, value: str) -> str:
        return validate_otp(value)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip().lower()

    @field_validator("device_label")
    @classmethod
    def normalize_device_label(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class AuthUser(BaseModel):
    id: UUID
    phone: str
    name: str | None
    username: str | None
    trust_score: int
    role: str
    status: str

    model_config = {"from_attributes": True}


class VerifyOtpResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: AuthUser


class SessionOut(BaseModel):
    id: UUID
    device_label: str | None
    user_agent: str | None
    ip_address: str | None
    status: str
    expires_at: datetime
    last_seen_at: datetime
    created_at: datetime

    model_config = {"from_attributes": True}
