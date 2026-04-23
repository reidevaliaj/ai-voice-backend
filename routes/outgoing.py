from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app_config import (
    FASTAPI_INTERNAL_API_KEY,
    LIVEKIT_OUTGOING_SIP_PASSWORD,
    LIVEKIT_OUTGOING_SIP_URI,
    LIVEKIT_OUTGOING_SIP_USERNAME,
    PUBLIC_BASE_URL,
    TELNYX_API_KEY,
    TELNYX_OUTGOING_WEBHOOK_PATH,
)
from db import get_db
from outgoing_db import get_outgoing_db
from services.outgoing import (
    apply_telnyx_event_to_call,
    build_outgoing_runtime,
    get_outgoing_call,
    log_outgoing_event,
    mark_outgoing_call_error,
    mark_outgoing_call_status,
    save_outgoing_transcript,
)
from services.telnyx_voice import (
    decode_client_state,
    encode_client_state,
    flatten_voice_event,
    hangup_call,
    is_voice_event,
    telnyx_command_id,
    transfer_call,
)
from services.tenants import get_tenant_by_id, get_tenant_by_slug, normalize_phone_number

logger = logging.getLogger("outgoing")

router = APIRouter()


class OutgoingSessionConfigRequest(BaseModel):
    tenant_id: str = ""
    tenant_slug: str = ""
    outgoing_call_id: str = ""
    room_name: str = ""
    call_sid: str = ""


class OutgoingTranscriptPayload(BaseModel):
    tenant_id: str = ""
    tenant_slug: str = ""
    outgoing_call_id: str = ""
    call_sid: str = ""
    room_name: str = ""
    shutdown_reason: str = ""
    timestamp: int = 0
    transcript: str = ""
    messages: list[dict[str, Any]] = Field(default_factory=list)


def _require_internal_api_key(x_internal_api_key: str | None = Header(default=None)) -> None:
    if FASTAPI_INTERNAL_API_KEY and x_internal_api_key != FASTAPI_INTERNAL_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid internal API key")


def _public_url(path: str) -> str:
    return PUBLIC_BASE_URL.rstrip("/") + path


def _telnyx_outgoing_webhook_url() -> str:
    path = TELNYX_OUTGOING_WEBHOOK_PATH if TELNYX_OUTGOING_WEBHOOK_PATH.startswith("/") else f"/{TELNYX_OUTGOING_WEBHOOK_PATH}"
    return _public_url(path)


def _telnyx_transport_protocol(sip_uri: str) -> str:
    candidate = (sip_uri or "").lower()
    if "transport=tls" in candidate:
        return "TLS"
    if "transport=tcp" in candidate:
        return "TCP"
    return "UDP"


def _resolve_outgoing_tenant(db: Session, event_payload: dict[str, Any], client_state: dict[str, Any]):
    tenant = None
    tenant_id = str(client_state.get("tenant_id") or "")
    tenant_slug = str(client_state.get("tenant_slug") or "")
    if tenant_id:
        tenant = get_tenant_by_id(db, tenant_id)
    if tenant is None and tenant_slug:
        tenant = get_tenant_by_slug(db, tenant_slug)
    if tenant is None:
        tenant = get_tenant_by_slug(db, str(event_payload.get("tenant_slug") or ""))
    return tenant


def _requires_livekit_outgoing_target() -> None:
    if not (LIVEKIT_OUTGOING_SIP_URI and LIVEKIT_OUTGOING_SIP_USERNAME and LIVEKIT_OUTGOING_SIP_PASSWORD):
        raise RuntimeError("LIVEKIT outgoing SIP target is not configured")


def _is_primary_pstn_leg(call: Any, event_payload: dict[str, Any]) -> bool:
    return normalize_phone_number(str(event_payload.get("To") or event_payload.get("to") or "")) == normalize_phone_number(call.target_number)


