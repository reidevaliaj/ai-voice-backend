from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app_config import FASTAPI_INTERNAL_API_KEY
from db import get_db
from services.tenants import resolve_session_config

router = APIRouter()


class SessionConfigRequest(BaseModel):
    tenant_id: str = ""
    tenant_slug: str = ""
    config_version: int | None = None
    room_name: str = ""
    caller_id: str = ""
    called_number: str = ""
    call_sid: str = ""


def _require_internal_api_key(x_internal_api_key: str | None = Header(default=None)) -> None:
    if FASTAPI_INTERNAL_API_KEY and x_internal_api_key != FASTAPI_INTERNAL_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid internal API key")


@router.post("/agent/session-config")
async def agent_session_config(
    payload: SessionConfigRequest,
    db: Session = Depends(get_db),
    _: None = Depends(_require_internal_api_key),
):
    try:
        snapshot = resolve_session_config(
            db,
            tenant_id=payload.tenant_id,
            tenant_slug=payload.tenant_slug,
            config_version=payload.config_version,
            room_name=payload.room_name,
            caller_id=payload.caller_id,
            called_number=payload.called_number,
            call_sid=payload.call_sid,
        )
        return {"ok": True, **snapshot}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
