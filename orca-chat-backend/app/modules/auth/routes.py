from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.models import AuthSession, User
from app.modules.auth import schemas, service
from app.utils.security import bearer, revoke_access_token
from app.utils.time import utc_now

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/send-otp", response_model=schemas.SendOtpResponse)
def send_otp(payload: schemas.SendOtpRequest, request: Request, db: Session = Depends(get_db)):
    challenge = service.send_otp(db, payload.phone, ip_address=request.client.host if request.client else None)
    return {"challenge_id": challenge.id, "dev_otp": challenge.otp_code if challenge.otp_code == "123456" else ""}


@router.post("/verify-otp", response_model=schemas.VerifyOtpResponse)
def verify_otp(
    payload: schemas.VerifyOtpRequest,
    request: Request,
    db: Session = Depends(get_db),
    user_agent: str | None = Header(default=None),
):
    token, user = service.verify_otp(
        db,
        payload.phone,
        payload.otp,
        payload.name,
        payload.username,
        device_label=payload.device_label,
        user_agent=user_agent,
        ip_address=request.client.host if request.client else None,
    )
    return {"access_token": token, "user": user}


@router.get("/me", response_model=schemas.AuthUser)
def me(current_user: User = Depends(get_current_user)):
    return current_user


@router.post("/logout")
def logout(
    credentials: HTTPAuthorizationCredentials = Depends(bearer),
    db: Session = Depends(get_db),
):
    revoke_access_token(credentials, db)
    return {"status": "ok"}


@router.get("/sessions", response_model=list[schemas.SessionOut])
def sessions(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return (
        db.query(AuthSession)
        .filter(AuthSession.user_id == current_user.id, AuthSession.status == "active")
        .order_by(AuthSession.last_seen_at.desc())
        .all()
    )


@router.post("/sessions/{session_id}/revoke")
def revoke_session(session_id: UUID, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    session = (
        db.query(AuthSession)
        .filter(AuthSession.id == session_id, AuthSession.user_id == current_user.id)
        .one_or_none()
    )
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    session.status = "revoked"
    session.revoked_at = utc_now()
    db.commit()
    return {"status": "revoked"}
