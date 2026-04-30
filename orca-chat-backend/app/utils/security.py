from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import AuthSession, User
from app.utils.time import as_utc, utc_now

bearer = HTTPBearer()


def create_access_token(
    db: Session,
    user_id: UUID,
    device_label: str | None = None,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> str:
    jti = uuid4().hex
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    db.add(AuthSession(
        user_id=user_id,
        jti=jti,
        device_label=device_label,
        user_agent=user_agent,
        ip_address=ip_address,
        expires_at=expires_at,
    ))
    db.flush()
    return jwt.encode(
        {"sub": str(user_id), "jti": jti, "exp": expires_at},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer),
    db: Session = Depends(get_db),
) -> User:
    return authenticate_access_token(credentials.credentials, db)


def authenticate_access_token(token: str, db: Session) -> User:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
        user_id = UUID(payload["sub"])
        jti = payload["jti"]
    except (JWTError, KeyError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc

    session = db.query(AuthSession).filter(AuthSession.jti == jti, AuthSession.user_id == user_id).one_or_none()
    if session is None or session.status != "active" or as_utc(session.expires_at) <= utc_now():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    session.last_seen_at = utc_now()
    db.commit()

    user = db.get(User, user_id)
    if user is None or user.status != "active":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Inactive user")
    return user


def revoke_access_token(credentials: HTTPAuthorizationCredentials, db: Session) -> None:
    try:
        payload = jwt.decode(
            credentials.credentials,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
        jti = payload["jti"]
    except (JWTError, KeyError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc

    session = db.query(AuthSession).filter(AuthSession.jti == jti).one_or_none()
    if session:
        session.status = "revoked"
        session.revoked_at = utc_now()
        db.commit()
