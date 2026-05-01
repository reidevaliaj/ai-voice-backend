from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app_config import (
    LIVEKIT_API_KEY,
    LIVEKIT_API_SECRET,
    LIVEKIT_OUTGOING_AGENT_NAME,
    LIVEKIT_OUTGOING_SIP_PASSWORD,
    LIVEKIT_OUTGOING_SIP_URI,
    LIVEKIT_OUTGOING_SIP_USERNAME,
    LIVEKIT_TELNYX_OUTBOUND_HOST,
    LIVEKIT_TELNYX_OUTBOUND_PASSWORD,
    LIVEKIT_TELNYX_OUTBOUND_TRUNK_ID,
    LIVEKIT_TELNYX_OUTBOUND_USERNAME,
    LIVEKIT_URL,
    PUBLIC_BASE_URL,
    TELNYX_API_KEY,
    TELNYX_OUTGOING_AMD_MODE,
    TELNYX_OUTGOING_HANDOFF_MODE,
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
)
from services.livekit_voice import create_agent_dispatch, ensure_telnyx_outbound_trunk
from services.outgoing import (
    build_outgoing_prompt_tags,
    create_outgoing_call,
    get_default_outgoing_number,
    list_outgoing_numbers,
    parse_outgoing_prompt_tags,
    render_outgoing_template,
)
from services.telnyx_voice import (
    dial_call as telnyx_dial_call,
    encode_client_state,
    ensure_outbound_recording_for_connection,
    telnyx_command_id,
)
from services.tenants import normalize_phone_number
from services.twilio_voice import dial_call as twilio_dial_call


class OutgoingLaunchError(RuntimeError):
    def __init__(self, message: str, *, call: Any | None = None):
        super().__init__(message)
        self.call = call


@dataclass(slots=True)
class OutgoingLaunchRequest:
    tenant: Any
    profile: Any
    active_config: Any | None
    target_number: str
    target_name: str = ""
    notes: str = ""
    from_number: str = ""
    tag_website: str = ""
    tag_reason: str = ""
    tag_specific: str = ""
    extra_tags_raw: str = ""
    extra_json: dict[str, Any] | None = None


def _outgoing_room_name(call_id: str) -> str:
    return f"outgoing-call-{call_id}"


def _provider_numbers(session: Session, tenant_id: str, provider: str) -> set[str]:
    return {
        item.phone_number
        for item in list_outgoing_numbers(session, tenant_id)
        if str(item.provider or "telnyx").strip().lower() == provider and item.status == "active"
    }


def validate_outgoing_launch_request(session: Session, request: OutgoingLaunchRequest) -> tuple[str, str, str]:
    tenant = request.tenant
    profile = request.profile
    provider = str(profile.provider or "telnyx").strip().lower() or "telnyx"
    handoff_mode = (TELNYX_OUTGOING_HANDOFF_MODE or "direct").strip().lower()
    target_number = normalize_phone_number(request.target_number)
    selected_from_number = normalize_phone_number(request.from_number)
    default_number = get_default_outgoing_number(session, tenant.id, provider)
    from_number = selected_from_number or (default_number.phone_number if default_number else "")
    provider_numbers = _provider_numbers(session, tenant.id, provider)

    if provider == "telnyx" and handoff_mode == "livekit_first":
        if not (LIVEKIT_URL and LIVEKIT_API_KEY and LIVEKIT_API_SECRET):
            raise OutgoingLaunchError("LiveKit management API credentials are missing on the backend server")
        if not (
            LIVEKIT_TELNYX_OUTBOUND_TRUNK_ID
            or (LIVEKIT_TELNYX_OUTBOUND_HOST and LIVEKIT_TELNYX_OUTBOUND_USERNAME and LIVEKIT_TELNYX_OUTBOUND_PASSWORD)
        ):
            raise OutgoingLaunchError("LiveKit Telnyx outbound trunk settings are missing on the backend server")
    elif not (LIVEKIT_OUTGOING_SIP_URI and LIVEKIT_OUTGOING_SIP_USERNAME and LIVEKIT_OUTGOING_SIP_PASSWORD):
        raise OutgoingLaunchError("The outgoing LiveKit SIP target is not configured on the backend server yet")

    if not target_number:
        raise OutgoingLaunchError("A destination phone number is required")
    if not from_number:
        raise OutgoingLaunchError("Save at least one outgoing caller ID for this tenant first")
    if provider_numbers and from_number not in provider_numbers:
        raise OutgoingLaunchError(f"Choose a caller ID saved for the {provider} provider")
    if profile.status != "active":
        raise OutgoingLaunchError("Set the tenant's outgoing status to active before launching calls")
    if provider == "telnyx" and not TELNYX_API_KEY and handoff_mode != "livekit_first":
        raise OutgoingLaunchError("TELNYX_API_KEY is missing on the backend server")
    if provider == "telnyx" and not profile.telnyx_connection_id and handoff_mode != "livekit_first":
        raise OutgoingLaunchError("Save the tenant's Telnyx Voice API application ID first")
    if provider == "twilio" and not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN):
        raise OutgoingLaunchError("TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN is missing on the backend server")
    return provider, handoff_mode, from_number


