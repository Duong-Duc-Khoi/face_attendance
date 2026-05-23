"""
Admin APIs for external integrations.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.services.integration_settings import list_ai_provider_settings, update_ai_provider_setting

router = APIRouter(prefix="/api/integrations", tags=["integrations"])


class AIKeyUpdate(BaseModel):
    api_key: Optional[str] = None
    model: Optional[str] = None
    is_enabled: bool = True
    clear_key: bool = False


def _require_admin(user: User):
    if user.role != "admin":
        raise HTTPException(403, "Yêu cầu quyền admin")


@router.get("/ai-keys")
def api_list_ai_keys(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    return {"providers": list_ai_provider_settings(db)}


@router.put("/ai-keys/{provider}")
def api_update_ai_key(
    provider: str,
    body: AIKeyUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_admin(current_user)
    try:
        return update_ai_provider_setting(
            provider=provider,
            db=db,
            model=body.model or "",
            api_key=body.api_key,
            is_enabled=body.is_enabled,
            clear_key=body.clear_key,
            updated_by=current_user.email,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
