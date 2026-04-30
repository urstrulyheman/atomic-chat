import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.dependencies import get_current_user
from app.models import PaymentOrder, User
from app.modules.payments import schemas, service

router = APIRouter(prefix="/payments", tags=["payments"])


@router.get("/recharge-packs", response_model=list[schemas.RechargePackOut])
def recharge_packs(current_user: User = Depends(get_current_user)):
    return service.list_recharge_packs()


@router.post("/razorpay/order", response_model=schemas.RechargeOrderResponse)
def create_razorpay_order(payload: schemas.RechargeOrderRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    order = service.create_order(db, current_user.id, payload.pack_id)
    return {
        "payment_order_id": order.id,
        "gateway_order_id": order.gateway_order_id,
        "amount": order.amount,
        "currency": order.currency,
        "coins": order.coins_to_credit,
        "razorpay_key_id": settings.razorpay_key_id,
    }


@router.post("/razorpay/webhook")
async def razorpay_webhook(request: Request, x_razorpay_signature: str = Header(default=""), db: Session = Depends(get_db)):
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > settings.razorpay_webhook_max_bytes:
                raise HTTPException(status_code=413, detail="Webhook payload too large")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length header") from exc

    raw_body = await request.body()
    if len(raw_body) > settings.razorpay_webhook_max_bytes:
        raise HTTPException(status_code=413, detail="Webhook payload too large")

    return {"status": service.handle_webhook(db, raw_body, x_razorpay_signature)}


@router.post("/dev/capture")
def dev_capture(payload: schemas.DevCaptureRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not settings.enable_dev_payment_capture or settings.is_production:
        raise HTTPException(status_code=404, detail="Not found")

    payment_order = (
        db.query(PaymentOrder)
        .filter(PaymentOrder.gateway_order_id == payload.gateway_order_id, PaymentOrder.user_id == current_user.id)
        .one_or_none()
    )
    if payment_order is None:
        return {"status": "not_found"}
    gateway_payment_id = payload.gateway_payment_id or f"pay_dev_{uuid.uuid4().hex}"
    return {"status": service.credit_successful_payment(db, payment_order, gateway_payment_id)}


@router.get("/history", response_model=list[schemas.PaymentOrderOut])
def history(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    status: Literal["created", "success", "failed"] | None = Query(default=None),
    gateway: str | None = Query(default=None, min_length=1, max_length=50),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    start_at, end_before = history_window(start_date, end_date)
    query = db.query(PaymentOrder).filter(PaymentOrder.user_id == current_user.id)
    if status:
        query = query.filter(PaymentOrder.status == status)
    if gateway:
        gateway = normalize_query_filter("gateway", gateway).lower()
        query = query.filter(PaymentOrder.gateway == gateway)
    if start_at:
        query = query.filter(PaymentOrder.created_at >= start_at)
    if end_before:
        query = query.filter(PaymentOrder.created_at < end_before)
    return query.order_by(PaymentOrder.created_at.desc()).offset(offset).limit(limit).all()


def normalize_query_filter(name: str, value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise HTTPException(status_code=422, detail=f"{name} cannot be blank")
    return normalized


def history_window(start_date: date | None, end_date: date | None) -> tuple[datetime | None, datetime | None]:
    if start_date and end_date and start_date > end_date:
        raise HTTPException(status_code=400, detail="start_date must be before or equal to end_date")
    start_at = datetime.combine(start_date, time.min, tzinfo=timezone.utc) if start_date else None
    end_before = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=timezone.utc) if end_date else None
    return start_at, end_before
