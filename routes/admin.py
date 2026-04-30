import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app_config import (
    AGENT_DEBUG_LOG_PATH,
    AGENT_LOG_PATH,
    DEBUG_LOG_MAX_CHARS,
    LIVEKIT_API_KEY,
    LIVEKIT_API_SECRET,
    LIVEKIT_OUTGOING_SIP_PASSWORD,
    LIVEKIT_OUTGOING_AGENT_NAME,
    LIVEKIT_OUTGOING_SIP_URI,
    LIVEKIT_OUTGOING_SIP_USERNAME,
    LIVEKIT_TELNYX_OUTBOUND_HOST,
    LIVEKIT_TELNYX_OUTBOUND_PASSWORD,
    LIVEKIT_TELNYX_OUTBOUND_TRUNK_ID,
    LIVEKIT_TELNYX_OUTBOUND_USERNAME,
    LIVEKIT_URL,
    OUTGOING_AGENT_DEBUG_LOG_PATH,
    OUTGOING_AGENT_LOG_PATH,
    PUBLIC_BASE_URL,
    TELNYX_API_KEY,
    TELNYX_OUTGOING_HANDOFF_MODE,
    TELNYX_OUTGOING_AMD_MODE,
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
)
from db import get_db
from outgoing_db import get_outgoing_db
from models import AdminUser, CallEvent, Tenant, TenantPhoneNumber
from security import decrypt_json, mask_secret, verify_password
from services.cartesia import get_cartesia_voice_options
from services.livekit_voice import create_agent_dispatch, ensure_telnyx_outbound_trunk
from services.outgoing import (
    clear_outgoing_events,
    create_outgoing_call,
    ensure_outgoing_profile,
    get_default_outgoing_number,
    list_outgoing_numbers,
    list_recent_outgoing_calls,
    outgoing_profile_form_payload,
    save_outgoing_profile,
    sync_outgoing_call_from_provider,
    upsert_outgoing_number,
)
from services.telnyx_voice import (
    dial_call as telnyx_dial_call,
    encode_client_state,
    ensure_outbound_recording_for_connection,
    get_call_details,
    list_call_recordings,
    telnyx_command_id,
)
from services.tenants import (
    build_runtime_context,
    config_form_payload,
    create_config_version,
    create_tenant,
    get_active_config,
    get_integration_payload,
    get_tenant_by_slug,
    integration_form_payload,
    normalize_assistant_language,
    normalize_endpointing_window,
    normalize_phone_number,
    normalize_stt_language,
    normalize_tts_speed,
    parse_lines,
    supported_assistant_languages,
    supported_stt_languages,
    upsert_integration,
    upsert_phone_number,
)
from services.twilio_voice import dial_call as twilio_dial_call
from tools.email_resend import send_email_resend
from tools.google_calendar import CalendarContext, validate_calendar_context
from tools.zoom_meetings import ZoomContext, validate_zoom_context

router = APIRouter()
templates = Jinja2Templates(directory="templates")
logger = logging.getLogger("admin")

OUTGOING_ACTIVE_STATUSES = {
    "queued",
    "dialing",
    "initiated",
    "answered",
    "awaiting_machine_detection",
    "human_detected",
    "livekit_transfer_requested",
}
OUTGOING_PROVIDER_CHOICES = (
    {"value": "telnyx", "label": "Telnyx"},
    {"value": "twilio", "label": "Twilio"},
)


def _outgoing_room_name(call_id: str) -> str:
    return f"outgoing-call-{call_id}"


def _read_log_tail(path_value: str, max_chars: int = DEBUG_LOG_MAX_CHARS) -> dict[str, Any]:
    path = Path(path_value)
    if not path.exists():
        return {"path": str(path), "exists": False, "content": "", "size": 0}
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return {"path": str(path), "exists": True, "content": f"Unable to read log: {exc}", "size": 0}
    content = raw[-max_chars:] if len(raw) > max_chars else raw
    return {"path": str(path), "exists": True, "content": content, "size": len(raw)}


def _parse_debug_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except Exception:
        return raw


def _summarize_debug_fields(fields: dict[str, Any]) -> str:
    preferred_keys = (
        "text",
        "new_state",
        "old_state",
        "room_name",
        "provider",
        "reason",
        "name",
        "status",
        "output",
        "notes",
    )
    parts: list[str] = []
    for key in preferred_keys:
        value = fields.get(key)
        if value in (None, "", [], {}):
            continue
        rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        parts.append(f"{key}={rendered}")
    if not parts:
        remaining_keys = [key for key in fields.keys() if fields.get(key) not in (None, "", [], {})]
        for key in remaining_keys[:4]:
            value = fields.get(key)
            rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            parts.append(f"{key}={rendered}")
    summary = " | ".join(parts)
    return summary if len(summary) <= 220 else summary[:217] + "..."


def _parse_debug_log_line(line: str) -> dict[str, Any] | None:
    raw = line.strip()
    if not raw:
        return None
    parts = raw.split(" | ")
    if len(parts) < 3:
        return None
    try:
        timestamp = datetime.fromisoformat(parts[0])
    except ValueError:
        return None
    fields: dict[str, Any] = {}
    for item in parts[3:]:
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        fields[key] = _parse_debug_value(value)
    return {
        "raw_line": raw,
        "timestamp": parts[0],
        "dt": timestamp,
        "category": parts[1],
        "event": parts[2],
        "fields": fields,
    }


def _latest_debug_session_entries(raw_content: str) -> list[dict[str, Any]]:
    parsed = [
        entry
        for entry in (_parse_debug_log_line(line) for line in raw_content.splitlines())
        if entry is not None
    ]
    if not parsed:
        return []
    session_start_index = 0
    for idx, entry in enumerate(parsed):
        if entry["category"] == "CALL" and entry["event"] == "session_started":
            session_start_index = idx
    return parsed[session_start_index:]


def _parse_iso_datetime(value: Any) -> datetime | None:
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


