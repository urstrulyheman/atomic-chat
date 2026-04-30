import hmac
import json
import uuid
from decimal import Decimal
from hashlib import sha256

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.models import PaymentOrder
from app.utils.ledger import create_transaction, credit_wallet

RECHARGE_PACKS = {
    "starter_99": {"amount": Decimal("99.00"), "coins": Decimal("100.000000")},
    "growth_299": {"amount": Decimal("299.00"), "coins": Decimal("330.000000")},
    "power_999": {"amount": Decimal("999.00"), "coins": Decimal("1200.000000")},
}


def list_recharge_packs() -> list[dict]:
    return [
        {"id": pack_id, "amount": pack["amount"], "currency": "INR", "coins": pack["coins"]}
        for pack_id, pack in sorted(RECHARGE_PACKS.items(), key=lambda item: item[1]["amount"])
    ]


def create_order(db: Session, user_id, pack_id: str) -> PaymentOrder:
    pack = RECHARGE_PACKS.get(pack_id)
    if pack is None:
        raise HTTPException(status_code=404, detail="Recharge pack not found")

    gateway_order_id = f"order_dev_{user_id}_{pack_id}_{uuid.uuid4().hex}"
    if settings.razorpay_key_secret != "xxx":
        import razorpay

        client = razorpay.Client(auth=(settings.razorpay_key_id, settings.razorpay_key_secret))
        order = client.order.create({
            "amount": int(pack["amount"] * 100),
            "currency": "INR",
            "receipt": f"orca_{user_id}_{pack_id}",
            "payment_capture": 1,
        })
        gateway_order_id = order["id"]

    payment_order = PaymentOrder(
        user_id=user_id,
        gateway_order_id=gateway_order_id,
        amount=pack["amount"],
        currency="INR",
        coins_to_credit=pack["coins"],
    )
    db.add(payment_order)
    db.commit()
    db.refresh(payment_order)
    return payment_order


def credit_successful_payment(db: Session, payment_order: PaymentOrder, gateway_payment_id: str) -> str:
    try:
        existing = (
            db.query(PaymentOrder)
            .filter(PaymentOrder.gateway_payment_id == gateway_payment_id, PaymentOrder.status == "success")
            .one_or_none()
        )
        if existing:
            return "already_processed"
        if payment_order.status == "success":
            return "already_processed"
        if payment_order.status == "failed":
            return "payment_order_failed"

        wallet = payment_order.user.wallet if hasattr(payment_order, "user") else None
        if wallet is None:
            from app.models import Wallet

            wallet = db.query(Wallet).filter(Wallet.user_id == payment_order.user_id).with_for_update().one()

        transaction = create_transaction(
            db,
            transaction_type="recharge",
            to_wallet_id=wallet.id,
            gross_amount=payment_order.coins_to_credit,
            metadata={"razorpay_payment_id": gateway_payment_id, "gateway_order_id": payment_order.gateway_order_id},
        )
        credit_wallet(db, wallet, Decimal(str(payment_order.coins_to_credit)), "purchased", transaction, "Razorpay recharge")
        payment_order.gateway_payment_id = gateway_payment_id
        payment_order.status = "success"
        db.commit()
        return "credited"
    except Exception:
        db.rollback()
        raise


def verify_webhook_signature(raw_body: bytes, signature: str) -> None:
    if not settings.razorpay_webhook_secret or settings.razorpay_webhook_secret == "xxx":
        if settings.is_production:
            raise HTTPException(status_code=500, detail="Razorpay webhook secret is not configured")
        return
    expected = hmac.new(settings.razorpay_webhook_secret.encode(), raw_body, sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=400, detail="Invalid Razorpay signature")


def handle_webhook(db: Session, raw_body: bytes, signature: str) -> str:
    verify_webhook_signature(raw_body, signature)
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid webhook payload") from exc
    event = payload.get("event")
    if event not in {"payment.captured", "payment.failed"}:
        return "ignored"

    try:
        payment = payload["payload"]["payment"]["entity"]
        gateway_order_id = payment["order_id"]
        gateway_payment_id = payment["id"]
        gateway_amount = int(payment["amount"])
        gateway_currency = payment["currency"]
    except (KeyError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid payment webhook payload") from exc

    payment_order = db.query(PaymentOrder).filter(PaymentOrder.gateway_order_id == gateway_order_id).one_or_none()
    if payment_order is None:
        raise HTTPException(status_code=404, detail="Payment order not found")
    expected_amount = int(Decimal(str(payment_order.amount)) * 100)
    if gateway_amount != expected_amount or gateway_currency.upper() != payment_order.currency.upper():
        raise HTTPException(status_code=400, detail="Payment amount or currency mismatch")
    if event == "payment.failed":
        return mark_payment_failed(db, payment_order, gateway_payment_id)
    return credit_successful_payment(db, payment_order, gateway_payment_id)


def mark_payment_failed(db: Session, payment_order: PaymentOrder, gateway_payment_id: str | None = None) -> str:
    try:
        if payment_order.status == "success":
            return "already_processed"
        if payment_order.status == "failed":
            return "already_processed"
        payment_order.status = "failed"
        if gateway_payment_id:
            payment_order.gateway_payment_id = gateway_payment_id
        db.commit()
        return "failed"
    except Exception:
        db.rollback()
        raise
