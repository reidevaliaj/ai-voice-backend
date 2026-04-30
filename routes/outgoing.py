from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from twilio.twiml.voice_response import VoiceResponse

from app_config import (
    FASTAPI_INTERNAL_API_KEY,
    LIVEKIT_OUTGOING_SIP_PASSWORD,
    LIVEKIT_OUTGOING_SIP_URI,
    LIVEKIT_OUTGOING_SIP_USERNAME,
    PUBLIC_BASE_URL,
    TELNYX_API_KEY,
    TELNYX_OUTGOING_HANDOFF_MODE,
    TELNYX_OUTGOING_AMD_MODE,
    TELNYX_OUTGOING_RECORDING_CHANNELS,
    TELNYX_OUTGOING_RECORDING_FORMAT,
    TELNYX_OUTGOING_RECORDING_MAX_LENGTH,
    TELNYX_OUTGOING_WEBHOOK_PATH,
    TWILIO_OUTGOING_SIP_STATUS_PATH,
    TWILIO_OUTGOING_STATUS_PATH,
    TWILIO_OUTGOING_TWIML_PATH,
)
from db import get_db
from outgoing_db import get_outgoing_db
from services.livekit_voice import cleanup_outgoing_room
from services.outgoing import (
    apply_telnyx_event_to_call,
    apply_twilio_event_to_call,
    build_outgoing_runtime,
    get_outgoing_call,
    log_outgoing_event,
    mark_outgoing_call_error,
    mark_outgoing_call_status,
    save_outgoing_transcript,
    update_outgoing_call_extra,
)
from services.telnyx_voice import (
    decode_client_state,
    encode_client_state,
    flatten_voice_event,
    hangup_call,
    is_voice_event,
    start_recording,
    telnyx_command_id,
    transfer_call,
)
from services.tenants import get_tenant_by_id, get_tenant_by_slug, normalize_phone_number
from services.twilio_voice import hangup_call as twilio_hangup_call
from tools.email_resend import send_email_resend
from tools.storage import append_event
from tools.transcript_ai import analyze_outgoing_transcript

logger = logging.getLogger("outgoing")

router = APIRouter()
TWILIO_OUTGOING_TWIML_ROUTE = TWILIO_OUTGOING_TWIML_PATH if TWILIO_OUTGOING_TWIML_PATH.startswith("/") else f"/{TWILIO_OUTGOING_TWIML_PATH}"
TWILIO_OUTGOING_STATUS_ROUTE = TWILIO_OUTGOING_STATUS_PATH if TWILIO_OUTGOING_STATUS_PATH.startswith("/") else f"/{TWILIO_OUTGOING_STATUS_PATH}"
TWILIO_OUTGOING_SIP_STATUS_ROUTE = (
    TWILIO_OUTGOING_SIP_STATUS_PATH if TWILIO_OUTGOING_SIP_STATUS_PATH.startswith("/") else f"/{TWILIO_OUTGOING_SIP_STATUS_PATH}"
)


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


class OutgoingEndCallRequest(BaseModel):
    tenant_id: str = ""
    tenant_slug: str = ""
    outgoing_call_id: str = ""
    call_sid: str = ""
    reason: str = ""
    notes: str = ""
    timestamp: int = 0


class OutgoingLiveKitStatusRequest(BaseModel):
    tenant_id: str = ""
    tenant_slug: str = ""
    outgoing_call_id: str = ""
    room_name: str = ""
    status: str = ""
    provider_call_sid: str = ""
    participant_identity: str = ""
    sip_status_code: str = ""
    sip_status: str = ""
    error: str = ""
    timestamp: int = 0


def _require_internal_api_key(x_internal_api_key: str | None = Header(default=None)) -> None:
    if FASTAPI_INTERNAL_API_KEY and x_internal_api_key != FASTAPI_INTERNAL_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid internal API key")


def _public_url(path: str) -> str:
    return PUBLIC_BASE_URL.rstrip("/") + path


def _sip_uri_with_headers(base_uri: str, headers: dict[str, str]) -> str:
    prefix = "&" if "?" in base_uri else "?"
    parts = [f"{quote_plus(key)}={quote_plus(value)}" for key, value in headers.items() if value]
    if not parts:
        return base_uri
    return base_uri + prefix + "&".join(parts)


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
    normalized_to = normalize_phone_number(str(event_payload.get("To") or event_payload.get("to") or ""))
    if normalized_to and normalized_to == normalize_phone_number(call.target_number):
        return True
    return str(event_payload.get("CallSid") or event_payload.get("call_control_id") or "") == str(call.telnyx_call_control_id or "")


def _current_debug_state(call: Any) -> dict[str, Any]:
    return dict(getattr(call, "extra_json", {}) or {})