def _debug_is_user_stop(entry: dict[str, Any]) -> bool:
    return (
        entry["category"] == "TURN"
        and entry["event"] == "user_state_changed"
        and str(entry["fields"].get("new_state") or "") == "listening"
    )


def _debug_is_user_committed(entry: dict[str, Any]) -> bool:
    return entry["category"] == "TRANSCRIPT" and entry["event"] == "USER_COMMITTED"


def _debug_is_agent_speaking(entry: dict[str, Any]) -> bool:
    return (
        entry["category"] == "AGENT"
        and entry["event"] == "agent_state_changed"
        and str(entry["fields"].get("new_state") or "") == "speaking"
    )


def _build_bridge_metrics(timeline: list[dict[str, Any]]) -> dict[str, Any]:
    bridges: list[dict[str, Any]] = []
    last_user_committed_index = -1

    for idx, entry in enumerate(timeline):
        if not _debug_is_user_committed(entry):
            continue

        speech_end = None
        for back_idx in range(idx - 1, last_user_committed_index, -1):
            candidate = timeline[back_idx]
            if _debug_is_user_stop(candidate):
                speech_end = candidate
                break

        agent_speaking = None
        for forward_idx in range(idx + 1, len(timeline)):
            candidate = timeline[forward_idx]
            if _debug_is_agent_speaking(candidate):
                agent_speaking = candidate
                break
            if _debug_is_user_committed(candidate):
                break

        last_user_committed_index = idx
        if speech_end is None or agent_speaking is None:
            continue

        user_text = str(entry["fields"].get("text") or "").strip()
        speech_end_to_committed_sec = round(entry["elapsed_sec"] - speech_end["elapsed_sec"], 3)
        committed_to_speaking_sec = round(agent_speaking["elapsed_sec"] - entry["elapsed_sec"], 3)
        full_bridge_sec = round(agent_speaking["elapsed_sec"] - speech_end["elapsed_sec"], 3)
        bridges.append(
            {
                "turn_number": len(bridges) + 1,
                "user_text": user_text,
                "speech_end_elapsed_sec": speech_end["elapsed_sec"],
                "user_committed_elapsed_sec": entry["elapsed_sec"],
                "agent_speaking_elapsed_sec": agent_speaking["elapsed_sec"],
                "speech_end_to_committed_sec": speech_end_to_committed_sec,
                "committed_to_speaking_sec": committed_to_speaking_sec,
                "full_bridge_sec": full_bridge_sec,
            }
        )

    if not bridges:
        return {"bridges": [], "summary": {}}

    settle = [item["speech_end_to_committed_sec"] for item in bridges]
    respond = [item["committed_to_speaking_sec"] for item in bridges]
    full = [item["full_bridge_sec"] for item in bridges]
    summary = {
        "bridge_count": len(bridges),
        "average_speech_end_to_committed_sec": round(sum(settle) / len(settle), 3),
        "average_committed_to_speaking_sec": round(sum(respond) / len(respond), 3),
        "average_full_bridge_sec": round(sum(full) / len(full), 3),
        "max_full_bridge_sec": round(max(full), 3),
        "min_full_bridge_sec": round(min(full), 3),
    }
    return {"bridges": bridges, "summary": summary}


def _build_debug_timeline(path_value: str, max_chars: int = DEBUG_LOG_MAX_CHARS) -> dict[str, Any]:
    raw_log = _read_log_tail(path_value, max_chars=max_chars)
    entries = _latest_debug_session_entries(raw_log.get("content", ""))
    if not entries:
        return {
            "log": raw_log,
            "entries": [],
            "summary": {},
        }

    first_dt = entries[0]["dt"]
    previous_dt = None
    timeline: list[dict[str, Any]] = []
    for entry in entries:
        current_dt = entry["dt"]
        elapsed_sec = round((current_dt - first_dt).total_seconds(), 3)
        delta_prev_sec = round((current_dt - previous_dt).total_seconds(), 3) if previous_dt else 0.0
        previous_dt = current_dt
        timeline.append(
            {
                "timestamp": entry["timestamp"],
                "category": entry["category"],
                "event": entry["event"],
                "elapsed_sec": elapsed_sec,
                "delta_prev_sec": delta_prev_sec,
                "fields": entry["fields"],
                "fields_summary": _summarize_debug_fields(entry["fields"]),
                "raw_line": entry["raw_line"],
            }
        )

    def _first_elapsed(category: str, event: str, field_key: str = "", field_value: str = "") -> float | None:
        for item in timeline:
            if item["category"] != category or item["event"] != event:
                continue
            if field_key and str(item["fields"].get(field_key, "")) != field_value:
                continue
            return item["elapsed_sec"]
        return None

    bridge_metrics = _build_bridge_metrics(timeline)
    summary = {
        "events_count": len(timeline),
        "session_duration_sec": timeline[-1]["elapsed_sec"],
        "first_agent_speaking_sec": _first_elapsed("AGENT", "agent_state_changed", "new_state", "speaking"),
        "first_assistant_committed_sec": _first_elapsed("TRANSCRIPT", "ASSISTANT_COMMITTED"),
        "first_user_speaking_sec": _first_elapsed("TURN", "user_state_changed", "new_state", "speaking"),
        "first_user_committed_sec": _first_elapsed("TRANSCRIPT", "USER_COMMITTED"),
        "first_tool_executed_sec": _first_elapsed("TOOL", "TOOL_EXECUTED"),
        "shutdown_started_sec": _first_elapsed("CALL", "shutdown_started"),
        "shutdown_finished_sec": _first_elapsed("CALL", "shutdown_finished"),
        **bridge_metrics["summary"],
    }

    raw_log["content"] = "\n".join(item["raw_line"] for item in timeline)
    return {"log": raw_log, "entries": timeline, "summary": summary, "bridges": bridge_metrics["bridges"]}


def _truncate_log(path_value: str) -> dict[str, Any]:
    path = Path(path_value)
    if not path.exists():
        return {"path": str(path), "exists": False, "cleared": False}
    path.write_text("", encoding="utf-8")
    return {"path": str(path), "exists": True, "cleared": True}


