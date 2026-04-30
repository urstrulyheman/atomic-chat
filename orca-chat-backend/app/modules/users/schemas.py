from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class UserOut(BaseModel):
    id: UUID
    phone: str
    email: str | None
    name: str | None
    username: str | None
    avatar_url: str | None
    trust_score: int
    role: str
    status: str

    model_config = {"from_attributes": True}


class PublicUserOut(BaseModel):
    id: UUID
    name: str | None
    username: str | None
    avatar_url: str | None
    trust_score: int

    model_config = {"from_attributes": True}


class ProfileUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    username: str | None = Field(default=None, min_length=3, max_length=50, pattern=r"^[A-Za-z0-9_]+$")
    email: str | None = Field(default=None, max_length=255)
    avatar_url: str | None = Field(default=None, max_length=2000)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("Name cannot be blank")
        return normalized

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if "@" not in normalized or normalized.startswith("@") or normalized.endswith("@"):
            raise ValueError("Invalid email address")
        local, domain = normalized.rsplit("@", 1)
        if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
            raise ValueError("Invalid email address")
        return normalized

    @field_validator("avatar_url")
    @classmethod
    def validate_avatar_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized.startswith(("https://", "http://")):
            raise ValueError("Avatar URL must use http or https")
        return normalized