async def launch_outgoing_call(session: Session, request: OutgoingLaunchRequest) -> Any:
    tenant = request.tenant
    profile = request.profile
    provider, handoff_mode, from_number = validate_outgoing_launch_request(session, request)
    target_number = normalize_phone_number(request.target_number)
    target_name = str(request.target_name or "").strip()
    notes = str(request.notes or "").strip()
    extra_tags_raw = str(request.extra_tags_raw or "").strip()
    extra_json = dict(request.extra_json or {})

    call = create_outgoing_call(
        session,
        tenant=tenant,
        profile=profile,
        target_number=target_number,
        from_number=from_number,
        target_name=target_name,
        notes=notes,
        tenant_config_version=request.active_config.version if request.active_config else 1,
    )
    prompt_tags = build_outgoing_prompt_tags(
        tenant_display_name=tenant.display_name,
        caller_display_name=profile.caller_display_name or tenant.display_name,
        target_name=target_name,
        target_number=target_number,
        from_number=from_number,
        notes=notes,
        website=request.tag_website,
        reason=request.tag_reason,
        specific=request.tag_specific,
        extra_tags=parse_outgoing_prompt_tags(extra_tags_raw),
    )
    call.opening_phrase = render_outgoing_template(profile.opening_phrase, prompt_tags) or profile.opening_phrase
    call.extra_json = {
        **(call.extra_json or {}),
        **extra_json,
        "provider": provider,
        "handoff_mode": handoff_mode,
        "amd_mode": TELNYX_OUTGOING_AMD_MODE,
        "launch_notes": notes,
        "prompt_tags": prompt_tags,
        "extra_tags_raw": extra_tags_raw,
        "rendered_system_prompt": render_outgoing_template(profile.system_prompt, prompt_tags),
        "rendered_profile_notes": render_outgoing_template(profile.notes, prompt_tags),
    }
    session.flush()

    try:
        if provider == "telnyx":
            if handoff_mode == "livekit_first":
                trunk = await ensure_telnyx_outbound_trunk()
                room_name = _outgoing_room_name(call.id)
                participant_identity = f"callee-{call.id}"
                recording_state: dict[str, Any] = {}
                try:
                    recording_state = await ensure_outbound_recording_for_connection(
                        sip_username=LIVEKIT_TELNYX_OUTBOUND_USERNAME,
                        caller_number=from_number,
                    )
                except Exception as exc:
                    recording_state = {
                        "recording_enabled": False,
                        "recording_error": str(exc),
                    }
                dispatch_metadata = {
                    "mode": "livekit_first",
                    "provider": "telnyx",
                    "tenant_id": tenant.id,
                    "tenant_slug": tenant.slug,
                    "outgoing_call_id": call.id,
                    "phone_number": target_number,
                    "from_number": from_number,
                    "participant_identity": participant_identity,
                    "participant_name": target_name or target_number,
                    "caller_display_name": profile.caller_display_name or tenant.display_name,
                    "sip_trunk_id": trunk["sip_trunk_id"],
                }
                dispatch = await create_agent_dispatch(
                    room_name=room_name,
                    metadata=dispatch_metadata,
                    agent_name=LIVEKIT_OUTGOING_AGENT_NAME,
                )
                call.livekit_room_name = room_name
                call.status = "dialing"
                call.started_at = call.started_at or datetime.now(timezone.utc)
                call.provider_call_sid = ""
                call.telnyx_call_control_id = ""
                call.telnyx_call_leg_id = ""
                call.telnyx_call_session_id = ""
                call.extra_json = {
                    **(call.extra_json or {}),
                    "livekit_first": True,
                    "livekit_dispatch_id": dispatch.get("dispatch_id", ""),
                    "livekit_participant_identity": participant_identity,
                    "livekit_outbound_trunk_id": trunk["sip_trunk_id"],
                    "livekit_dispatch_metadata": dispatch_metadata,
                    "telnyx_credential_connection_id": recording_state.get("connection_id", ""),
                    "telnyx_outbound_voice_profile_id": recording_state.get("outbound_voice_profile_id", ""),
                    "recording_expected": bool(recording_state.get("recording_enabled")),
                    "recording_provider": "telnyx_outbound_voice_profile",
                    "recording_provider_updated": bool(recording_state.get("updated")),
                    "recording_provider_settings": recording_state.get("call_recording", {}),
                    "recording_enable_error": str(recording_state.get("recording_error") or ""),
                }
                session.flush()
            else:
                client_state = encode_client_state(
                    {
                        "provider": "telnyx",
                        "mode": "outgoing",
                        "tenant_id": tenant.id,
                        "tenant_slug": tenant.slug,
                        "outgoing_call_id": call.id,
                        "target_number": target_number,
                        "from_number": from_number,
                    }
                )
                dial_payload = {
                    "connection_id": profile.telnyx_connection_id,
                    "to": target_number,
                    "from": from_number,
                    "from_display_name": profile.caller_display_name or tenant.display_name,
                    "webhook_url": f"{PUBLIC_BASE_URL.rstrip('/')}/outgoing/telnyx/webhook",
                    "webhook_url_method": "POST",
                    "client_state": client_state,
                    "command_id": telnyx_command_id("outgoing-dial", call.id),
                }
                if handoff_mode == "amd":
                    dial_payload["answering_machine_detection"] = TELNYX_OUTGOING_AMD_MODE
                result = await telnyx_dial_call(dial_payload)
                data = result.get("data") or {}
                call.telnyx_call_control_id = str(data.get("call_control_id") or call.telnyx_call_control_id or "")
                call.provider_call_sid = call.telnyx_call_control_id or call.provider_call_sid or ""
                call.telnyx_call_leg_id = str(data.get("call_leg_id") or call.telnyx_call_leg_id or "")
                call.telnyx_call_session_id = str(data.get("call_session_id") or call.telnyx_call_session_id or "")
                call.status = "dialing"
                session.flush()
        else:
            twiml_url = f"{PUBLIC_BASE_URL.rstrip('/')}/outgoing/twilio/twiml?outgoing_call_id={call.id}"
            status_callback = f"{PUBLIC_BASE_URL.rstrip('/')}/outgoing/twilio/status?outgoing_call_id={call.id}"
            result = await twilio_dial_call(
                to=target_number,
                from_number=from_number,
                url=twiml_url,
                status_callback=status_callback,
            )
            call.twilio_call_sid = str(result.get("sid") or call.twilio_call_sid or "")
            call.provider_call_sid = call.twilio_call_sid or call.provider_call_sid or ""
            call.status = str(result.get("status") or "queued").strip().lower() or "queued"
            session.flush()
    except Exception as exc:
        call.status = "failed"
        call.last_error = str(exc)
        session.flush()
        raise OutgoingLaunchError(str(exc), call=call) from exc

    return call
