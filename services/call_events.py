from typing import Any

from sqlalchemy.orm import Session

from models import CallEvent, Tenant
from services.tenants import normalize_phone_number


def log_call_event(
    session: Session,
    *,
    event_type: str,
    payload: dict[str, Any],
    tenant: Tenant | None = None,
    room_name: str = "",
) -> CallEvent:
    event = CallEvent(
        tenant_id=tenant.id if tenant else None,
        event_type=event_type,
        call_sid=str(payload.get("CallSid") or payload.get("call_sid") or ""),
        parent_call_sid=str(payload.get("ParentCallSid") or payload.get("parent_call_sid") or ""),
        sip_call_id=str(payload.get("SipCallId") or payload.get("sip_call_id") or ""),
        room_name=room_name or str(payload.get("room_name") or ""),
        call_status=str(payload.get("CallStatus") or payload.get("call_status") or ""),
        sip_response_code=str(payload.get("SipResponseCode") or payload.get("sip_response_code") or ""),
        caller_number=normalize_phone_number(str(payload.get("From") or payload.get("caller_id") or payload.get("caller_number") or "")),
        called_number=normalize_phone_number(str(payload.get("CalledVia") or payload.get("To") or payload.get("called_number") or "")),
        callback_timestamp=str(payload.get("Timestamp") or payload.get("timestamp") or ""),
        payload_json=payload,
    )
    session.add(event)
    session.flush()
    return event