@router.post("/agent/outgoing-session-config")
async def outgoing_agent_session_config(
    payload: OutgoingSessionConfigRequest,
    db: Session = Depends(get_db),
    outgoing_db: Session = Depends(get_outgoing_db),
    _: None = Depends(_require_internal_api_key),
):
    tenant = None
    if payload.tenant_id:
        tenant = get_tenant_by_id(db, payload.tenant_id)
    if tenant is None and payload.tenant_slug:
        tenant = get_tenant_by_slug(db, payload.tenant_slug)
    call = get_outgoing_call(
        outgoing_db,
        outgoing_call_id=payload.outgoing_call_id,
        telnyx_call_control_id=payload.call_sid,
        tenant_id=tenant.id if tenant else "",
    )
    if tenant is None and call is not None:
        tenant = get_tenant_by_id(db, call.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Unable to resolve tenant for outgoing call")

    runtime = build_outgoing_runtime(
        db,
        outgoing_db,
        tenant=tenant,
        call_control_id=payload.call_sid,
        outgoing_call_id=payload.outgoing_call_id,
        room_name=payload.room_name,
    )
    return {"ok": True, **runtime}


@router.post("/outgoing/events/transcript")
async def outgoing_transcript_event(
    payload: OutgoingTranscriptPayload,
    db: Session = Depends(get_db),
    outgoing_db: Session = Depends(get_outgoing_db),
    _: None = Depends(_require_internal_api_key),
):
    tenant = None
    if payload.tenant_id:
        tenant = get_tenant_by_id(db, payload.tenant_id)
    if tenant is None and payload.tenant_slug:
        tenant = get_tenant_by_slug(db, payload.tenant_slug)
    call = get_outgoing_call(
        outgoing_db,
        outgoing_call_id=payload.outgoing_call_id,
        telnyx_call_control_id=payload.call_sid,
        tenant_id=tenant.id if tenant else "",
    )
    if call is None:
        raise HTTPException(status_code=404, detail="Outgoing call not found")
    save_outgoing_transcript(
        outgoing_db,
        call=call,
        transcript_text=payload.transcript,
        transcript_payload=payload.model_dump(),
    )
    log_outgoing_event(
        outgoing_db,
        tenant_id=call.tenant_id,
        tenant_slug=call.tenant_slug,
        event_type="agent_transcript",
        payload=payload.model_dump(),
        call=call,
        room_name=payload.room_name,
    )
    return {"ok": True, "call_id": call.id}


@router.post(TELNYX_OUTGOING_WEBHOOK_PATH)
async def telnyx_outgoing_webhook(
    request: Request,
    db: Session = Depends(get_db),
    outgoing_db: Session = Depends(get_outgoing_db),
):
    payload = await request.json()
    if not isinstance(payload, dict) or not is_voice_event(payload):
        return JSONResponse({"ok": False, "error": "Unsupported payload"}, status_code=400)

    event_payload = flatten_voice_event(payload)
    client_state = decode_client_state(str(event_payload.get("client_state") or ""))
    tenant = _resolve_outgoing_tenant(db, event_payload, client_state)
    if tenant is None:
        return JSONResponse({"ok": False, "error": "Unable to resolve tenant"}, status_code=400)

    call = get_outgoing_call(
        outgoing_db,
        outgoing_call_id=str(client_state.get("outgoing_call_id") or ""),
        telnyx_call_control_id=str(event_payload.get("CallSid") or ""),
        tenant_id=tenant.id,
    )
    if call is not None:
        apply_telnyx_event_to_call(outgoing_db, call, str(event_payload.get("event_type") or ""), event_payload)

    log_outgoing_event(
        outgoing_db,
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
        event_type=str(event_payload.get("event_type") or "telnyx_event"),
        payload=event_payload,
        call=call,
        room_name=str(event_payload.get("room_name") or ""),
    )

    logger.info(
        "[TELNYX_OUTGOING] event=%s call_control_id=%s tenant=%s to=%s state=%s",
        event_payload.get("event_type"),
        event_payload.get("CallSid"),
        tenant.slug,
        event_payload.get("To"),
        event_payload.get("state"),
    )

    if call is None:
        return JSONResponse({"ok": True, "handled": "logged"})

    event_type = str(event_payload.get("event_type") or "")
    if event_type == "call.answered" and _is_primary_pstn_leg(call, event_payload):
        if call.status not in {"livekit_transfer_requested", "bridged", "completed", "failed"}:
            try:
                _requires_livekit_outgoing_target()
                runtime = build_outgoing_runtime(db, outgoing_db, tenant=tenant, call_control_id=call.telnyx_call_control_id)
                config_version = str(runtime["config"].get("version") or call.tenant_config_version or 1)
                transfer_state = encode_client_state(
                    {
                        "provider": "telnyx",
                        "mode": "outgoing",
                        "tenant_id": tenant.id,
                        "tenant_slug": tenant.slug,
                        "config_version": config_version,
                        "outgoing_call_id": call.id,
                        "called_number": call.target_number,
                        "caller_number": call.from_number,
                    }
                )
                mark_outgoing_call_status(outgoing_db, call, "livekit_transfer_requested")
                await transfer_call(
                    call.telnyx_call_control_id,
                    {
                        "to": LIVEKIT_OUTGOING_SIP_URI,
                        "timeout_secs": 20,
                        "sip_auth_username": LIVEKIT_OUTGOING_SIP_USERNAME,
                        "sip_auth_password": LIVEKIT_OUTGOING_SIP_PASSWORD,
                        "sip_transport_protocol": _telnyx_transport_protocol(LIVEKIT_OUTGOING_SIP_URI),
                        "command_id": telnyx_command_id("outgoing-transfer", call.telnyx_call_control_id),
                        "client_state": transfer_state,
                        "target_leg_client_state": transfer_state,
                        "webhook_url": _telnyx_outgoing_webhook_url(),
                        "webhook_url_method": "POST",
                        "custom_headers": [
                            {"name": "X-Tenant-Id", "value": tenant.id},
                            {"name": "X-Tenant-Slug", "value": tenant.slug},
                            {"name": "X-Config-Version", "value": config_version},
                            {"name": "X-Called-Number", "value": call.target_number},
                            {"name": "X-Caller-Number", "value": call.from_number},
                            {"name": "X-Parent-Call-Sid", "value": call.telnyx_call_control_id},
                            {"name": "X-Outgoing-Call-Id", "value": call.id},
                            {"name": "X-Call-Direction", "value": "outgoing"},
                            {"name": "X-Call-Provider", "value": "telnyx"},
                        ],
                    },
                )
            except Exception as exc:
                logger.exception("[TELNYX_OUTGOING] transfer failed call=%s", call.id)
                mark_outgoing_call_error(outgoing_db, call, str(exc))
                try:
                    if call.telnyx_call_control_id:
                        await hangup_call(
                            call.telnyx_call_control_id,
                            {
                                "command_id": telnyx_command_id("outgoing-hangup-on-error", call.telnyx_call_control_id),
                                "client_state": encode_client_state(
                                    {
                                        "provider": "telnyx",
                                        "mode": "outgoing",
                                        "tenant_id": tenant.id,
                                        "tenant_slug": tenant.slug,
                                        "outgoing_call_id": call.id,
                                    }
                                ),
                            },
                        )
                except Exception:
                    logger.exception("[TELNYX_OUTGOING] hangup after transfer failure also failed call=%s", call.id)
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    if event_type == "call.bridged":
        mark_outgoing_call_status(outgoing_db, call, "bridged")
    elif event_type == "call.hangup" and call.status not in {"completed", "failed"}:
        mark_outgoing_call_status(outgoing_db, call, "completed", ended_at=call.ended_at)

    return JSONResponse({"ok": True, "handled": event_type or "logged", "call_id": call.id})