def _flash(request: Request, level: str, message: str) -> None:
    request.session["flash"] = {"level": level, "message": message}


def _consume_flash(request: Request) -> dict[str, str] | None:
    return request.session.pop("flash", None)


def _current_admin(request: Request, session: Session) -> AdminUser | None:
    admin_id = request.session.get("admin_user_id")
    if not admin_id:
        return None
    return session.get(AdminUser, admin_id)


def require_admin(request: Request, session: Session) -> AdminUser:
    admin = _current_admin(request, session)
    if admin is None or not admin.is_active:
        raise HTTPException(status_code=303, headers={"Location": "/admin/login"})
    return admin


def _integration_summary(session: Session, tenant_id: str) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for provider in ("google_calendar", "zoom", "email"):
        payload = get_integration_payload(session, tenant_id, provider)
        credentials = payload.get("credentials") or {}
        masked = {key: mask_secret(str(value)) for key, value in credentials.items() if isinstance(value, (str, int))}
        summary[provider] = {
            "status": payload.get("status", "missing"),
            "settings": payload.get("settings", {}),
            "credentials_masked": masked,
            "last_validated_at": payload.get("last_validated_at", ""),
            "last_error": payload.get("last_error", ""),
        }
    return summary


async def _sync_recent_outgoing_calls_with_telnyx(outgoing_db: Session, calls: list[Any]) -> list[Any]:
    synced: list[Any] = []
    started = time.perf_counter()
    for call in calls:
        if (
            getattr(call, "provider", "telnyx") != "telnyx"
            or call.status not in OUTGOING_ACTIVE_STATUSES
            or not call.telnyx_call_control_id
        ):
            synced.append(call)
            continue
        try:
            provider_payload = await get_call_details(call.telnyx_call_control_id)
            synced.append(sync_outgoing_call_from_provider(outgoing_db, call, provider_payload))
        except Exception as exc:
            logger.warning(
                "[OUTGOING_SYNC] provider sync failed call_id=%s provider=%s status=%s error=%s",
                getattr(call, "id", ""),
                getattr(call, "provider", ""),
                getattr(call, "status", ""),
                exc,
            )
            synced.append(call)
    elapsed = time.perf_counter() - started
    if elapsed > 1.0:
        logger.warning("[OUTGOING_SYNC] synced_recent_calls count=%s elapsed=%.3fs", len(calls), elapsed)
    return synced


def _call_recording_urls(call: Any) -> dict[str, str]:
    extra = dict(getattr(call, "extra_json", {}) or {})
    recording_urls = extra.get("public_recording_urls") or extra.get("recording_urls") or {}
    return recording_urls if isinstance(recording_urls, dict) else {}


def _call_needs_telnyx_recording_lookup(call: Any) -> bool:
    if getattr(call, "provider", "") != "telnyx":
        return False
    extra = dict(getattr(call, "extra_json", {}) or {})
    if not extra.get("livekit_first"):
        return False
    if not extra.get("recording_expected"):
        return False
    if _call_recording_urls(call):
        return False
    return getattr(call, "status", "") in {"completed", "failed"}


def _recording_match_score(call: Any, recording: dict[str, Any]) -> float | None:
    call_from = normalize_phone_number(str(getattr(call, "from_number", "") or ""))
    call_to = normalize_phone_number(str(getattr(call, "target_number", "") or ""))
    if call_from and call_from != normalize_phone_number(str(recording.get("from") or "")):
        return None
    if call_to and call_to != normalize_phone_number(str(recording.get("to") or "")):
        return None

    expected_connection_id = str((getattr(call, "extra_json", {}) or {}).get("telnyx_credential_connection_id") or "").strip()
    recording_connection_id = str(recording.get("connection_id") or "").strip()
    if expected_connection_id and recording_connection_id and expected_connection_id != recording_connection_id:
        return None

    call_time = (
        getattr(call, "ended_at", None)
        or getattr(call, "answered_at", None)
        or getattr(call, "started_at", None)
        or getattr(call, "created_at", None)
    )
    recording_time = (
        _parse_iso_datetime(recording.get("recording_ended_at"))
        or _parse_iso_datetime(recording.get("recording_started_at"))
        or _parse_iso_datetime(recording.get("created_at"))
        or _parse_iso_datetime(recording.get("updated_at"))
    )
    if call_time is None or recording_time is None:
        return 0.0
    return abs((recording_time - call_time).total_seconds())


async def _enrich_recent_outgoing_calls_with_recordings(outgoing_db: Session, calls: list[Any]) -> list[Any]:
    candidates = [call for call in calls if _call_needs_telnyx_recording_lookup(call)]
    if not candidates or not TELNYX_API_KEY:
        return calls

    try:
        recordings = await list_call_recordings(page_size=max(25, min(100, len(candidates) * 10)))
    except Exception as exc:
        logger.warning("[OUTGOING_RECORDINGS] lookup failed count=%s error=%s", len(candidates), exc)
        return calls

    used_recording_ids: set[str] = set()
    lookup_timestamp = datetime.now(timezone.utc).isoformat()
    for call in candidates:
        best_recording = None
        best_score = None
        for recording in recordings:
            recording_id = str(recording.get("id") or "").strip()
            if not recording_id or recording_id in used_recording_ids:
                continue
            download_urls = recording.get("download_urls") if isinstance(recording.get("download_urls"), dict) else {}
            if not download_urls:
                continue
            score = _recording_match_score(call, recording)
            if score is None or score > 7200:
                continue
            if best_score is None or score < best_score:
                best_recording = recording
                best_score = score

        if best_recording is None:
            update_outgoing_call_extra(
                outgoing_db,
                call,
                {
                    "recording_last_lookup_at": lookup_timestamp,
                    "recording_sync_pending": True,
                },
            )
            continue

        used_recording_ids.add(str(best_recording.get("id") or ""))
        download_urls = best_recording.get("download_urls") if isinstance(best_recording.get("download_urls"), dict) else {}
        update_outgoing_call_extra(
            outgoing_db,
            call,
            {
                "recording_last_lookup_at": lookup_timestamp,
                "recording_sync_pending": False,
                "recording_sync_source": "telnyx_recordings_api",
                "recording_id": str(best_recording.get("id") or ""),
                "recording_urls": download_urls,
                "public_recording_urls": download_urls,
                "recording_saved_at": str(best_recording.get("updated_at") or best_recording.get("created_at") or ""),
                "recording_started_at": str(best_recording.get("recording_started_at") or ""),
                "recording_ended_at": str(best_recording.get("recording_ended_at") or ""),
                "recording_duration_millis": best_recording.get("duration_millis"),
                "recording_status": str(best_recording.get("status") or ""),
            },
        )
    return calls


