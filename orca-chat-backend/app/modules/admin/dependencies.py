from fastapi import Depends, HTTPException

from app.dependencies import get_current_user
from app.models import User


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user
