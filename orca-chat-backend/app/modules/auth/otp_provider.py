import random
from dataclasses import dataclass

from app.config import settings

DEV_OTP = "123456"


@dataclass(frozen=True)
class OtpDelivery:
    code: str
    provider: str
    provider_message_id: str | None = None
    dev_otp: str | None = None


class OtpProvider:
    provider_name = "base"

    def send(self, phone: str) -> OtpDelivery:
        raise NotImplementedError


class DevOtpProvider(OtpProvider):
    provider_name = "dev"

    def send(self, phone: str) -> OtpDelivery:
        return OtpDelivery(code=DEV_OTP, provider=self.provider_name, dev_otp=DEV_OTP)


class Msg91OtpProvider(OtpProvider):
    provider_name = "msg91"

    def send(self, phone: str) -> OtpDelivery:
        # Production integration point:
        # call MSG91 OTP API here using settings.msg91_auth_key/template_id/sender_id,
        # then persist the generated code for verification.
        code = generate_numeric_otp(settings.otp_code_length)
        if not settings.msg91_auth_key or not settings.msg91_template_id:
            raise RuntimeError("MSG91 OTP provider is not configured")
        raise NotImplementedError("MSG91 network delivery is not implemented in this MVP")


def generate_numeric_otp(length: int) -> str:
    lower = 10 ** (length - 1)
    upper = (10 ** length) - 1
    return str(random.randint(lower, upper))


def get_otp_provider() -> OtpProvider:
    if settings.otp_provider == "dev":
        return DevOtpProvider()
    if settings.otp_provider == "msg91":
        return Msg91OtpProvider()
    raise RuntimeError(f"Unsupported OTP provider: {settings.otp_provider}")
