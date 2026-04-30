from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.dependencies import get_current_user
from app.models import User, Wallet
from app.modules.users.schemas import ProfileUpdate, PublicUserOut, UserOut

router = APIRouter(prefix="/users", tags=["users"])


@router.get("", response_model=list[UserOut])
def list_users(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not settings.enable_public_user_directory:
        raise HTTPException(status_code=403, detail="Public user directory is disabled")
    return (
        db.query(User)
        .filter(User.id != current_user.id, User.status == "active")
        .order_by(User.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )


@router.get("/search", response_model=list[PublicUserOut])
def search_users(
    q: str = Query(min_length=2, max_length=50),
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    normalized_q = q.strip().lower()
    if len(normalized_q) < 2:
        raise HTTPException(status_code=422, detail="q must contain at least 2 non-whitespace characters")
    query = f"%{normalized_q}%"
    return (
        db.query(User)
        .filter(
            User.id != current_user.id,
            User.status == "active",
            or_(
                User.name.ilike(query),
                User.username.ilike(query),
                User.phone.ilike(query),
            ),
        )
        .order_by(User.trust_score.desc(), User.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )


@router.patch("/me", response_model=UserOut)
def update_profile(payload: ProfileUpdate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if payload.username is not None:
        username = payload.username.strip().lower()
        existing = (
            db.query(User)
            .filter(User.username == username, User.id != current_user.id)
            .one_or_none()
        )
        if existing is not None:
            raise HTTPException(status_code=409, detail="Username is already taken")
        current_user.username = username
    if payload.email is not None:
        existing = (
            db.query(User)
            .filter(User.email == payload.email, User.id != current_user.id)
            .one_or_none()
        )
        if existing is not None:
            raise HTTPException(status_code=409, detail="Email is already taken")

    for field in ("name", "username", "email", "avatar_url"):
        if field == "username":
            continue
        value = getattr(payload, field)
        if value is not None:
            setattr(current_user, field, value.strip() if isinstance(value, str) else value)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Profile value already exists") from exc
    db.refresh(current_user)
    return current_user


@router.post("/dev-seed", response_model=list[UserOut])
def seed_demo_users(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not settings.enable_dev_user_seed or settings.is_production:
        raise HTTPException(status_code=404, detail="Not found")
    demo_users = [
        {"phone": "+919000000101", "name": "Alice Chen", "username": "alice", "trust_score": 80},
        {"phone": "+919000000102", "name": "Bob Martin", "username": "bob", "trust_score": 72},
        {"phone": "+919000000103", "name": "Carol White", "username": "carol", "trust_score": 88},
        {"phone": "+919000000104", "name": "Dave Kim", "username": "dave", "trust_score": 61},
    ]
    for item in demo_users:
        user = db.query(User).filter(User.phone == item["phone"]).one_or_none()
        if user is None:
            user = User(**item)
            db.add(user)
            db.flush()
            db.add(Wallet(user_id=user.id, purchased_balance=20))
    db.commit()
    return list_users(current_user=current_user, db=db)
