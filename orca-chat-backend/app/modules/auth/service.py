from decimal import Decimal
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.models import AuthSession, OtpChallenge, User, Wallet
from app.modules.auth.otp_provider import get_otp_provider
from app.utils.ledger import create_transaction, credit_wallet
from app.utils.security import create_access_token
from app.utils.time import as_utc, utc_now


def send_otp(db: Session, phone: str, ip_address: str | None = None) -> OtpChallenge:
    now = utc_now()
    latest = (
        db.query(OtpChallenge)
        .filter(OtpChallenge.phone == phone)
        .order_by(OtpChallenge.created_at.desc())
        .first()
    )
    if latest and latest.status == "pending":
        cooldown_until = as_utc(latest.last_sent_at) + timedelta(seconds=settings.otp_resend_cooldown_seconds)
        if now < cooldown_until:
            raise HTTPException(status_code=429, detail="Please wait before requesting another OTP")

    hour_ago = now - timedelta(hours=1)
    sends_in_hour = (
        db.query(OtpChallenge)
        .filter(OtpChallenge.phone == phone, OtpChallenge.created_at >= hour_ago)
        .count()
    )
    if sends_in_hour >= settings.otp_max_sends_per_hour:
        raise HTTPException(status_code=429, detail="Too many OTP requests. Try again later")

    if ip_address:
        ip_sends_in_hour = (
            db.query(OtpChallenge)
            .filter(OtpChallenge.ip_address == ip_address, OtpChallenge.created_at >= hour_ago)
            .count()
        )
        if ip_sends_in_hour >= settings.otp_max_ip_sends_per_hour:
            raise HTTPException(status_code=429, detail="Too many OTP requests from this network. Try again later")

    try:
        delivery = get_otp_provider().send(phone)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    challenge = OtpChallenge(
        phone=phone,
        ip_address=ip_address,
        otp_code=delivery.code,
        last_sent_at=now,
        expires_at=now + timedelta(minutes=settings.otp_expire_minutes),
    )
    db.add(challenge)
    db.commit()
    db.refresh(challenge)
    return challenge


def verify_otp(
    db: Session,
    phone: str,
    otp: str,
    name: str | None,
    username: str | None,
    device_label: str | None = None,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> tuple[str, User]:
    challenge = (
        db.query(OtpChallenge)
        .filter(OtpChallenge.phone == phone)
        .order_by(OtpChallenge.created_at.desc())
        .first()
    )
    if challenge is None:
        raise HTTPException(status_code=400, detail="Invalid OTP")
    if challenge.status == "blocked":
        raise HTTPException(status_code=429, detail="Too many OTP attempts")
    if challenge.status != "pending":
        raise HTTPException(status_code=400, detail="Invalid OTP")
    if utc_now() > as_utc(challenge.expires_at):
        challenge.status = "expired"
        db.commit()
        raise HTTPException(status_code=400, detail="OTP expired")
    if challenge.attempts >= settings.otp_max_verify_attempts:
        challenge.status = "blocked"
        db.commit()
        raise HTTPException(status_code=429, detail="Too many OTP attempts")
    if challenge.otp_code != otp:
        challenge.attempts += 1
        if challenge.attempts >= settings.otp_max_verify_attempts:
            challenge.status = "blocked"
        db.commit()
        raise HTTPException(status_code=400, detail="Invalid OTP")

    user = db.query(User).filter(User.phone == phone).one_or_none()
    is_new = user is None
    if username:
        existing_username = db.query(User).filter(User.username == username).one_or_none()
        if existing_username is not None and (is_new or existing_username.id != user.id):
            raise HTTPException(status_code=409, detail="Username is already taken")
    if user is None:
        enforce_device_account_limit(db, device_label)
        user = User(phone=phone, name=name, username=username)
        db.add(user)
        db.flush()
        wallet = Wallet(user_id=user.id)
        db.add(wallet)
        db.flush()
        if settings.welcome_bonus_coins > Decimal("0"):
            transaction = create_transaction(
                db,
                transaction_type="welcome_bonus",
                to_wallet_id=wallet.id,
                gross_amount=settings.welcome_bonus_coins,
                metadata={"reason": "new_user_signup"},
            )
            credit_wallet(db, wallet, settings.welcome_bonus_coins, "purchased", transaction, "Welcome bonus")
    else:
        if user.status != "active":
            raise HTTPException(status_code=403, detail="User account is not active")
        if name:
            user.name = name
        if username:
            user.username = username

    token = create_access_token(db, user.id, device_label=device_label, user_agent=user_agent, ip_address=ip_address)
    challenge.status = "verified"
    db.commit()
    db.refresh(user)
    return token, user


def enforce_device_account_limit(db: Session, device_label: str | None) -> None:
    if not device_label or settings.auth_max_accounts_per_device <= 0:
        return
    account_count = (
        db.query(func.count(func.distinct(AuthSession.user_id)))
        .filter(AuthSession.device_label == device_label)
        .scalar()
        or 0
    )
    if account_count >= settings.auth_max_accounts_per_device:
        raise HTTPException(status_code=429, detail="Too many accounts registered from this device")