def _latest_email_events(limit: int = 10, tenant_id: str | None = None) -> list[dict[str, Any]]:
    path = Path("data") / "email_summary_events.jsonl"
    if not path.exists():
        return []
    try:
        lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except Exception:
        return []
    events: list[dict[str, Any]] = []
    for line in reversed(lines):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if tenant_id and str(payload.get("tenant_id") or "") != tenant_id:
            continue
        events.append(payload)
        if len(events) >= limit:
            break
    return events


@router.get("/admin/login")
async def admin_login_page(request: Request):
    return templates.TemplateResponse(
        request,
        "admin/login.html",
        {
            "page_title": "Admin Login",
            "flash": _consume_flash(request),
        },
    )


@router.post("/admin/login")
async def admin_login(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    email = str(form.get("email") or "").strip().lower()
    password = str(form.get("password") or "")
    admin = db.scalar(select(AdminUser).where(AdminUser.email == email))
    if admin is None or not verify_password(password, admin.password_hash):
        _flash(request, "error", "Invalid email or password")
        return RedirectResponse(url="/admin/login", status_code=303)
    request.session["admin_user_id"] = admin.id
    _flash(request, "success", "Welcome back.")
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/admin/logout")
async def admin_logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/admin/login", status_code=303)


@router.get("/admin")
async def admin_home(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    tenants = list(db.scalars(select(Tenant).order_by(Tenant.created_at.desc())))
    cards = []
    for tenant in tenants:
        config = get_active_config(db, tenant.id)
        cards.append(
            {
                "tenant": tenant,
                "phone_numbers": list(db.scalars(select(TenantPhoneNumber).where(TenantPhoneNumber.tenant_id == tenant.id))),
                "config": config,
            }
        )
    return templates.TemplateResponse(
        request,
        "admin/dashboard.html",
        {
            "page_title": "Tenant Dashboard",
            "flash": _consume_flash(request),
            "tenant_cards": cards,
            "public_base_url": PUBLIC_BASE_URL,
            "language_choices": supported_assistant_languages(),
            "stt_language_choices": supported_stt_languages(),
        },
    )


@router.post("/admin/tenants")
async def create_tenant_action(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    form = await request.form()
    slug = str(form.get("slug") or "").strip().lower()
    display_name = str(form.get("display_name") or "").strip()
    phone_number = str(form.get("phone_number") or "").strip()
    notes = str(form.get("notes") or "").strip()
    assistant_language = normalize_assistant_language(str(form.get("assistant_language") or "en"))
    tenant_prompt = str(form.get("tenant_prompt") or "").strip()
    stt_language = normalize_stt_language(str(form.get("stt_language") or ""), assistant_language)
    min_endpointing_delay, max_endpointing_delay = normalize_endpointing_window(
        form.get("min_endpointing_delay"),
        form.get("max_endpointing_delay"),
    )
    tts_voice = str(form.get("tts_voice") or "").strip()
    tts_speed = normalize_tts_speed(form.get("tts_speed"))
    if not slug or not display_name:
        _flash(request, "error", "Slug and display name are required")
        return RedirectResponse(url="/admin", status_code=303)
    if not tenant_prompt:
        _flash(request, "error", "A tenant base prompt is required when creating a tenant")
        return RedirectResponse(url="/admin", status_code=303)
    if get_tenant_by_slug(db, slug):
        _flash(request, "error", f"Tenant '{slug}' already exists")
        return RedirectResponse(url="/admin", status_code=303)
    tenant = create_tenant(
        db,
        slug,
        display_name,
        notes=notes,
        config_overrides={
            "assistant_language": assistant_language,
            "tenant_prompt": tenant_prompt,
            "stt_language": stt_language,
            "min_endpointing_delay": min_endpointing_delay,
            "max_endpointing_delay": max_endpointing_delay,
            "tts_voice": tts_voice,
            "tts_speed": tts_speed,
        },
    )
    if phone_number:
        upsert_phone_number(db, tenant, phone_number)
    _flash(request, "success", f"Tenant '{display_name}' created")
    return RedirectResponse(url=f"/admin/tenants/{tenant.slug}", status_code=303)


@router.get("/admin/tenants/{slug}")
async def tenant_detail(slug: str, request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    tenant = get_tenant_by_slug(db, slug)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    config = get_active_config(db, tenant.id)
    phone_numbers = list(db.scalars(select(TenantPhoneNumber).where(TenantPhoneNumber.tenant_id == tenant.id).order_by(TenantPhoneNumber.created_at.asc())))
    recent_events = list(
        db.scalars(
            select(CallEvent).where(CallEvent.tenant_id == tenant.id).order_by(CallEvent.created_at.desc()).limit(30)
        )
    )
    integration_summary = _integration_summary(db, tenant.id)
    runtime = build_runtime_context(db, tenant) if config else None
    selected_language = normalize_assistant_language((runtime or {}).get("config", {}).get("assistant_language", "en"))
    selected_voice = str((runtime or {}).get("config", {}).get("tts_voice") or "")
    voice_options: list[dict[str, Any]] = []
    voice_error = ""
    try:
        voice_options = get_cartesia_voice_options(selected_language, selected_voice=selected_voice)
    except Exception as exc:
        voice_error = str(exc)
    return templates.TemplateResponse(
        request,
        "admin/tenant_detail.html",
        {
            "page_title": f"Tenant {tenant.display_name}",
            "tenant": tenant,
            "config": config,
            "config_form": config_form_payload(config),
            "phone_numbers": phone_numbers,
            "integration_summary": integration_summary,
            "integration_forms": {
                provider: integration_form_payload(get_integration_payload(db, tenant.id, provider))
                for provider in ("google_calendar", "zoom", "email")
            },
            "recent_events": recent_events,
            "recent_email_events": _latest_email_events(tenant_id=tenant.id),
            "runtime": runtime,
            "language_choices": supported_assistant_languages(),
            "stt_language_choices": supported_stt_languages(),
            "cartesia_voice_options": voice_options,
            "cartesia_voice_error": voice_error,
            "agent_debug_log": _read_log_tail(AGENT_DEBUG_LOG_PATH),
            "agent_runtime_log": _read_log_tail(AGENT_LOG_PATH),
            "flash": _consume_flash(request),
        },
    )


@router.get("/admin/tenants/{slug}/debug-log")
async def tenant_debug_log(slug: str, request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    tenant = get_tenant_by_slug(db, slug)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return JSONResponse(
        {
            "ok": True,
            "tenant": tenant.slug,
            "agent_debug_log": _read_log_tail(AGENT_DEBUG_LOG_PATH),
            "agent_runtime_log": _read_log_tail(AGENT_LOG_PATH),
            "recent_email_events": _latest_email_events(tenant_id=tenant.id),
        }
    )


@router.post("/admin/tenants/{slug}/clear-log")
async def clear_tenant_log(slug: str, request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    tenant = get_tenant_by_slug(db, slug)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    form = await request.form()
    log_type = str(form.get("log_type") or "").strip().lower()
    if log_type == "runtime":
        _truncate_log(AGENT_LOG_PATH)
        _flash(request, "success", "Agent runtime log cleared")
    elif log_type == "debug":
        _truncate_log(AGENT_DEBUG_LOG_PATH)
        _flash(request, "success", "Live call debug log cleared")
    elif log_type == "outgoing_runtime":
        _truncate_log(OUTGOING_AGENT_LOG_PATH)
        _flash(request, "success", "Outgoing agent runtime log cleared")
    elif log_type == "outgoing_debug":
        _truncate_log(OUTGOING_AGENT_DEBUG_LOG_PATH)
        _flash(request, "success", "Outgoing call debug log cleared")
    else:
        _flash(request, "error", "Unknown log type")
    return RedirectResponse(url=f"/admin/tenants/{slug}", status_code=303)


@router.get("/admin/tenants/{slug}/outgoing")
async def tenant_outgoing_detail(
    slug: str,
    request: Request,
    db: Session = Depends(get_db),
    outgoing_db: Session = Depends(get_outgoing_db),
):
    require_admin(request, db)
    tenant = get_tenant_by_slug(db, slug)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    active_config = get_active_config(db, tenant.id)
    profile = ensure_outgoing_profile(outgoing_db, tenant, active_config=active_config)
    active_provider = str(profile.provider or "telnyx").strip().lower() or "telnyx"
    outgoing_numbers = list_outgoing_numbers(outgoing_db, tenant.id)
    provider_numbers = [item for item in outgoing_numbers if str(item.provider or "telnyx").strip().lower() == active_provider]
    selected_language = normalize_assistant_language(profile.assistant_language or getattr(active_config, "assistant_language", "en"))
    selected_voice = str(profile.tts_voice or getattr(active_config, "tts_voice", "") or "")
    voice_options: list[dict[str, Any]] = []
    voice_error = ""
    try:
        voice_options = get_cartesia_voice_options(selected_language, selected_voice=selected_voice)
    except Exception as exc:
        voice_error = str(exc)
    recent_calls = list_recent_outgoing_calls(outgoing_db, tenant.id)
    recent_calls = await _enrich_recent_outgoing_calls_with_recordings(outgoing_db, recent_calls)
    outgoing_debug_timeline = _build_debug_timeline(OUTGOING_AGENT_DEBUG_LOG_PATH)
    return templates.TemplateResponse(
        request,
        "admin/outgoing_calls.html",
        {
            "page_title": f"Outgoing Calls - {tenant.display_name}",
            "tenant": tenant,
            "config": active_config,
            "runtime": build_runtime_context(db, tenant) if active_config else None,
            "outgoing_profile_form": outgoing_profile_form_payload(profile, tenant),
            "outgoing_numbers": outgoing_numbers,
            "active_provider_numbers": provider_numbers,
            "recent_outgoing_calls": recent_calls,
            "outgoing_agent_debug_log": outgoing_debug_timeline["log"],
            "outgoing_agent_debug_timeline": outgoing_debug_timeline,
            "telnyx_key_configured": bool(TELNYX_API_KEY),
            "twilio_key_configured": bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN),
            "outgoing_handoff_mode": (TELNYX_OUTGOING_HANDOFF_MODE or "direct").strip().lower(),
            "default_outgoing_number": get_default_outgoing_number(outgoing_db, tenant.id, active_provider),
            "outgoing_provider_choices": OUTGOING_PROVIDER_CHOICES,
            "language_choices": supported_assistant_languages(),
            "stt_language_choices": supported_stt_languages(),
            "cartesia_voice_options": voice_options,
            "cartesia_voice_error": voice_error,
            "flash": _consume_flash(request),
        },
    )


@router.get("/admin/tenants/{slug}/outgoing/debug-log")
async def outgoing_debug_log(
    slug: str,
    request: Request,
    db: Session = Depends(get_db),
    outgoing_db: Session = Depends(get_outgoing_db),
):
    require_admin(request, db)
    tenant = get_tenant_by_slug(db, slug)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    outgoing_debug_timeline = _build_debug_timeline(OUTGOING_AGENT_DEBUG_LOG_PATH)
    return JSONResponse(
        {
            "ok": True,
            "tenant": tenant.slug,
            "outgoing_agent_debug_log": outgoing_debug_timeline["log"],
            "outgoing_agent_debug_timeline": outgoing_debug_timeline,
        }
    )


@router.post("/admin/tenants/{slug}/outgoing/config")
async def save_outgoing_config(
    slug: str,
    request: Request,
    db: Session = Depends(get_db),
    outgoing_db: Session = Depends(get_outgoing_db),
):
    require_admin(request, db)
    tenant = get_tenant_by_slug(db, slug)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    form = await request.form()
    payload = {
        "status": str(form.get("status") or "inactive").strip(),
        "provider": str(form.get("provider") or "telnyx").strip(),
        "telnyx_connection_id": str(form.get("telnyx_connection_id") or "").strip(),
        "assistant_language": str(form.get("assistant_language") or "").strip(),
        "stt_language": str(form.get("stt_language") or "").strip(),
        "llm_model": str(form.get("llm_model") or "").strip(),
        "tts_voice": str(form.get("tts_voice") or "").strip(),
        "tts_speed": form.get("tts_speed"),
        "min_endpointing_delay": form.get("min_endpointing_delay"),
        "max_endpointing_delay": form.get("max_endpointing_delay"),
        "opening_phrase": str(form.get("opening_phrase") or "").strip(),
        "system_prompt": str(form.get("system_prompt") or "").strip(),
        "caller_display_name": str(form.get("caller_display_name") or tenant.display_name).strip(),
        "notes": str(form.get("notes") or "").strip(),
    }
    save_outgoing_profile(outgoing_db, tenant, payload, active_config=get_active_config(db, tenant.id))
    _flash(request, "success", "Outgoing call settings saved")
    return RedirectResponse(url=f"/admin/tenants/{slug}/outgoing", status_code=303)


@router.post("/admin/tenants/{slug}/outgoing/events/clear")
async def clear_outgoing_events_action(
    slug: str,
    request: Request,
    db: Session = Depends(get_db),
    outgoing_db: Session = Depends(get_outgoing_db),
):
    require_admin(request, db)
    tenant = get_tenant_by_slug(db, slug)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    deleted = clear_outgoing_events(outgoing_db, tenant.id)
    _flash(request, "success", f"Cleared {deleted} outgoing event(s)")
    return RedirectResponse(url=f"/admin/tenants/{slug}/outgoing", status_code=303)


@router.post("/admin/tenants/{slug}/outgoing/sync")
async def sync_outgoing_calls_action(
    slug: str,
    request: Request,
    db: Session = Depends(get_db),
    outgoing_db: Session = Depends(get_outgoing_db),
):
    require_admin(request, db)
    tenant = get_tenant_by_slug(db, slug)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    recent_calls = list_recent_outgoing_calls(outgoing_db, tenant.id)
    synced_calls = await _sync_recent_outgoing_calls_with_telnyx(outgoing_db, recent_calls)
    synced_calls = await _enrich_recent_outgoing_calls_with_recordings(outgoing_db, synced_calls)
    active_count = sum(1 for call in synced_calls if getattr(call, "status", "") in OUTGOING_ACTIVE_STATUSES)
    _flash(request, "success", f"Provider sync completed for {len(synced_calls)} call(s). Active after sync: {active_count}.")
    return RedirectResponse(url=f"/admin/tenants/{slug}/outgoing", status_code=303)


@router.post("/admin/tenants/{slug}/outgoing/numbers")
async def save_outgoing_number_action(
    slug: str,
    request: Request,
    db: Session = Depends(get_db),
    outgoing_db: Session = Depends(get_outgoing_db),
):
    require_admin(request, db)
    tenant = get_tenant_by_slug(db, slug)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    form = await request.form()
    provider = str(form.get("provider") or "telnyx").strip()
    phone_number = normalize_phone_number(str(form.get("phone_number") or ""))
    label = str(form.get("label") or "primary").strip() or "primary"
    is_default = form.get("is_default") == "on"
    if not phone_number:
        _flash(request, "error", "An outgoing caller ID number is required")
        return RedirectResponse(url=f"/admin/tenants/{slug}/outgoing", status_code=303)
    upsert_outgoing_number(
        outgoing_db,
        tenant,
        provider=provider,
        phone_number=phone_number,
        label=label,
        is_default=is_default,
    )
    _flash(request, "success", f"Outgoing caller ID {phone_number} saved for {provider}")
    return RedirectResponse(url=f"/admin/tenants/{slug}/outgoing", status_code=303)


@router.post("/admin/tenants/{slug}/outgoing/calls")
async def launch_outgoing_call(
    slug: str,
    request: Request,
    db: Session = Depends(get_db),
    outgoing_db: Session = Depends(get_outgoing_db),
):
    require_admin(request, db)
    tenant = get_tenant_by_slug(db, slug)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    active_config = get_active_config(db, tenant.id)
    profile = ensure_outgoing_profile(outgoing_db, tenant, active_config=active_config)
    form = await request.form()
    target_number = normalize_phone_number(str(form.get("target_number") or ""))
    target_name = str(form.get("target_name") or "").strip()
    notes = str(form.get("notes") or "").strip()
    selected_from_number = normalize_phone_number(str(form.get("from_number") or ""))
    provider = str(profile.provider or "telnyx").strip().lower() or "telnyx"
    handoff_mode = (TELNYX_OUTGOING_HANDOFF_MODE or "direct").strip().lower()
    provider_numbers = {
        item.phone_number
        for item in list_outgoing_numbers(outgoing_db, tenant.id)
        if str(item.provider or "telnyx").strip().lower() == provider and item.status == "active"
    }
    default_number = get_default_outgoing_number(outgoing_db, tenant.id, provider)
    from_number = selected_from_number or (default_number.phone_number if default_number else "")
    if provider == "telnyx" and handoff_mode == "livekit_first":
        if not (LIVEKIT_URL and LIVEKIT_API_KEY and LIVEKIT_API_SECRET):
            _flash(request, "error", "LiveKit management API credentials are missing on the backend server")
            return RedirectResponse(url=f"/admin/tenants/{slug}/outgoing", status_code=303)
        if not (LIVEKIT_TELNYX_OUTBOUND_TRUNK_ID or (LIVEKIT_TELNYX_OUTBOUND_HOST and LIVEKIT_TELNYX_OUTBOUND_USERNAME and LIVEKIT_TELNYX_OUTBOUND_PASSWORD)):
            _flash(request, "error", "LiveKit Telnyx outbound trunk settings are missing on the backend server")
            return RedirectResponse(url=f"/admin/tenants/{slug}/outgoing", status_code=303)
    elif not (LIVEKIT_OUTGOING_SIP_URI and LIVEKIT_OUTGOING_SIP_USERNAME and LIVEKIT_OUTGOING_SIP_PASSWORD):
        _flash(request, "error", "The outgoing LiveKit SIP target is not configured on the backend server yet")
        return RedirectResponse(url=f"/admin/tenants/{slug}/outgoing", status_code=303)
    if not target_number:
        _flash(request, "error", "A destination phone number is required")
        return RedirectResponse(url=f"/admin/tenants/{slug}/outgoing", status_code=303)
    if not from_number:
        _flash(request, "error", "Save at least one outgoing caller ID for this tenant first")
        return RedirectResponse(url=f"/admin/tenants/{slug}/outgoing", status_code=303)
    if provider_numbers and from_number not in provider_numbers:
        _flash(request, "error", f"Choose a caller ID saved for the {provider} provider")
        return RedirectResponse(url=f"/admin/tenants/{slug}/outgoing", status_code=303)
    if profile.status != "active":
        _flash(request, "error", "Set the tenant's outgoing status to active before launching calls")
        return RedirectResponse(url=f"/admin/tenants/{slug}/outgoing", status_code=303)
    if provider == "telnyx" and not TELNYX_API_KEY:
        if handoff_mode != "livekit_first":
            _flash(request, "error", "TELNYX_API_KEY is missing on the backend server")
            return RedirectResponse(url=f"/admin/tenants/{slug}/outgoing", status_code=303)
    if provider == "telnyx" and not profile.telnyx_connection_id:
        if handoff_mode != "livekit_first":
            _flash(request, "error", "Save the tenant's Telnyx Voice API application ID first")
            return RedirectResponse(url=f"/admin/tenants/{slug}/outgoing", status_code=303)
    if provider == "twilio" and not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN):
        _flash(request, "error", "TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN is missing on the backend server")
        return RedirectResponse(url=f"/admin/tenants/{slug}/outgoing", status_code=303)

    call = create_outgoing_call(
        outgoing_db,
        tenant=tenant,
        profile=profile,
        target_number=target_number,
        from_number=from_number,
        target_name=target_name,
        notes=notes,
        tenant_config_version=active_config.version if active_config else 1,
    )
    call.extra_json = {
        **(call.extra_json or {}),
        "provider": provider,
        "handoff_mode": handoff_mode,
        "amd_mode": TELNYX_OUTGOING_AMD_MODE,
        "launch_notes": notes,
    }
    outgoing_db.flush()

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
                    logger.warning("[OUTGOING_RECORDINGS] could not enable Telnyx trunk recording call_id=%s error=%s", call.id, exc)
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
                outgoing_db.flush()
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
                outgoing_db.flush()
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
            outgoing_db.flush()
        _flash(request, "success", f"Outgoing {provider} call started to {target_number}")
    except Exception as exc:
        call.status = "failed"
        call.last_error = str(exc)
        outgoing_db.flush()
        _flash(request, "error", f"Could not start the outgoing call: {exc}")

    return RedirectResponse(url=f"/admin/tenants/{slug}/outgoing", status_code=303)


@router.get("/admin/cartesia/voices")
async def cartesia_voice_options(request: Request, language: str = "en", selected: str = "", db: Session = Depends(get_db)):
    require_admin(request, db)
    normalized_language = normalize_assistant_language(language)
    try:
        voices = get_cartesia_voice_options(normalized_language, selected_voice=selected)
        return JSONResponse({"ok": True, "language": normalized_language, "voices": voices})
    except Exception as exc:
        return JSONResponse({"ok": False, "language": normalized_language, "voices": [], "error": str(exc)})


@router.post("/admin/tenants/{slug}/config")
async def update_tenant_config(slug: str, request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    tenant = get_tenant_by_slug(db, slug)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    form = await request.form()
    enabled_tools = {
        "email_summary": form.get("tool_email_summary") == "on",
        "meeting_creation": form.get("tool_meeting_creation") == "on",
        "case_creation": form.get("tool_case_creation") == "on",
        "calendar_lookup": form.get("tool_calendar_lookup") == "on",
        "zoom_meetings": form.get("tool_zoom_meetings") == "on",
    }
    try:
        extra_settings = json.loads(str(form.get("extra_settings") or "{}"))
    except json.JSONDecodeError:
        _flash(request, "error", "Extra settings must be valid JSON")
        return RedirectResponse(url=f"/admin/tenants/{slug}", status_code=303)

    payload = {
        "business_name": str(form.get("business_name") or tenant.display_name).strip(),
        "assistant_language": normalize_assistant_language(str(form.get("assistant_language") or "en")),
        "timezone": str(form.get("timezone") or "Europe/Budapest").strip(),
        "greeting": str(form.get("greeting") or "").strip(),
        "tenant_prompt": str(form.get("tenant_prompt") or "").strip(),
        "services": parse_lines(str(form.get("services") or "")),
        "faq_notes": str(form.get("faq_notes") or "").strip(),
        "prompt_appendix": str(form.get("prompt_appendix") or "").strip(),
        "business_hours": str(form.get("business_hours") or "09:00-17:00").strip(),
        "business_days": str(form.get("business_days") or "1,2,3,4,5").strip(),
        "meeting_duration_minutes": int(str(form.get("meeting_duration_minutes") or 30).strip() or 30),
        "booking_horizon_days": int(str(form.get("booking_horizon_days") or 14).strip() or 14),
        "enabled_tools": enabled_tools,
        "llm_model": str(form.get("llm_model") or "gpt-4.1-mini").strip(),
        "stt_language": "",
        "min_endpointing_delay": form.get("min_endpointing_delay"),
        "max_endpointing_delay": form.get("max_endpointing_delay"),
        "tts_voice": str(form.get("tts_voice") or "").strip(),
        "tts_speed": normalize_tts_speed(form.get("tts_speed")),
        "owner_name": str(form.get("owner_name") or "").strip(),
        "owner_email": str(form.get("owner_email") or "").strip(),
        "reply_to_email": str(form.get("reply_to_email") or "").strip(),
        "from_email": str(form.get("from_email") or "").strip(),
        "notification_targets": parse_lines(str(form.get("notification_targets") or "")),
        "extra_settings": extra_settings,
    }
    payload["stt_language"] = normalize_stt_language(str(form.get("stt_language") or ""), payload["assistant_language"])
    (
        payload["min_endpointing_delay"],
        payload["max_endpointing_delay"],
    ) = normalize_endpointing_window(
        payload.get("min_endpointing_delay"),
        payload.get("max_endpointing_delay"),
    )
    if not payload["tenant_prompt"]:
        _flash(request, "error", "Tenant base prompt is required")
        return RedirectResponse(url=f"/admin/tenants/{slug}", status_code=303)
    create_config_version(db, tenant, payload)
    _flash(request, "success", "New configuration version saved")
    return RedirectResponse(url=f"/admin/tenants/{slug}", status_code=303)


@router.post("/admin/tenants/{slug}/phone-numbers")
async def add_phone_number(slug: str, request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    tenant = get_tenant_by_slug(db, slug)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    form = await request.form()
    phone_number = normalize_phone_number(str(form.get("phone_number") or ""))
    label = str(form.get("label") or "primary").strip() or "primary"
    if not phone_number:
        _flash(request, "error", "Phone number is required")
        return RedirectResponse(url=f"/admin/tenants/{slug}", status_code=303)
    upsert_phone_number(db, tenant, phone_number, label=label)
    _flash(request, "success", f"Phone number {phone_number} saved")
    return RedirectResponse(url=f"/admin/tenants/{slug}", status_code=303)


@router.post("/admin/tenants/{slug}/integrations/{provider}")
async def save_integration(slug: str, provider: str, request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    tenant = get_tenant_by_slug(db, slug)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    form = await request.form()
    try:
        credentials = json.loads(str(form.get("credentials") or "{}"))
        settings = json.loads(str(form.get("settings") or "{}"))
    except json.JSONDecodeError:
        _flash(request, "error", f"{provider} credentials/settings must be valid JSON")
        return RedirectResponse(url=f"/admin/tenants/{slug}", status_code=303)

    status = str(form.get("status") or "active").strip() or "active"
    upsert_integration(db, tenant, provider, credentials=credentials, settings=settings, status=status)
    _flash(request, "success", f"{provider} integration saved")
    return RedirectResponse(url=f"/admin/tenants/{slug}", status_code=303)


@router.post("/admin/tenants/{slug}/integrations/{provider}/validate")
async def validate_integration(slug: str, provider: str, request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    tenant = get_tenant_by_slug(db, slug)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    payload = get_integration_payload(db, tenant.id, provider)
    try:
        updated_credentials = None
        if provider == "google_calendar":
            result = validate_calendar_context(CalendarContext(payload["credentials"], payload["settings"]))
        elif provider == "zoom":
            result = validate_zoom_context(ZoomContext(payload["credentials"], payload["settings"]))
            updated_credentials = result.pop("updated_credentials", None)
        elif provider == "email":
            settings = payload.get("settings") or {}
            from_email = str(settings.get("from_email") or "").strip()
            reply_to = str(settings.get("reply_to_email") or "").strip()
            targets = [item for item in settings.get("notification_targets", []) if item]
            if not from_email:
                raise RuntimeError("Email integration requires from_email in settings")
            if not targets:
                raise RuntimeError("Email integration requires at least one notification target")
            resend_result = send_email_resend(
                to=str(targets[0]),
                subject=f"[AI Voice] Email validation for {tenant.display_name}",
                html=(
                    f"<p>This is a validation email for tenant <strong>{tenant.display_name}</strong>.</p>"
                    f"<p>If you received this, the Resend integration is working.</p>"
                ),
                from_email=from_email,
                reply_to=reply_to,
                tags=[{"name": "tool", "value": "email-validation"}],
            )
            result = {
                "ok": True,
                "from_email": from_email,
                "notification_target": targets[0],
                "resend_result": resend_result,
            }
        else:
            raise RuntimeError(f"Unsupported provider: {provider}")

        if updated_credentials:
            upsert_integration(
                db,
                tenant,
                provider,
                credentials=updated_credentials,
                settings=payload.get("settings") or {},
                status="active",
                mark_validated=True,
            )
        else:
            upsert_integration(
                db,
                tenant,
                provider,
                credentials=payload.get("credentials") or {},
                settings=payload.get("settings") or {},
                status="active",
                mark_validated=True,
            )
        summary = ", ".join(f"{key}={value}" for key, value in result.items() if key != "ok")
        _flash(request, "success", f"{provider} validation passed. {summary}")
    except Exception as exc:
        upsert_integration(
            db,
            tenant,
            provider,
            credentials=payload.get("credentials") or {},
            settings=payload.get("settings") or {},
            status="error",
            last_error=str(exc),
        )
        _flash(request, "error", f"{provider} validation failed: {exc}")
    return RedirectResponse(url=f"/admin/tenants/{slug}", status_code=303)
