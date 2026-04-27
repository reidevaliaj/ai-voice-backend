from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from outgoing_models import OutgoingCall, OutgoingCallEvent, OutgoingCallerNumber, OutgoingTenantProfile
from services.telnyx_voice import decode_client_state
from services.tenants import (
    build_runtime_context,
    get_active_config,
    normalize_assistant_language,
    normalize_endpointing_window,
    normalize_phone_number,
    normalize_stt_language,
    normalize_tts_speed,
)

SUPPORTED_OUTGOING_PROVIDERS = {"telnyx", "twilio"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso_datetime(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalize_outgoing_provider(value: Any) -> str:
    candidate = str(value or "telnyx").strip().lower()
    return candidate if candidate in SUPPORTED_OUTGOING_PROVIDERS else "telnyx"


def default_outgoing_opening_phrase(display_name: str, assistant_language: str = "en") -> str:
    business = (display_name or "our office").strip() or "our office"
    language = normalize_assistant_language(assistant_language)
    if language == "it":
        return f"Buongiorno, la chiamo da {business}. Vorrei parlare con lei per un momento."
    if language == "de":
        return f"Guten Tag, hier ist {business}. Ich wollte kurz mit Ihnen sprechen."
    return f"Hello, this is {business} calling. I wanted to speak with you for a moment."


def default_outgoing_prompt(display_name: str, assistant_language: str = "en") -> str:
    business = (display_name or "the business").strip() or "the business"
    language = normalize_assistant_language(assistant_language)
    language_label = {"en": "English", "it": "Italian", "de": "German"}.get(language, "English")
    return (
        f"You are making an outbound call on behalf of {business}. "
        f"Speak only in {language_label} unless the callee clearly switches language. "
        "Open with the configured opening phrase, explain why you are calling in a natural way, "
        "use only the outbound call notes and outbound prompt as your source of truth, "
        "and end politely when the callee is done."
    )


def get_outgoing_profile(session: Session, tenant_id: str) -> OutgoingTenantProfile | None:
    return session.scalar(select(OutgoingTenantProfile).where(OutgoingTenantProfile.tenant_id == tenant_id))


def ensure_outgoing_profile(session: Session, tenant: Any, active_config: Any | None = None) -> OutgoingTenantProfile:
    assistant_language = normalize_assistant_language(getattr(active_config, "assistant_language", "") if active_config else "")
    stt_language = normalize_stt_language(getattr(active_config, "stt_language", "") if active_config else "", assistant_language)
    llm_model = str(getattr(active_config, "llm_model", "") or "").strip()
    tts_voice = str(getattr(active_config, "tts_voice", "") or "").strip()
    tts_speed = normalize_tts_speed(getattr(active_config, "tts_speed", None))
    min_endpointing_delay, max_endpointing_delay = normalize_endpointing_window(
        getattr(active_config, "min_endpointing_delay", 0.3) if active_config else 0.3,
        getattr(active_config, "max_endpointing_delay", 1.2) if active_config else 1.2,
    )
    profile = get_outgoing_profile(session, tenant.id)
    if profile is None:
        profile = OutgoingTenantProfile(
            tenant_id=tenant.id,
            tenant_slug=tenant.slug,
            display_name=tenant.display_name,
            status="inactive",
            provider="telnyx",
            telnyx_connection_id="",
            assistant_language=assistant_language,
            stt_language=stt_language,
            llm_model=llm_model,
            tts_voice=tts_voice,
            tts_speed=tts_speed,
            min_endpointing_delay=min_endpointing_delay,
            max_endpointing_delay=max_endpointing_delay,
            opening_phrase=default_outgoing_opening_phrase(tenant.display_name, assistant_language),
            system_prompt=default_outgoing_prompt(tenant.display_name, assistant_language),
            caller_display_name=tenant.display_name,
            notes="",
        )
        session.add(profile)
        session.flush()
    else:
        profile.tenant_slug = tenant.slug
        profile.display_name = tenant.display_name
        profile.provider = _normalize_outgoing_provider(getattr(profile, "provider", "telnyx"))
        if not profile.assistant_language:
            profile.assistant_language = assistant_language
        if not profile.stt_language:
            profile.stt_language = stt_language
        if not profile.llm_model:
            profile.llm_model = llm_model
        if not profile.tts_voice:
            profile.tts_voice = tts_voice
        if not profile.tts_speed:
            profile.tts_speed = tts_speed
        if not profile.min_endpointing_delay:
            profile.min_endpointing_delay = min_endpointing_delay
        if not profile.max_endpointing_delay:
            profile.max_endpointing_delay = max_endpointing_delay
        old_default_opening = default_outgoing_opening_phrase(tenant.display_name, "en")
        if profile.opening_phrase == old_default_opening and normalize_assistant_language(profile.assistant_language) != "en":
            profile.opening_phrase = default_outgoing_opening_phrase(tenant.display_name, profile.assistant_language)
        old_default_prompt = default_outgoing_prompt(tenant.display_name, "en")
        if profile.system_prompt == old_default_prompt and normalize_assistant_language(profile.assistant_language) != "en":
            profile.system_prompt = default_outgoing_prompt(tenant.display_name, profile.assistant_language)
        if not profile.opening_phrase:
            profile.opening_phrase = default_outgoing_opening_phrase(tenant.display_name, profile.assistant_language or assistant_language)
        if not profile.system_prompt:
            profile.system_prompt = default_outgoing_prompt(tenant.display_name, profile.assistant_language or assistant_language)
        if not profile.caller_display_name:
            profile.caller_display_name = tenant.display_name

    if active_config and not profile.caller_display_name:
        profile.caller_display_name = str(getattr(active_config, "business_name", "") or tenant.display_name)
    session.flush()
    return profile


def list_outgoing_numbers(session: Session, tenant_id: str) -> list[OutgoingCallerNumber]:
    stmt = (
        select(OutgoingCallerNumber)
        .where(OutgoingCallerNumber.tenant_id == tenant_id)
        .order_by(OutgoingCallerNumber.provider.asc(), OutgoingCallerNumber.is_default.desc(), OutgoingCallerNumber.created_at.asc())
    )
    return list(session.scalars(stmt))


def get_default_outgoing_number(session: Session, tenant_id: str, provider: str = "") -> OutgoingCallerNumber | None:
    stmt = (
        select(OutgoingCallerNumber)
        .where(
            OutgoingCallerNumber.tenant_id == tenant_id,
            OutgoingCallerNumber.status == "active",
        )
        .order_by(OutgoingCallerNumber.is_default.desc(), OutgoingCallerNumber.created_at.asc())
    )
    normalized_provider = _normalize_outgoing_provider(provider) if provider else ""
    if normalized_provider:
        stmt = stmt.where(OutgoingCallerNumber.provider == normalized_provider)
    return session.scalars(stmt).first()


def upsert_outgoing_number(
    session: Session,
    tenant: Any,
    *,
    provider: str = "telnyx",
    phone_number: str,
    label: str = "primary",
    is_default: bool = False,
) -> OutgoingCallerNumber:
    normalized = normalize_phone_number(phone_number)
    normalized_provider = _normalize_outgoing_provider(provider)
    if not normalized:
        raise ValueError("Phone number is required")

    profile = ensure_outgoing_profile(session, tenant)
    stmt = select(OutgoingCallerNumber).where(
        OutgoingCallerNumber.tenant_id == tenant.id,
        OutgoingCallerNumber.phone_number == normalized,
    )
    record = session.scalar(stmt)
    if record is None:
        record = OutgoingCallerNumber(
            profile_id=profile.id,
            tenant_id=tenant.id,
            tenant_slug=tenant.slug,
            provider=normalized_provider,
            phone_number=normalized,
            label=label or "primary",
            status="active",
            is_default=is_default,
        )
        session.add(record)
    else:
        record.profile_id = profile.id
        record.tenant_slug = tenant.slug
        record.provider = normalized_provider
        record.label = label or record.label or "primary"
        record.status = "active"
        record.is_default = is_default or record.is_default

    if record.is_default:
        others = list_outgoing_numbers(session, tenant.id)
        for other in others:
            if other.id != record.id and _normalize_outgoing_provider(other.provider) == normalized_provider:
                other.is_default = False
    elif get_default_outgoing_number(session, tenant.id, normalized_provider) is None:
        record.is_default = True

    session.flush()
    return record


def save_outgoing_profile(
    session: Session,
    tenant: Any,
    payload: dict[str, Any],
    *,
    active_config: Any | None = None,
) -> OutgoingTenantProfile:
    profile = ensure_outgoing_profile(session, tenant, active_config=active_config)
    provider = _normalize_outgoing_provider(payload.get("provider") or profile.provider or "telnyx")
    assistant_language = normalize_assistant_language(str(payload.get("assistant_language") or profile.assistant_language or "en"))
    min_endpointing_delay, max_endpointing_delay = normalize_endpointing_window(
        payload.get("min_endpointing_delay") if payload.get("min_endpointing_delay") not in (None, "") else profile.min_endpointing_delay,
        payload.get("max_endpointing_delay") if payload.get("max_endpointing_delay") not in (None, "") else profile.max_endpointing_delay,
    )
    profile.assistant_language = assistant_language
    profile.stt_language = normalize_stt_language(str(payload.get("stt_language") or profile.stt_language or ""), assistant_language)
    profile.llm_model = str(payload.get("llm_model") or profile.llm_model or "").strip()
    profile.tts_voice = str(payload.get("tts_voice") or profile.tts_voice or "").strip()
    profile.tts_speed = normalize_tts_speed(payload.get("tts_speed") if payload.get("tts_speed") not in (None, "") else profile.tts_speed)
    profile.min_endpointing_delay = min_endpointing_delay
    profile.max_endpointing_delay = max_endpointing_delay
    profile.status = str(payload.get("status") or profile.status or "inactive").strip().lower() or "inactive"
    profile.provider = provider
    profile.telnyx_connection_id = str(payload.get("telnyx_connection_id") or "").strip()
    profile.opening_phrase = str(payload.get("opening_phrase") or "").strip() or default_outgoing_opening_phrase(tenant.display_name, assistant_language)
    profile.system_prompt = str(payload.get("system_prompt") or "").strip() or default_outgoing_prompt(tenant.display_name, assistant_language)
    profile.caller_display_name = str(payload.get("caller_display_name") or tenant.display_name).strip() or tenant.display_name
    profile.notes = str(payload.get("notes") or "").strip()
    session.flush()
    return profile


def outgoing_profile_form_payload(profile: OutgoingTenantProfile | None, tenant: Any) -> dict[str, Any]:
    if profile is None:
        assistant_language = "en"
        return {
            "status": "inactive",
            "provider": "telnyx",
            "telnyx_connection_id": "",
            "assistant_language": assistant_language,
            "stt_language": normalize_stt_language("", assistant_language),
            "llm_model": "",
            "tts_voice": "",
            "tts_speed": 1.0,
            "min_endpointing_delay": 0.3,
            "max_endpointing_delay": 1.2,
            "opening_phrase": default_outgoing_opening_phrase(tenant.display_name, assistant_language),
            "system_prompt": default_outgoing_prompt(tenant.display_name, assistant_language),
            "caller_display_name": tenant.display_name,
            "notes": "",
        }
    return {
        "status": profile.status,
        "provider": _normalize_outgoing_provider(profile.provider),
        "telnyx_connection_id": profile.telnyx_connection_id,
        "assistant_language": profile.assistant_language,
        "stt_language": profile.stt_language,
        "llm_model": profile.llm_model,
        "tts_voice": profile.tts_voice,
        "tts_speed": profile.tts_speed,
        "min_endpointing_delay": profile.min_endpointing_delay,
        "max_endpointing_delay": profile.max_endpointing_delay,
        "opening_phrase": profile.opening_phrase,
        "system_prompt": profile.system_prompt,
        "caller_display_name": profile.caller_display_name,
        "notes": profile.notes,
    }


def create_outgoing_call(
    session: Session,
    *,
    tenant: Any,
    profile: OutgoingTenantProfile,
    target_number: str,
    from_number: str,
    target_name: str = "",
    notes: str = "",
    tenant_config_version: int = 1,
) -> OutgoingCall:
    provider = _normalize_outgoing_provider(profile.provider)
    call = OutgoingCall(
        profile_id=profile.id,
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
        tenant_display_name=tenant.display_name,
        provider=provider,
        target_number=normalize_phone_number(target_number),
        target_name=str(target_name or "").strip(),
        from_number=normalize_phone_number(from_number),
        opening_phrase=profile.opening_phrase,
        notes=str(notes or "").strip(),
        status="queued",
        tenant_config_version=int(tenant_config_version or 1),
        telnyx_connection_id=profile.telnyx_connection_id if provider == "telnyx" else "",
        extra_json={},
    )
    session.add(call)
    session.flush()
    return call


def get_outgoing_call(
    session: Session,
    *,
    outgoing_call_id: str = "",
    provider_call_sid: str = "",
    telnyx_call_control_id: str = "",
    twilio_call_sid: str = "",
    tenant_id: str = "",
) -> OutgoingCall | None:
    if outgoing_call_id:
        record = session.get(OutgoingCall, outgoing_call_id)
        if record is not None:
            return record
    if provider_call_sid:
        stmt = select(OutgoingCall).where(OutgoingCall.provider_call_sid == provider_call_sid)
        record = session.scalar(stmt)
        if record is not None:
            return record
    if telnyx_call_control_id:
        stmt = select(OutgoingCall).where(OutgoingCall.telnyx_call_control_id == telnyx_call_control_id)
        record = session.scalar(stmt)
        if record is not None:
            return record
    if twilio_call_sid:
        stmt = select(OutgoingCall).where(OutgoingCall.twilio_call_sid == twilio_call_sid)
        record = session.scalar(stmt)
        if record is not None:
            return record
    if tenant_id:
        stmt = select(OutgoingCall).where(OutgoingCall.tenant_id == tenant_id).order_by(OutgoingCall.created_at.desc())
        return session.scalars(stmt).first()
    return None


def list_recent_outgoing_calls(session: Session, tenant_id: str, limit: int = 30) -> list[OutgoingCall]:
    stmt = (
        select(OutgoingCall)
        .where(OutgoingCall.tenant_id == tenant_id)
        .order_by(OutgoingCall.created_at.desc())
        .limit(limit)
    )
    return list(session.scalars(stmt))


def list_recent_outgoing_events(session: Session, tenant_id: str, limit: int = 40) -> list[OutgoingCallEvent]:
    stmt = (
        select(OutgoingCallEvent)
        .where(OutgoingCallEvent.tenant_id == tenant_id)
        .order_by(OutgoingCallEvent.created_at.desc())
        .limit(limit)
    )
    return list(session.scalars(stmt))


def clear_outgoing_events(session: Session, tenant_id: str) -> int:
    result = session.execute(
        delete(OutgoingCallEvent).where(OutgoingCallEvent.tenant_id == tenant_id)
    )
    session.flush()
    return int(result.rowcount or 0)


def log_outgoing_event(
    session: Session,
    *,
    tenant_id: str,
    tenant_slug: str,
    event_type: str,
    payload: dict[str, Any],
    call: OutgoingCall | None = None,
    room_name: str = "",
) -> OutgoingCallEvent:
    provider = _normalize_outgoing_provider((call.provider if call else "") or payload.get("provider") or "telnyx")
    provider_call_sid = str(
        payload.get("provider_call_sid")
        or payload.get("call_control_id")
        or payload.get("CallSid")
        or (call.provider_call_sid if call else "")
        or ""
    )
    event = OutgoingCallEvent(
        outgoing_call_id=call.id if call else None,
        tenant_id=tenant_id,
        tenant_slug=tenant_slug,
        provider=provider,
        event_type=event_type,
        provider_call_sid=provider_call_sid,
        telnyx_call_control_id=str(payload.get("call_control_id") or payload.get("CallSid") or ""),
        room_name=room_name or str(payload.get("room_name") or ""),
        payload_json=payload,
    )
    session.add(event)
    session.flush()
    return event


def apply_telnyx_event_to_call(session: Session, call: OutgoingCall, event_type: str, payload: dict[str, Any]) -> OutgoingCall:
    call.provider = "telnyx"
    call.telnyx_event_type = event_type
    call.telnyx_call_control_id = str(payload.get("call_control_id") or call.telnyx_call_control_id or "")
    call.provider_call_sid = call.telnyx_call_control_id or call.provider_call_sid or ""
    call.telnyx_call_leg_id = str(payload.get("call_leg_id") or call.telnyx_call_leg_id or "")
    call.telnyx_call_session_id = str(payload.get("call_session_id") or call.telnyx_call_session_id or "")
    call.updated_at = _utcnow()

    if event_type == "call.initiated":
        if not call.started_at:
            call.started_at = _utcnow()
        if call.status == "queued":
            call.status = "initiated"
    elif event_type == "call.answered":
        if not call.answered_at:
            call.answered_at = _utcnow()
        if normalize_phone_number(str(payload.get("to") or "")) == call.target_number:
            call.status = "answered"
    elif event_type == "call.bridged":
        if not call.bridged_at:
            call.bridged_at = _utcnow()
        call.status = "bridged"
    elif event_type == "call.hangup":
        call.ended_at = _utcnow()
        call.telnyx_hangup_cause = str(payload.get("hangup_cause") or "")
        if call.status not in {"completed", "failed"}:
            call.status = "completed" if call.answered_at else "failed"
    session.flush()
    return call


def apply_twilio_event_to_call(
    session: Session,
    call: OutgoingCall,
    event_type: str,
    payload: dict[str, Any],
    *,
    is_sip_leg: bool = False,
) -> OutgoingCall:
    call.provider = "twilio"
    call.twilio_event_type = event_type
    primary_call_sid = str(
        payload.get("provider_call_sid")
        or payload.get("ParentCallSid")
        or (payload.get("CallSid") if not is_sip_leg else "")
        or call.twilio_call_sid
        or ""
    )
    if primary_call_sid:
        call.twilio_call_sid = primary_call_sid
        call.provider_call_sid = primary_call_sid
    call.updated_at = _utcnow()

    status = str(payload.get("CallStatus") or "").strip().lower()
    if is_sip_leg:
        if event_type in {"answered", "in-progress"} or status == "in-progress":
            if not call.bridged_at:
                call.bridged_at = _utcnow()
            if call.status not in {"completed", "failed"}:
                call.status = "bridged"
        elif event_type == "completed":
            if not call.bridged_at and status == "completed":
                call.bridged_at = _utcnow()
        session.flush()
        return call

    if event_type in {"initiated", "queued"}:
        if not call.started_at:
            call.started_at = _utcnow()
        if call.status == "queued":
            call.status = "initiated"
    elif event_type == "ringing":
        if not call.started_at:
            call.started_at = _utcnow()
        if call.status == "queued":
            call.status = "dialing"
    elif event_type in {"answered", "in-progress"} or status == "in-progress":
        if not call.answered_at:
            call.answered_at = _utcnow()
        if call.status not in {"bridged", "completed", "failed"}:
            call.status = "answered"
    elif event_type == "completed":
        if not call.ended_at:
            call.ended_at = _utcnow()
        call.twilio_hangup_cause = status
        if status in {"busy", "failed", "no-answer", "canceled"}:
            call.status = "failed"
            if not call.last_error:
                call.last_error = f"Twilio call ended with status '{status}'."
        elif call.status not in {"completed", "failed"}:
            call.status = "completed" if call.answered_at or call.bridged_at else "failed"
    session.flush()
    return call


def mark_outgoing_call_error(session: Session, call: OutgoingCall, message: str) -> OutgoingCall:
    call.last_error = str(message or "").strip()
    call.status = "failed"
    call.ended_at = _utcnow()
    session.flush()
    return call


def mark_outgoing_call_status(session: Session, call: OutgoingCall, status: str, **fields: Any) -> OutgoingCall:
    call.status = status
    for key, value in fields.items():
        if hasattr(call, key):
            setattr(call, key, value)
    call.updated_at = _utcnow()
    session.flush()
    return call


def update_outgoing_call_extra(session: Session, call: OutgoingCall, updates: dict[str, Any]) -> OutgoingCall:
    debug_payload = dict(call.extra_json or {})
    for key, value in updates.items():
        if value is None:
            continue
        debug_payload[key] = value
    call.extra_json = debug_payload
    call.updated_at = _utcnow()
    session.flush()
    return call


def sync_outgoing_call_from_provider(session: Session, call: OutgoingCall, provider_payload: dict[str, Any]) -> OutgoingCall:
    if _normalize_outgoing_provider(call.provider) != "telnyx":
        return call

    data = provider_payload.get("data") if isinstance(provider_payload, dict) else {}
    if not isinstance(data, dict):
        return call

    provider_state = dict(call.extra_json or {})
    client_state = decode_client_state(str(data.get("client_state") or ""))
    provider_state.update(
        {
            "provider_sync_at": _utcnow().isoformat(),
            "provider_is_alive": bool(data.get("is_alive")),
            "provider_start_time": str(data.get("start_time") or ""),
            "provider_end_time": str(data.get("end_time") or ""),
            "provider_call_duration": data.get("call_duration"),
            "provider_client_state": client_state,
        }
    )
    call.extra_json = provider_state

    if data.get("call_leg_id") and not call.telnyx_call_leg_id:
        call.telnyx_call_leg_id = str(data.get("call_leg_id") or "")
    if data.get("call_session_id") and not call.telnyx_call_session_id:
        call.telnyx_call_session_id = str(data.get("call_session_id") or "")
    if data.get("call_control_id") and not call.provider_call_sid:
        call.provider_call_sid = str(data.get("call_control_id") or "")

    start_time = _parse_iso_datetime(str(data.get("start_time") or ""))
    end_time = _parse_iso_datetime(str(data.get("end_time") or ""))
    if start_time and call.started_at is None:
        call.started_at = start_time
    if end_time:
        call.ended_at = end_time

    if bool(data.get("is_alive")):
        call.updated_at = _utcnow()
        session.flush()
        return call

    reason = str(client_state.get("reason") or "").strip().lower()
    if reason == "machine":
        call.status = "machine_detected"
        if not call.last_error:
            call.last_error = "Telnyx classified the destination answer as a machine/auto-answer and the call was ended before the AI handoff."
    elif call.status not in {"completed", "failed", "machine_detected"}:
        call.status = "completed" if call.answered_at else "failed"

    call.updated_at = _utcnow()
    session.flush()
    return call


def save_outgoing_transcript(
    session: Session,
    *,
    call: OutgoingCall,
    transcript_text: str,
    transcript_payload: dict[str, Any],
) -> OutgoingCall:
    call.transcript_text = transcript_text
    call.transcript_json = dict(transcript_payload or {})
    room_name = str((transcript_payload or {}).get("room_name") or "").strip()
    if room_name and not call.livekit_room_name:
        call.livekit_room_name = room_name
    if call.status not in {"completed", "failed"}:
        call.status = "completed"
    if not call.ended_at:
        call.ended_at = _utcnow()
    session.flush()
    return call


def build_outgoing_runtime(
    primary_session: Session,
    outgoing_session: Session,
    *,
    tenant: Any,
    call_sid: str = "",
    call_control_id: str = "",
    outgoing_call_id: str = "",
    room_name: str = "",
) -> dict[str, Any]:
    active_config = get_active_config(primary_session, tenant.id)
    call = get_outgoing_call(
        outgoing_session,
        outgoing_call_id=outgoing_call_id,
        provider_call_sid=call_sid,
        telnyx_call_control_id=call_control_id,
        twilio_call_sid=call_sid,
        tenant_id=tenant.id,
    )
    config_version = call.tenant_config_version if call is not None else None
    runtime = build_runtime_context(primary_session, tenant, config_version=config_version)
    profile = ensure_outgoing_profile(outgoing_session, tenant, active_config=active_config)
    effective_config = dict(runtime["config"])
    profile_min_endpointing_delay, profile_max_endpointing_delay = normalize_endpointing_window(
        profile.min_endpointing_delay,
        profile.max_endpointing_delay,
    )
    effective_config.update(
        {
            "assistant_language": profile.assistant_language or effective_config.get("assistant_language"),
            "stt_language": profile.stt_language or effective_config.get("stt_language"),
            "llm_model": profile.llm_model or effective_config.get("llm_model"),
            "tts_voice": profile.tts_voice or effective_config.get("tts_voice"),
            "tts_speed": profile.tts_speed if profile.tts_speed else effective_config.get("tts_speed"),
            "min_endpointing_delay": profile_min_endpointing_delay,
            "max_endpointing_delay": profile_max_endpointing_delay,
        }
    )

    return {
        "tenant": runtime["tenant"],
        "config": effective_config,
        "outgoing": {
            "profile_id": profile.id,
            "status": profile.status,
            "provider": _normalize_outgoing_provider(profile.provider),
            "telnyx_connection_id": profile.telnyx_connection_id,
            "assistant_language": profile.assistant_language,
            "stt_language": profile.stt_language,
            "llm_model": profile.llm_model,
            "tts_voice": profile.tts_voice,
            "tts_speed": profile.tts_speed,
            "min_endpointing_delay": profile_min_endpointing_delay,
            "max_endpointing_delay": profile_max_endpointing_delay,
            "opening_phrase": (call.opening_phrase if call else profile.opening_phrase) or default_outgoing_opening_phrase(tenant.display_name, profile.assistant_language or effective_config.get("assistant_language") or "en"),
            "system_prompt": profile.system_prompt or default_outgoing_prompt(tenant.display_name, profile.assistant_language or effective_config.get("assistant_language") or "en"),
            "caller_display_name": profile.caller_display_name or tenant.display_name,
            "notes": profile.notes,
        },
        "call": {
            "id": call.id if call else "",
            "status": call.status if call else "unknown",
            "provider": _normalize_outgoing_provider(call.provider if call else profile.provider),
            "provider_call_id": call.provider_call_sid if call else call_sid or call_control_id,
            "target_number": call.target_number if call else "",
            "target_name": call.target_name if call else "",
            "from_number": call.from_number if call else "",
            "notes": call.notes if call else "",
            "livekit_room_name": call.livekit_room_name if call else room_name,
            "telnyx_call_control_id": call.telnyx_call_control_id if call else call_control_id,
            "twilio_call_sid": call.twilio_call_sid if call else call_sid,
        },
    }
