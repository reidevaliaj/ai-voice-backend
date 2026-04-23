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
    TELNYX_OUTGOING_AMD_MODE,
    TELNYX_OUTGOING_RECORDING_CHANNELS,
    TELNYX_OUTGOING_RECORDING_FORMAT,
    TELNYX_OUTGOING_RECORDING_MAX_LENGTH,
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


def _is_human_detection_result(result: str) -> bool:
    return result in {"human", "human_business", "human_residence", "not_sure"}


def _is_machine_detection_result(result: str) -> bool:
    return result in {"machine", "fax_detected", "silence"}


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
    update_outgoing_call_extra(
        outgoing_db,
        call,
        {
            "last_event_type": event_type,
            "last_event_state": str(event_payload.get("state") or ""),
            "last_event_at": str(event_payload.get("occurred_at") or event_payload.get("Timestamp") or ""),
            "last_event_id": str(event_payload.get("event_id") or ""),
            "last_event_result": str(event_payload.get("result") or ""),
            "amd_mode": _normalized_amd_mode(),
        },
    )

    if event_type == "call.answered" and _is_primary_pstn_leg(call, event_payload):
        if call.status not in {"livekit_transfer_requested", "bridged", "completed", "failed"}:
            mark_outgoing_call_status(outgoing_db, call, "awaiting_machine_detection")
            update_outgoing_call_extra(
                outgoing_db,
                call,
                {
                    "target_answered_at": str(event_payload.get("occurred_at") or event_payload.get("Timestamp") or ""),
                    "target_answer_state": str(event_payload.get("state") or ""),
                },
            )
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

    elif event_type in {"call.machine.detection.ended", "call.machine.premium.detection.ended"} and _is_primary_pstn_leg(call, event_payload):
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

    elif event_type in {"call.machine.greeting.ended", "call.machine.premium.greeting.ended"} and _is_primary_pstn_leg(call, event_payload):
        update_outgoing_call_extra(
            outgoing_db,
            call,
            {
                "machine_greeting_result": str(event_payload.get("result") or ""),
                "machine_greeting_ended_at": str(event_payload.get("occurred_at") or event_payload.get("Timestamp") or ""),
            },
        )

    elif event_type == "call.recording.saved" and _is_primary_pstn_leg(call, event_payload):
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

    elif event_type == "call.recording.error" and _is_primary_pstn_leg(call, event_payload):
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