def _normalized_amd_mode() -> str:
    mode = str(TELNYX_OUTGOING_AMD_MODE or "").strip().lower()
    allowed = {"detect", "detect_beep", "detect_words", "greeting_end", "premium"}
    return mode if mode in allowed else "premium"


def _normalized_handoff_mode() -> str:
    mode = str(TELNYX_OUTGOING_HANDOFF_MODE or "").strip().lower()
    return mode if mode in {"direct", "amd"} else "direct"


def _format_timestamp(ts: int | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _to_html(text: str) -> str:
    return (text or "").replace("\n", "<br/>")


def _parse_email_targets(raw: str) -> list[str]:
    values = []
    for chunk in str(raw or "").replace(";", "\n").replace(",", "\n").splitlines():
        item = chunk.strip()
        if item:
            values.append(item)
    return values


def _email_targets_from_runtime(runtime: dict[str, Any]) -> list[str]:
    outgoing = runtime.get("outgoing") or {}
    configured = _parse_email_targets(str(outgoing.get("summary_notification_targets") or ""))
    return configured or ["info@cos-st.com"]


def _send_outgoing_email_summary(
    *,
    payload: OutgoingTranscriptPayload,
    analysis: dict[str, Any],
    runtime: dict[str, Any],
    call: Any,
) -> dict[str, Any]:
    config = runtime["config"]
    outgoing = runtime.get("outgoing") or {}
    targets = _email_targets_from_runtime(runtime)
    subject = (
        f"[AI Voice] {config['business_name']} outgoing call summary - "
        f"{analysis.get('interest_status') or 'unclear'}"
    )
    html = f"""
    <h3>Outgoing Call Summary</h3>
    <p><b>Tenant:</b> {runtime['tenant']['slug']}</p>
    <p><b>Business:</b> {config['business_name']}</p>
    <p><b>Interested:</b> {analysis.get('interest_status', 'unclear')}</p>
    <p><b>Consultation / callback time:</b> {analysis.get('consultation_time_window', '') or analysis.get('callback_time_window', '')}</p>
    <p><b>Next step:</b> {_to_html(str(analysis.get('next_step', '')))}</p>
    <p><b>Objections:</b> {_to_html(str(analysis.get('objections', '')))}</p>
    <p><b>Summary:</b> {_to_html(str(analysis.get('summary', '')))}</p>
    <p><b>Contact name:</b> {analysis.get('contact_name', '') or call.target_name or ''}</p>
    <p><b>Contact email:</b> {analysis.get('contact_email', '')}</p>
    <p><b>Contact phone:</b> {analysis.get('contact_phone', '') or call.target_number}</p>
    <p><b>To:</b> {call.target_number}</p>
    <p><b>From:</b> {call.from_number}</p>
    <p><b>Room:</b> {payload.room_name or ''}</p>
    <p><b>Call timestamp (UTC):</b> {_format_timestamp(payload.timestamp)}</p>
    <hr/>
    <p><b>Full transcript:</b></p>
    <p>{_to_html(payload.transcript)}</p>
    """
    sent_to: list[str] = []
    results: list[dict[str, Any]] = []
    from_email = str(outgoing.get("summary_from_email") or "info@cos-st.com").strip() or "info@cos-st.com"
    reply_to = str(outgoing.get("summary_reply_to_email") or "").strip()
    for target in targets:
        result = send_email_resend(
            to=target,
            subject=subject,
            html=html,
            from_email=from_email,
            reply_to=reply_to,
            tags=[{"name": "tool", "value": "outgoing-email-summary"}],
        )
        sent_to.append(target)
        results.append({"to": target, "result": result})
    email_event = {
        "tenant_id": payload.tenant_id or runtime["tenant"]["id"],
        "tenant_slug": runtime["tenant"]["slug"],
        "outgoing_call_id": call.id,
        "room_name": payload.room_name,
        "timestamp": payload.timestamp,
        "subject": subject,
        "from_email": from_email,
        "reply_to": reply_to,
        "targets": sent_to,
        "interest_status": analysis.get("interest_status", "unclear"),
        "consultation_time_window": analysis.get("consultation_time_window", ""),
        "callback_time_window": analysis.get("callback_time_window", ""),
        "results": results,
    }
    append_event("outgoing_email_summary_events.jsonl", email_event)
    return email_event


def _is_human_detection_result(result: str) -> bool:
    return result in {"human", "human_business", "human_residence", "not_sure"}


def _is_machine_detection_result(result: str) -> bool:
    return result in {"machine", "fax_detected", "silence"}


async def _request_form_dict(request: Request) -> dict[str, Any]:
    if request.method != "POST":
        return {}
    try:
        form = await request.form()
    except Exception:
        return {}
    return dict(form)


def _twilio_event_name(call_status: str) -> str:
    normalized = str(call_status or "").strip().lower()
    if normalized == "in-progress":
        return "answered"
    if normalized == "queued":
        return "initiated"
    return normalized or "status_callback"


async def _ensure_primary_leg_recording(call: Any, tenant: Any, outgoing_db: Session) -> None:
    debug_state = _current_debug_state(call)
    if debug_state.get("recording_start_requested"):
        return

    recording_state = encode_client_state(
        {
            "provider": "telnyx",
            "mode": "outgoing",
            "phase": "pre_bridge_recording",
            "tenant_id": tenant.id,
            "tenant_slug": tenant.slug,
            "outgoing_call_id": call.id,
            "target_number": call.target_number,
            "from_number": call.from_number,
        }
    )
    update_outgoing_call_extra(
        outgoing_db,
        call,
        {
            "recording_start_requested": True,
            "recording_start_requested_at": call.updated_at.isoformat() if call.updated_at else "",
            "recording_format": TELNYX_OUTGOING_RECORDING_FORMAT,
            "recording_channels": TELNYX_OUTGOING_RECORDING_CHANNELS,
            "recording_max_length": TELNYX_OUTGOING_RECORDING_MAX_LENGTH,
        },
    )
    await start_recording(
        call.telnyx_call_control_id,
        {
            "format": TELNYX_OUTGOING_RECORDING_FORMAT,
            "channels": TELNYX_OUTGOING_RECORDING_CHANNELS,
            "max_length": TELNYX_OUTGOING_RECORDING_MAX_LENGTH,
            "play_beep": False,
            "client_state": recording_state,
            "command_id": telnyx_command_id("outgoing-record-start", call.telnyx_call_control_id or call.id),
        },
    )


async def _request_livekit_transfer(
    *,
    db: Session,
    outgoing_db: Session,
    tenant: Any,
    call: Any,
    runtime: dict[str, Any] | None = None,
    amd_result: str = "",
) -> None:
    debug_state = _current_debug_state(call)
    if call.status in {"livekit_transfer_requested", "bridged", "completed", "failed"} or debug_state.get("transfer_requested"):
        return

    _requires_livekit_outgoing_target()
    resolved_runtime = runtime or build_outgoing_runtime(db, outgoing_db, tenant=tenant, call_control_id=call.telnyx_call_control_id)
    config_version = str(resolved_runtime["config"].get("version") or call.tenant_config_version or 1)
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
    update_outgoing_call_extra(
        outgoing_db,
        call,
        {
            "amd_result": amd_result or debug_state.get("amd_result", ""),
            "transfer_requested": True,
            "transfer_requested_at": call.updated_at.isoformat() if call.updated_at else "",
        },
    )
    mark_outgoing_call_status(outgoing_db, call, "livekit_transfer_requested")
    await transfer_call(
        call.telnyx_call_control_id,
        {
            "to": LIVEKIT_OUTGOING_SIP_URI,
            "timeout_secs": 20,
            "early_media": False,
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


@router.post("/outgoing/calls/end")
async def outgoing_end_call(
    payload: OutgoingEndCallRequest,
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
        provider_call_sid=payload.call_sid,
        telnyx_call_control_id=payload.call_sid,
        twilio_call_sid=payload.call_sid,
        tenant_id=tenant.id if tenant else "",
    )
    if call is None:
        raise HTTPException(status_code=404, detail="Outgoing call not found")

    update_outgoing_call_extra(
        outgoing_db,
        call,
        {
            "assistant_end_requested": True,
            "assistant_end_reason": str(payload.reason or "assistant_goodbye"),
            "assistant_end_notes": str(payload.notes or ""),
            "assistant_end_requested_at": _format_timestamp(payload.timestamp) or datetime.now(timezone.utc).isoformat(),
        },
    )
    try:
        provider = str(call.provider or "telnyx").strip().lower() or "telnyx"
        if provider == "telnyx" and (call.extra_json or {}).get("livekit_first"):
            await cleanup_outgoing_room(
                room_name=call.livekit_room_name,
                participant_identity=str((call.extra_json or {}).get("livekit_participant_identity") or ""),
                dispatch_id=str((call.extra_json or {}).get("livekit_dispatch_id") or ""),
            )
        elif provider == "telnyx" and call.telnyx_call_control_id:
            await hangup_call(
                call.telnyx_call_control_id,
                {
                    "command_id": telnyx_command_id("outgoing-end", call.telnyx_call_control_id or call.id),
                    "client_state": encode_client_state(
                        {
                            "provider": "telnyx",
                            "mode": "outgoing",
                            "tenant_id": call.tenant_id,
                            "tenant_slug": call.tenant_slug,
                            "outgoing_call_id": call.id,
                            "reason": "assistant_end",
                        }
                    ),
                },
            )
        elif provider == "twilio" and call.twilio_call_sid:
            await twilio_hangup_call(call.twilio_call_sid)
    except Exception as exc:
        logger.warning("[TELNYX_OUTGOING] hangup request after assistant goodbye failed call=%s error=%s", call.id, exc)
        update_outgoing_call_extra(
            outgoing_db,
            call,
            {"assistant_end_hangup_error": str(exc)},
        )

    mark_outgoing_call_status(outgoing_db, call, "completed", ended_at=call.ended_at or datetime.now(timezone.utc))
    log_outgoing_event(
        outgoing_db,
        tenant_id=call.tenant_id,
        tenant_slug=call.tenant_slug,
        event_type="assistant_end_request",
        payload=payload.model_dump(),
        call=call,
        room_name=call.livekit_room_name,
    )
    return {"ok": True, "call_id": call.id}


@router.post("/outgoing/calls/livekit-status")
async def outgoing_livekit_status(
    payload: OutgoingLiveKitStatusRequest,
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
        provider_call_sid=payload.provider_call_sid,
        tenant_id=tenant.id if tenant else "",
    )
    if call is None:
        raise HTTPException(status_code=404, detail="Outgoing call not found")

    now = datetime.now(timezone.utc)
    if payload.room_name:
        call.livekit_room_name = payload.room_name
    if payload.provider_call_sid:
        call.provider_call_sid = payload.provider_call_sid

    extra_updates = {
        "livekit_first": True,
        "livekit_participant_identity": payload.participant_identity,
        "livekit_status": payload.status,
        "livekit_status_updated_at": _format_timestamp(payload.timestamp) or now.isoformat(),
        "livekit_sip_status_code": payload.sip_status_code,
        "livekit_sip_status": payload.sip_status,
        "livekit_error": payload.error,
    }

    status = str(payload.status or "").strip().lower()
    if status == "bridged":
        mark_outgoing_call_status(
            outgoing_db,
            call,
            "bridged",
            started_at=call.started_at or now,
            answered_at=call.answered_at or now,
            bridged_at=call.bridged_at or now,
            livekit_room_name=payload.room_name or call.livekit_room_name,
            provider_call_sid=payload.provider_call_sid or call.provider_call_sid,
        )
    elif status == "failed":
        error_message = str(payload.error or "").strip()
        sip_code = str(payload.sip_status_code or "").strip()
        sip_status = str(payload.sip_status or "").strip()
        detail = " | ".join(part for part in [error_message, f"SIP {sip_code}" if sip_code else "", sip_status] if part)
        mark_outgoing_call_error(outgoing_db, call, detail or "LiveKit outbound call failed before answer")
    elif status:
        mark_outgoing_call_status(outgoing_db, call, status, livekit_room_name=payload.room_name or call.livekit_room_name)

    update_outgoing_call_extra(outgoing_db, call, extra_updates)
    log_outgoing_event(
        outgoing_db,
        tenant_id=call.tenant_id,
        tenant_slug=call.tenant_slug,
        event_type="livekit_status",
        payload=payload.model_dump(),
        call=call,
        room_name=payload.room_name or call.livekit_room_name,
    )
    return {"ok": True, "call_id": call.id}


async def _hangup_machine_answer(tenant: Any, call: Any, outgoing_db: Session, reason: str) -> None:
    debug_state = _current_debug_state(call)
    if debug_state.get("machine_hangup_requested"):
        return

    update_outgoing_call_extra(
        outgoing_db,
        call,
        {
            "machine_hangup_requested": True,
            "machine_hangup_requested_at": call.updated_at.isoformat() if call.updated_at else "",
            "machine_hangup_reason": reason,
        },
    )
    mark_outgoing_call_status(outgoing_db, call, "machine_detected")
    await hangup_call(
        call.telnyx_call_control_id,
        {
            "command_id": telnyx_command_id("outgoing-machine-hangup", call.telnyx_call_control_id or call.id),
            "client_state": encode_client_state(
                {
                    "provider": "telnyx",
                    "mode": "outgoing",
                    "tenant_id": tenant.id,
                    "tenant_slug": tenant.slug,
                    "outgoing_call_id": call.id,
                    "reason": reason,
                }
            ),
        },
    )


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
    if call is not None and payload.room_name:
        now = datetime.now(timezone.utc)
        if not call.livekit_room_name:
            call.livekit_room_name = payload.room_name
        if call.status in {"queued", "dialing", "initiated", "livekit_dispatch_requested"}:
            mark_outgoing_call_status(
                outgoing_db,
                call,
                "bridged",
                started_at=call.started_at or now,
                answered_at=call.answered_at or now,
                bridged_at=call.bridged_at or now,
                livekit_room_name=payload.room_name,
            )

    runtime = build_outgoing_runtime(
        db,
        outgoing_db,
        tenant=tenant,
        call_sid=payload.call_sid,
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
        provider_call_sid=payload.call_sid,
        telnyx_call_control_id=payload.call_sid,
        twilio_call_sid=payload.call_sid,
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
    if payload.room_name and not call.livekit_room_name:
        call.livekit_room_name = payload.room_name
    if call.status not in {"failed", "machine_detected", "completed"}:
        mark_outgoing_call_status(
            outgoing_db,
            call,
            "completed",
            ended_at=call.ended_at or datetime.now(timezone.utc),
            livekit_room_name=payload.room_name or call.livekit_room_name,
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
    if tenant is None:
        tenant = get_tenant_by_id(db, call.tenant_id)
    try:
        if tenant is None:
            raise LookupError("Tenant not found for outgoing transcript")

        runtime = build_outgoing_runtime(
            db,
            outgoing_db,
            tenant=tenant,
            call_sid=call.provider_call_sid,
            call_control_id=call.telnyx_call_control_id,
            outgoing_call_id=call.id,
            room_name=payload.room_name,
        )
        analysis = analyze_outgoing_transcript(
            payload.transcript,
            payload.messages,
            current_time_utc_iso=_format_timestamp(payload.timestamp),
            business_timezone=runtime["config"]["timezone"],
            business_context={
                "business_name": runtime["config"]["business_name"],
                "assistant_language": runtime["outgoing"].get("assistant_language") or runtime["config"]["assistant_language"],
            },
            outgoing_context={
                "opening_phrase": runtime["outgoing"].get("opening_phrase", ""),
                "system_prompt": runtime["outgoing"].get("system_prompt", ""),
                "notes": runtime["outgoing"].get("notes", ""),
                "target_name": runtime["call"].get("target_name", ""),
                "target_number": runtime["call"].get("target_number", ""),
            },
        )
        update_outgoing_call_extra(
            outgoing_db,
            call,
            {
                "outgoing_transcript_analysis": analysis,
                "interest_status": str(analysis.get("interest_status") or "unclear"),
                "interested": bool(analysis.get("interested", False)),
                "callback_requested": bool(analysis.get("callback_requested", False)),
                "callback_time_window": str(analysis.get("callback_time_window") or ""),
                "consultation_time_window": str(analysis.get("consultation_time_window") or ""),
                "next_step": str(analysis.get("next_step") or ""),
                "objections": str(analysis.get("objections") or ""),
            },
        )
        analysis_event = {
            "tenant_id": call.tenant_id,
            "tenant_slug": call.tenant_slug,
            "outgoing_call_id": call.id,
            "room_name": payload.room_name,
            "analysis": analysis,
        }
        append_event("outgoing_transcript_analysis_events.jsonl", analysis_event)
        log_outgoing_event(
            outgoing_db,
            tenant_id=call.tenant_id,
            tenant_slug=call.tenant_slug,
            event_type="outgoing_transcript_analysis",
            payload=analysis_event,
            call=call,
            room_name=payload.room_name,
        )
        if runtime["config"]["enabled_tools"].get("email_summary", True):
            email_event = _send_outgoing_email_summary(
                payload=payload,
                analysis=analysis,
                runtime=runtime,
                call=call,
            )
            log_outgoing_event(
                outgoing_db,
                tenant_id=call.tenant_id,
                tenant_slug=call.tenant_slug,
                event_type="outgoing_email_summary_sent",
                payload=email_event,
                call=call,
                room_name=payload.room_name,
            )
    except Exception as exc:
        logger.exception("Outgoing transcript post-processing failed call_id=%s", call.id)
        try:
            update_outgoing_call_extra(
                outgoing_db,
                call,
                {"outgoing_transcript_postprocess_error": str(exc)},
            )
            log_outgoing_event(
                outgoing_db,
                tenant_id=call.tenant_id,
                tenant_slug=call.tenant_slug,
                event_type="outgoing_transcript_postprocess_error",
                payload={"error": str(exc), "room_name": payload.room_name, "outgoing_call_id": call.id},
                call=call,
                room_name=payload.room_name,
            )
        except Exception:
            logger.exception("Failed to persist outgoing transcript post-process error call_id=%s", call.id)
    return {"ok": True, "call_id": call.id}


@router.api_route(TWILIO_OUTGOING_TWIML_ROUTE, methods=["GET", "POST"])
async def twilio_outgoing_twiml(
    request: Request,
    db: Session = Depends(get_db),
    outgoing_db: Session = Depends(get_outgoing_db),
):
    payload = await _request_form_dict(request)
    outgoing_call_id = str(request.query_params.get("outgoing_call_id") or payload.get("outgoing_call_id") or "").strip()
    call_sid = str(payload.get("CallSid") or "").strip()
    call = get_outgoing_call(
        outgoing_db,
        outgoing_call_id=outgoing_call_id,
        provider_call_sid=call_sid,
        twilio_call_sid=call_sid,
    )
    if call is None:
        vr = VoiceResponse()
        vr.say("We could not complete this outgoing call.")
        vr.hangup()
        return Response(content=str(vr), media_type="application/xml")

    tenant = get_tenant_by_id(db, call.tenant_id)
    if tenant is None:
        vr = VoiceResponse()
        vr.say("We could not complete this outgoing call.")
        vr.hangup()
        return Response(content=str(vr), media_type="application/xml")

    called_number = normalize_phone_number(str(payload.get("To") or call.target_number or ""))
    caller_number = normalize_phone_number(str(payload.get("From") or call.from_number or ""))
    config_version = str(call.tenant_config_version or 1)
    call_payload = {
        **payload,
        "provider": "twilio",
        "provider_call_sid": call_sid,
        "outgoing_call_id": call.id,
        "called_number": called_number,
        "caller_number": caller_number,
    }
    apply_twilio_event_to_call(outgoing_db, call, "answered", call_payload)
    update_outgoing_call_extra(
        outgoing_db,
        call,
        {
            "twilio_twiml_requested_at": datetime.now(timezone.utc).isoformat(),
            "twilio_called_number": called_number,
            "twilio_caller_number": caller_number,
        },
    )
    log_outgoing_event(
        outgoing_db,
        tenant_id=call.tenant_id,
        tenant_slug=call.tenant_slug,
        event_type="twilio_twiml_requested",
        payload=call_payload,
        call=call,
    )

    sip_uri = _sip_uri_with_headers(
        LIVEKIT_OUTGOING_SIP_URI,
        {
            "x-tenant-id": tenant.id,
            "x-tenant-slug": tenant.slug,
            "x-config-version": config_version,
            "x-called-number": call.target_number or called_number,
            "x-caller-number": call.from_number or caller_number,
            "x-parent-call-sid": call_sid,
            "x-outgoing-call-id": call.id,
            "x-call-direction": "outgoing",
            "x-call-provider": "twilio",
        },
    )
    sip_status_callback = _public_url(TWILIO_OUTGOING_SIP_STATUS_ROUTE) + f"?outgoing_call_id={call.id}"

    vr = VoiceResponse()
    dial = vr.dial(timeout=20)
    dial.sip(
        sip_uri,
        username=LIVEKIT_OUTGOING_SIP_USERNAME,
        password=LIVEKIT_OUTGOING_SIP_PASSWORD,
        status_callback=sip_status_callback,
        status_callback_method="POST",
        status_callback_event="initiated ringing answered completed",
    )
    return Response(content=str(vr), media_type="application/xml")


@router.api_route(TWILIO_OUTGOING_STATUS_ROUTE, methods=["GET", "POST"])
async def twilio_outgoing_status(
    request: Request,
    db: Session = Depends(get_db),
    outgoing_db: Session = Depends(get_outgoing_db),
):
    payload = {**dict(request.query_params), **(await _request_form_dict(request))}
    outgoing_call_id = str(payload.get("outgoing_call_id") or "").strip()
    call_sid = str(payload.get("CallSid") or "").strip()
    call = get_outgoing_call(
        outgoing_db,
        outgoing_call_id=outgoing_call_id,
        provider_call_sid=call_sid,
        twilio_call_sid=call_sid,
    )
    if call is None:
        return Response("OK")

    tenant = get_tenant_by_id(db, call.tenant_id)
    event_type = _twilio_event_name(str(payload.get("CallStatus") or ""))
    event_payload = {
        **payload,
        "provider": "twilio",
        "provider_call_sid": call_sid,
        "outgoing_call_id": call.id,
    }
    apply_twilio_event_to_call(outgoing_db, call, event_type, event_payload)
    update_outgoing_call_extra(
        outgoing_db,
        call,
        {
            "last_twilio_status": str(payload.get("CallStatus") or ""),
            "last_twilio_status_at": datetime.now(timezone.utc).isoformat(),
            "twilio_direction": str(payload.get("Direction") or ""),
            "twilio_call_duration": str(payload.get("CallDuration") or ""),
            "twilio_sip_response_code": str(payload.get("SipResponseCode") or ""),
        },
    )
    if tenant is not None:
        log_outgoing_event(
            outgoing_db,
            tenant_id=call.tenant_id,
            tenant_slug=call.tenant_slug,
            event_type=f"twilio_{event_type}",
            payload=event_payload,
            call=call,
            room_name=call.livekit_room_name,
        )
    return Response("OK")


@router.api_route(TWILIO_OUTGOING_SIP_STATUS_ROUTE, methods=["GET", "POST"])
async def twilio_outgoing_sip_status(
    request: Request,
    db: Session = Depends(get_db),
    outgoing_db: Session = Depends(get_outgoing_db),
):
    payload = {**dict(request.query_params), **(await _request_form_dict(request))}
    outgoing_call_id = str(payload.get("outgoing_call_id") or "").strip()
    parent_call_sid = str(payload.get("ParentCallSid") or "").strip()
    child_call_sid = str(payload.get("CallSid") or "").strip()
    call = get_outgoing_call(
        outgoing_db,
        outgoing_call_id=outgoing_call_id,
        provider_call_sid=parent_call_sid or child_call_sid,
        twilio_call_sid=parent_call_sid or child_call_sid,
    )
    if call is None:
        return Response("OK")

    tenant = get_tenant_by_id(db, call.tenant_id)
    event_type = _twilio_event_name(str(payload.get("CallStatus") or ""))
    event_payload = {
        **payload,
        "provider": "twilio",
        "provider_call_sid": parent_call_sid or call.provider_call_sid,
        "twilio_child_call_sid": child_call_sid,
        "outgoing_call_id": call.id,
    }
    apply_twilio_event_to_call(outgoing_db, call, event_type, event_payload, is_sip_leg=True)
    update_outgoing_call_extra(
        outgoing_db,
        call,
        {
            "twilio_sip_leg_status": str(payload.get("CallStatus") or ""),
            "twilio_sip_leg_status_at": datetime.now(timezone.utc).isoformat(),
            "twilio_sip_child_call_sid": child_call_sid,
            "twilio_sip_parent_call_sid": parent_call_sid,
        },
    )
    if tenant is not None:
        log_outgoing_event(
            outgoing_db,
            tenant_id=call.tenant_id,
            tenant_slug=call.tenant_slug,
            event_type=f"twilio_sip_{event_type}",
            payload=event_payload,
            call=call,
            room_name=call.livekit_room_name,
        )
    return Response("OK")


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
    update_outgoing_call_extra(
        outgoing_db,
        call,
        {
            "last_event_type": event_type,
            "last_event_state": str(event_payload.get("state") or ""),
            "last_event_at": str(event_payload.get("occurred_at") or event_payload.get("Timestamp") or ""),
            "last_event_id": str(event_payload.get("event_id") or ""),
            "last_event_result": str(event_payload.get("result") or ""),
            "handoff_mode": _normalized_handoff_mode(),
            "amd_mode": _normalized_amd_mode(),
        },
    )

    if event_type == "call.answered" and _is_primary_pstn_leg(call, event_payload):
        if call.status not in {"livekit_transfer_requested", "bridged", "completed", "failed"}:
            update_outgoing_call_extra(
                outgoing_db,
                call,
                {
                    "target_answered_at": str(event_payload.get("occurred_at") or event_payload.get("Timestamp") or ""),
                    "target_answer_state": str(event_payload.get("state") or ""),
                },
            )
            if _normalized_handoff_mode() == "direct":
                try:
                    mark_outgoing_call_status(outgoing_db, call, "human_detected")
                    await _request_livekit_transfer(
                        db=db,
                        outgoing_db=outgoing_db,
                        tenant=tenant,
                        call=call,
                        amd_result="direct_answer",
                    )
                except Exception as exc:
                    logger.exception("[TELNYX_OUTGOING] direct transfer failed call=%s", call.id)
                    mark_outgoing_call_error(outgoing_db, call, str(exc))
                    update_outgoing_call_extra(
                        outgoing_db,
                        call,
                        {
                            "transfer_error": str(exc),
                            "transfer_error_at": str(event_payload.get("occurred_at") or event_payload.get("Timestamp") or ""),
                        },
                    )
                return JSONResponse({"ok": True, "handled": event_type or "logged", "call_id": call.id})

            mark_outgoing_call_status(outgoing_db, call, "awaiting_machine_detection")
            try:
                await _ensure_primary_leg_recording(call, tenant, outgoing_db)
            except Exception as exc:
                logger.exception("[TELNYX_OUTGOING] record_start failed call=%s", call.id)
                call.last_error = f"Recording start failed: {exc}"
                update_outgoing_call_extra(
                    outgoing_db,
                    call,
                    {
                        "recording_start_failed_at": str(event_payload.get("occurred_at") or event_payload.get("Timestamp") or ""),
                        "recording_start_error": str(exc),
                    },
                )
                outgoing_db.flush()

    elif (
        _normalized_handoff_mode() == "amd"
        and event_type in {"call.machine.detection.ended", "call.machine.premium.detection.ended"}
        and _is_primary_pstn_leg(call, event_payload)
    ):
        amd_result = str(event_payload.get("result") or "").strip().lower()
        if not _current_debug_state(call).get("recording_start_requested"):
            try:
                await _ensure_primary_leg_recording(call, tenant, outgoing_db)
            except Exception as exc:
                logger.exception("[TELNYX_OUTGOING] late record_start failed call=%s", call.id)
                update_outgoing_call_extra(
                    outgoing_db,
                    call,
                    {
                        "recording_start_error": str(exc),
                        "recording_start_failed_at": str(event_payload.get("occurred_at") or event_payload.get("Timestamp") or ""),
                    },
                )
        update_outgoing_call_extra(
            outgoing_db,
            call,
            {
                "amd_result": amd_result,
                "amd_event_type": event_type,
                "amd_detected_at": str(event_payload.get("occurred_at") or event_payload.get("Timestamp") or ""),
            },
        )
        if _is_human_detection_result(amd_result):
            try:
                mark_outgoing_call_status(outgoing_db, call, "human_detected")
                await _request_livekit_transfer(
                    db=db,
                    outgoing_db=outgoing_db,
                    tenant=tenant,
                    call=call,
                    amd_result=amd_result,
                )
            except Exception as exc:
                logger.exception("[TELNYX_OUTGOING] transfer after AMD failed call=%s", call.id)
                mark_outgoing_call_error(outgoing_db, call, str(exc))
                update_outgoing_call_extra(
                    outgoing_db,
                    call,
                    {
                        "transfer_error": str(exc),
                        "transfer_error_at": str(event_payload.get("occurred_at") or event_payload.get("Timestamp") or ""),
                    },
                )
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
        elif _is_machine_detection_result(amd_result):
            try:
                await _hangup_machine_answer(tenant, call, outgoing_db, amd_result)
            except Exception as exc:
                logger.exception("[TELNYX_OUTGOING] machine hangup failed call=%s", call.id)
                mark_outgoing_call_error(outgoing_db, call, str(exc))
                update_outgoing_call_extra(
                    outgoing_db,
                    call,
                    {
                        "machine_hangup_error": str(exc),
                        "machine_hangup_error_at": str(event_payload.get("occurred_at") or event_payload.get("Timestamp") or ""),
                    },
                )
        else:
            update_outgoing_call_extra(
                outgoing_db,
                call,
                {"amd_unknown_result": amd_result or "missing"},
            )

    elif (
        _normalized_handoff_mode() == "amd"
        and event_type in {"call.machine.greeting.ended", "call.machine.premium.greeting.ended"}
        and _is_primary_pstn_leg(call, event_payload)
    ):
        update_outgoing_call_extra(
            outgoing_db,
            call,
            {
                "machine_greeting_result": str(event_payload.get("result") or ""),
                "machine_greeting_ended_at": str(event_payload.get("occurred_at") or event_payload.get("Timestamp") or ""),
            },
        )

    elif _normalized_handoff_mode() == "amd" and event_type == "call.recording.saved" and _is_primary_pstn_leg(call, event_payload):
        update_outgoing_call_extra(
            outgoing_db,
            call,
            {
                "recording_saved_at": str(event_payload.get("occurred_at") or event_payload.get("Timestamp") or ""),
                "recording_id": str(event_payload.get("recording_id") or ""),
                "recording_urls": event_payload.get("recording_urls") if isinstance(event_payload.get("recording_urls"), dict) else {},
                "public_recording_urls": event_payload.get("public_recording_urls") if isinstance(event_payload.get("public_recording_urls"), dict) else {},
                "recording_started_at": str(event_payload.get("recording_started_at") or ""),
                "recording_ended_at": str(event_payload.get("recording_ended_at") or ""),
            },
        )

    elif _normalized_handoff_mode() == "amd" and event_type == "call.recording.error" and _is_primary_pstn_leg(call, event_payload):
        error_text = str(event_payload.get("error") or event_payload.get("detail") or "Recording error").strip()
        call.last_error = error_text
        update_outgoing_call_extra(
            outgoing_db,
            call,
            {
                "recording_error": error_text,
                "recording_error_at": str(event_payload.get("occurred_at") or event_payload.get("Timestamp") or ""),
            },
        )
        outgoing_db.flush()

    if event_type == "call.bridged":
        mark_outgoing_call_status(outgoing_db, call, "bridged")
        update_outgoing_call_extra(
            outgoing_db,
            call,
            {"bridged_at_event": str(event_payload.get("occurred_at") or event_payload.get("Timestamp") or "")},
        )
    elif event_type == "call.hangup" and call.status not in {"completed", "failed"}:
        mark_outgoing_call_status(outgoing_db, call, "completed", ended_at=call.ended_at)
        update_outgoing_call_extra(
            outgoing_db,
            call,
            {
                "hangup_source": str(event_payload.get("hangup_source") or ""),
                "sip_hangup_cause": str(event_payload.get("sip_hangup_cause") or ""),
                "hangup_cause": str(event_payload.get("hangup_cause") or ""),
            },
        )

    return JSONResponse({"ok": True, "handled": event_type or "logged", "call_id": call.id})
