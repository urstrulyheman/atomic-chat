from decimal import Decimal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "OrcaChatCoin"
    env: str = "development"
    auto_create_tables: bool = True
    cors_origins: str = "http://127.0.0.1:5173,http://localhost:5173"
    allowed_hosts: str = "127.0.0.1,localhost,testserver"
    database_url: str = "sqlite:///./orca_chat_dev.db"
    redis_url: str = "redis://localhost:6379/0"
    readiness_check_redis: bool = False
    access_log_enabled: bool = True
    hsts_enabled: bool = False
    hsts_max_age_seconds: int = 31536000
    max_request_body_bytes: int = 1048576

    jwt_secret: str = "change_me"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60

    razorpay_key_id: str = "rzp_test_xxx"
    razorpay_key_secret: str = "xxx"
    razorpay_webhook_secret: str = "xxx"
    razorpay_webhook_max_bytes: int = 65536
    enable_dev_payment_capture: bool = True
    enable_public_user_directory: bool = True
    enable_dev_user_seed: bool = True

    message_default_cost: Decimal = Decimal("1.0")
    message_receiver_reward: Decimal = Decimal("0.65")
    message_platform_gas: Decimal = Decimal("0.25")
    message_reserve_reward: Decimal = Decimal("0.10")
    message_max_content_length: int = 4000
    message_max_sends_per_minute: int = 20
    message_idempotency_key_max_length: int = 120
    message_idempotency_key_pattern: str = "^[A-Za-z0-9._:-]+$"
    p2p_transfer_gas_percent: Decimal = Decimal("0.02")

    reward_lock_days: int = 7
    new_user_daily_reward_cap: Decimal = Decimal("25")
    verified_user_daily_reward_cap: Decimal = Decimal("250")
    new_user_daily_free_messages: int = 20
    verified_user_daily_free_messages: int = 100
    premium_user_daily_free_messages: int = 500
    welcome_bonus_coins: Decimal = Decimal("20")
    otp_expire_minutes: int = 5
    otp_resend_cooldown_seconds: int = 30
    otp_max_verify_attempts: int = 5
    otp_max_sends_per_hour: int = 5
    otp_max_ip_sends_per_hour: int = 300
    auth_max_accounts_per_device: int = 3
    otp_provider: str = "dev"
    otp_code_length: int = 6
    msg91_auth_key: str = ""
    msg91_template_id: str = ""
    msg91_sender_id: str = "ORCACH"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    @property
    def is_production(self) -> bool:
        return self.env.lower() == "production"

    @property
    def cors_origin_list(self) -> list[str]:
        return _csv(self.cors_origins)

    @property
    def allowed_host_list(self) -> list[str]:
        return _csv(self.allowed_hosts)

    @model_validator(mode="after")
    def validate_production_settings(self) -> "Settings":
        if not self.is_production:
            return self

        if self.jwt_secret == "change_me" or len(self.jwt_secret) < 32:
            raise ValueError("JWT_SECRET must be changed to a strong secret in production")
        if self.auto_create_tables:
            raise ValueError("AUTO_CREATE_TABLES must be false in production; run Alembic migrations instead")
        if "*" in self.cors_origin_list:
            raise ValueError("CORS_ORIGINS must not include '*' in production")
        if "*" in self.allowed_host_list:
            raise ValueError("ALLOWED_HOSTS must not include '*' in production")
        if not self.hsts_enabled:
            raise ValueError("HSTS_ENABLED must be true in production")
        if self.otp_provider == "dev":
            raise ValueError("OTP_PROVIDER must use a real provider in production")
        if self.otp_provider == "msg91" and (not self.msg91_auth_key or not self.msg91_template_id):
            raise ValueError("MSG91_AUTH_KEY and MSG91_TEMPLATE_ID are required in production")
        if self.razorpay_key_id == "rzp_test_xxx" or not self.razorpay_key_id:
            raise ValueError("RAZORPAY_KEY_ID must be configured in production")
        if self.razorpay_key_secret == "xxx" or not self.razorpay_key_secret:
            raise ValueError("RAZORPAY_KEY_SECRET must be configured in production")
        if self.razorpay_webhook_secret == "xxx" or not self.razorpay_webhook_secret:
            raise ValueError("RAZORPAY_WEBHOOK_SECRET must be configured in production")
        if self.enable_dev_payment_capture:
            raise ValueError("ENABLE_DEV_PAYMENT_CAPTURE must be false in production")
        if self.enable_public_user_directory:
            raise ValueError("ENABLE_PUBLIC_USER_DIRECTORY must be false in production")
        if self.enable_dev_user_seed:
            raise ValueError("ENABLE_DEV_USER_SEED must be false in production")
        return self


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


settings = Settings()
