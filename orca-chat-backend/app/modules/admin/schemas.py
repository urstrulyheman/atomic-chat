from pydantic import BaseModel, Field, field_validator


class StatusUpdateRequest(BaseModel):
    status: str = Field(max_length=30)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        status = value.strip().lower()
        if status not in {"open", "resolved", "dismissed"}:
            raise ValueError("Status must be open, resolved, or dismissed")
        return status


class AdminUserUpdateRequest(BaseModel):
    status: str | None = Field(default=None, max_length=30)
    role: str | None = Field(default=None, max_length=30)
    kyc_status: str | None = Field(default=None, max_length=30)
    trust_score: int | None = Field(default=None, ge=0, le=100)

    @field_validator("status")
    @classmethod
    def validate_user_status(cls, value: str | None) -> str | None:
        if value is None:
            return None
        status = value.strip().lower()
        if status not in {"active", "blocked"}:
            raise ValueError("Status must be active or blocked")
        return status

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: str | None) -> str | None:
        if value is None:
            return None
        role = value.strip().lower()
        if role not in {"user", "admin"}:
            raise ValueError("Role must be user or admin")
        return role

    @field_validator("kyc_status")
    @classmethod
    def validate_kyc_status(cls, value: str | None) -> str | None:
        if value is None:
            return None
        kyc_status = value.strip().lower()
        if kyc_status not in {"not_started", "pending", "verified", "approved", "rejected", "premium"}:
            raise ValueError("KYC status is not supported")
        return kyc_status
