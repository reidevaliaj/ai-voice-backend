import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from db import get_db
from services.call_events import log_call_event
from services.tenants import build_runtime_context, get_tenant_by_id
from tools.email_resend import send_email_resend
from tools.google_calendar import (
    CalendarContext,
    check_meeting_slot,
    create_meeting_event,
    get_fallback_slots_next_two_weeks,
    get_free_slots_next_two_weeks,
    update_calendar_event_with_zoom,
)
from tools.storage import append_event
from tools.transcript_ai import analyze_transcript, decide_call_end
from tools.zoom_meetings import ZoomContext, create_zoom_meeting

router = APIRouter()
logger = logging.getLogger("events")


class CallEndPayload(BaseModel):
    tenant_id: str
    call_type: str
    name: Optional[str] = ""
    company: Optional[str] = ""
    contact_email: Optional[str] = ""
    contact_phone: Optional[str] = ""
    topic: Optional[str] = ""
    notes: Optional[str] = ""
    urgency: Optional[str] = ""
    preferred_time_window: Optional[str] = ""
    room_name: Optional[str] = None
    caller_id: Optional[str] = None
    timestamp: Optional[int] = None


class TranscriptPayload(BaseModel):
    tenant_id: str
    room_name: Optional[str] = None
    caller_id: Optional[str] = None
    shutdown_reason: Optional[str] = None
    timestamp: Optional[int] = None
    transcript: str = ""
    messages: List[Dict[str, Any]] = []


class ValidateCallEndPayload(BaseModel):
    tenant_id: str = ""
    transcript: str = ""


class CheckAvailabilityReq(BaseModel):
    tenant_id: str = "default"
    duration_minutes: int = 30
    max_slots: int = 200


class CheckMeetingSlotReq(BaseModel):
    tenant_id: str = "default"
    preferred_start_iso: str
    duration_minutes: int = 30
    alternatives_limit: int = 3


def _load_last_jsonl_record(filename: str) -> Optional[Dict[str, Any]]:
    path = Path("data") / filename
    if not path.exists():
        return None
    try:
        lines = path.read_text(encoding="utf-8").strip().splitlines()
    except Exception:
        return None
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


def _format_timestamp(ts: Optional[int]) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _to_html(text: str) -> str:
    return (text or "").replace("\n", "<br/>")


def _infer_end_iso(start_iso: str, duration_minutes: int) -> str:
    value = (start_iso or "").strip()
    if not value:
        return ""
    try:
        start_dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return (start_dt + timedelta(minutes=max(duration_minutes, 1))).isoformat()


def _runtime_or_404(db: Session, tenant_id: str) -> dict[str, Any]:
    tenant = get_tenant_by_id(db, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail=f"Tenant {tenant_id} not found")
    return build_runtime_context(db, tenant)


def _business_context(runtime: dict[str, Any]) -> dict[str, Any]:
    config = runtime["config"]
    return {
        "business_name": config["business_name"],
        "services": config["services"],
        "faq_notes": config["faq_notes"],
        "prompt_appendix": config["prompt_appendix"],
        "business_hours": config["business_hours"],
        "business_days": config["business_days"],
    }


def _calendar_context(runtime: dict[str, Any]) -> CalendarContext:
    config = runtime["config"]
    integration = runtime["integrations"]["google_calendar"]
    settings = {
        **dict(integration.get("settings") or {}),
        "business_timezone": config["timezone"],
        "business_hours": config["business_hours"],
        "business_days": config["business_days"],
    }
    return CalendarContext(integration.get("credentials") or {}, settings)


def _zoom_context(runtime: dict[str, Any]) -> ZoomContext:
    integration = runtime["integrations"]["zoom"]
    return ZoomContext(integration.get("credentials") or {}, integration.get("settings") or {})


def _persist_zoom_credentials_if_changed(db: Session, runtime: dict[str, Any], updated_credentials: dict[str, Any] | None) -> None:
    if not updated_credentials:
        return
    from services.tenants import upsert_integration

    tenant = get_tenant_by_id(db, runtime["tenant"]["id"])
    if tenant is None:
        return
    current = runtime["integrations"]["zoom"]
    upsert_integration(
        db,
        tenant,
        "zoom",
        credentials=updated_credentials,
        settings=current.get("settings") or {},
        status="active",
    )


def _send_email_summary(payload: TranscriptPayload, analysis: Dict[str, Any], runtime: dict[str, Any]) -> None:
    config = runtime["config"]
    email_settings = runtime["integrations"]["email"].get("settings") or {}
    targets = list(email_settings.get("notification_targets") or config.get("notification_targets") or [])
    if not targets:
        targets = [config.get("owner_email")]
    subject = f"[AI Voice] {config['business_name']} call summary - {analysis.get('call_intent') or 'other'}"
    html = f"""
    <h3>Call Summary</h3>
    <p><b>Tenant:</b> {runtime['tenant']['slug']}</p>
    <p><b>Summary:</b> {_to_html(str(analysis.get('summary', '')))}</p>
    <p><b>Caller:</b> {analysis.get('caller_name', '')}</p>
    <p><b>Company:</b> {analysis.get('company', '')}</p>
    <p><b>Email:</b> {analysis.get('contact_email', '')}</p>
    <p><b>Phone:</b> {analysis.get('contact_phone', '')}</p>
    <p><b>Intent:</b> {analysis.get('call_intent', '')}</p>
    <p><b>Meeting requested:</b> {analysis.get('meeting_requested', False)}</p>
    <p><b>Case reported:</b> {analysis.get('case_reported', False)}</p>
    <p><b>Preferred time:</b> {analysis.get('preferred_time_window', '')}</p>
    <p><b>Problem:</b> {_to_html(str(analysis.get('problem_description', '')))}</p>
    <p><b>Room:</b> {payload.room_name or ''}</p>
    <p><b>Caller ID:</b> {payload.caller_id or ''}</p>
    <p><b>Call timestamp (UTC):</b> {_format_timestamp(payload.timestamp)}</p>
    <hr/>
    <p><b>Full transcript:</b></p>
    <p>{_to_html(payload.transcript)}</p>
    """
    for target in [item for item in targets if item]:
        send_email_resend(
            to=target,
            subject=subject,
            html=html,
            from_email=str(email_settings.get("from_email") or config.get("from_email") or config.get("owner_email") or "noreply@example.com"),
            reply_to=str(email_settings.get("reply_to_email") or config.get("reply_to_email") or config.get("owner_email") or ""),
            tags=[{"name": "tool", "value": "email-summary"}],
        )


def _run_meeting_creation(db: Session, payload: TranscriptPayload, analysis: Dict[str, Any], runtime: dict[str, Any]) -> None:
    config = runtime["config"]
    calendar_ctx = _calendar_context(runtime)
    zoom_ctx = _zoom_context(runtime)
    meeting_owner_email = str(config.get("extra_settings", {}).get("meeting_owner_email") or config.get("owner_email") or "").strip()
    record: Dict[str, Any] = {
        "tenant_id": payload.tenant_id,
        "room_name": payload.room_name,
        "timestamp": payload.timestamp,
        "caller_name": analysis.get("caller_name", ""),
        "contact_email": analysis.get("contact_email", ""),
        "contact_phone": analysis.get("contact_phone", ""),
        "preferred_time_window": analysis.get("preferred_time_window", ""),
        "meeting_reason": analysis.get("meeting_reason", ""),
        "meeting_confirmed": bool(analysis.get("meeting_confirmed", False)),
        "meeting_start_iso": analysis.get("meeting_start_iso", ""),
        "meeting_end_iso": analysis.get("meeting_end_iso", ""),
        "meeting_timezone": analysis.get("meeting_timezone", config["timezone"]),
        "status": "pending",
    }

    if record["meeting_confirmed"] and record["meeting_start_iso"] and not record["meeting_end_iso"]:
        inferred_end = _infer_end_iso(str(record["meeting_start_iso"]), config["meeting_duration_minutes"])
        if inferred_end:
            record["meeting_end_iso"] = inferred_end
            record["meeting_end_inferred"] = True

    try:
        if not record["meeting_confirmed"] or not record["meeting_start_iso"] or not record["meeting_end_iso"]:
            append_event("meeting_creation_events.jsonl", record)
            return

        title = f"Call with {analysis.get('caller_name') or 'Lead'}"
        description = (
            f"Source room: {payload.room_name}\n"
            f"Caller: {analysis.get('caller_name', '')}\n"
            f"Company: {analysis.get('company', '')}\n"
            f"Email: {analysis.get('contact_email', '')}\n"
            f"Phone: {analysis.get('contact_phone', '')}\n"
            f"Reason: {analysis.get('meeting_reason', '')}\n"
            f"Summary: {analysis.get('summary', '')}"
        )
        client_email = str(analysis.get("contact_email", "") or "").strip()
        attendees: List[str] = [meeting_owner_email] if "@" in meeting_owner_email else []
        if "@" in client_email:
            attendees.append(client_email)
        attendees = list(dict.fromkeys(attendees))

        create_resp = create_meeting_event(
            title=title,
            description=description,
            start_iso=str(record["meeting_start_iso"]),
            end_iso=str(record["meeting_end_iso"]),
            attendees=attendees,
            context=calendar_ctx,
            horizon_days=int(config["booking_horizon_days"]),
        )
        record["calendar_result"] = create_resp
        if not create_resp.get("created"):
            record["status"] = "skipped"
            append_event("meeting_creation_events.jsonl", record)
            return

        zoom_result: Dict[str, Any] = {"created": False}
        calendar_zoom_update: Dict[str, Any] = {"updated": False}
        try:
            if runtime["config"]["enabled_tools"].get("zoom_meetings", True):
                zoom_result = create_zoom_meeting(
                    start_iso=str(record["meeting_start_iso"]),
                    end_iso=str(record["meeting_end_iso"]),
                    topic=title,
                    agenda=description,
                    client_email=client_email,
                    timezone_name=str(record.get("meeting_timezone") or config["timezone"]),
                    context=zoom_ctx,
                )
                _persist_zoom_credentials_if_changed(db, runtime, zoom_result.get("updated_credentials"))
                if zoom_result.get("created") and create_resp.get("event_id"):
                    calendar_zoom_update = update_calendar_event_with_zoom(
                        event_id=str(create_resp.get("event_id")),
                        zoom_join_url=str(zoom_result.get("join_url") or ""),
                        attendees=attendees,
                        context=calendar_ctx,
                    )
        except Exception as exc:
            logger.exception("[TOOL meeting-creation] zoom step failed room=%s", payload.room_name)
            zoom_result = {"created": False, "error": str(exc)}

        record["zoom_result"] = zoom_result
        record["calendar_zoom_update"] = calendar_zoom_update
        record["status"] = "created_with_zoom" if zoom_result.get("created") else "created_without_zoom"
        append_event("meeting_creation_events.jsonl", record)
    except Exception as exc:
        record["status"] = "error"
        record["error"] = str(exc)
        append_event("meeting_creation_events.jsonl", record)
        logger.exception("[TOOL meeting-creation] pipeline failed room=%s", payload.room_name)


def _run_case_creation(payload: TranscriptPayload, analysis: Dict[str, Any]) -> None:
    append_event(
        "case_creation_events.jsonl",
        {
            "tenant_id": payload.tenant_id,
            "room_name": payload.room_name,
            "timestamp": payload.timestamp,
            "caller_name": analysis.get("caller_name", ""),
            "company": analysis.get("company", ""),
            "contact_email": analysis.get("contact_email", ""),
            "contact_phone": analysis.get("contact_phone", ""),
            "case_reason": analysis.get("case_reason", ""),
            "problem_description": analysis.get("problem_description", ""),
        },
    )


@router.post("/events/call-end")
async def call_end(payload: CallEndPayload, db: Session = Depends(get_db)):
    try:
        runtime = _runtime_or_404(db, payload.tenant_id)
        append_event("call_end_events.jsonl", payload.model_dump())
        log_call_event(
            db,
            event_type="call_end_event",
            payload={
                **payload.model_dump(),
                "call_sid": payload.room_name or "",
                "caller_number": payload.caller_id or payload.contact_phone or "",
            },
            tenant=get_tenant_by_id(db, payload.tenant_id),
            room_name=payload.room_name or "",
        )
        return {"ok": True, "tenant": runtime["tenant"]["slug"]}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/events/transcript")
async def transcript_event(payload: TranscriptPayload, db: Session = Depends(get_db)):
    try:
        runtime = _runtime_or_404(db, payload.tenant_id)
        append_event("transcript_events.jsonl", payload.model_dump())
        log_call_event(
            db,
            event_type="transcript_event",
            payload={
                **payload.model_dump(),
                "caller_number": payload.caller_id or "",
                "call_status": payload.shutdown_reason or "",
            },
            tenant=get_tenant_by_id(db, payload.tenant_id),
            room_name=payload.room_name or "",
        )
        analysis = analyze_transcript(
            payload.transcript,
            payload.messages,
            current_time_utc_iso=_format_timestamp(payload.timestamp),
            business_timezone=runtime["config"]["timezone"],
            business_context=_business_context(runtime),
        )
        append_event("transcript_analysis_events.jsonl", analysis)
        tools_run: List[str] = []
        enabled_tools = runtime["config"]["enabled_tools"]

        if enabled_tools.get("email_summary", True):
            _send_email_summary(payload, analysis, runtime)
            tools_run.append("email-summary")
        if enabled_tools.get("meeting_creation", True) and bool(analysis.get("meeting_requested", False)):
            _run_meeting_creation(db, payload, analysis, runtime)
            tools_run.append("meeting-creation")
        if enabled_tools.get("case_creation", True) and bool(analysis.get("case_reported", False)):
            _run_case_creation(payload, analysis)
            tools_run.append("case-creation")

        return {"ok": True, "tools_run": tools_run, "tenant": runtime["tenant"]["slug"]}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/tools/check-availability")
async def check_availability(req: CheckAvailabilityReq, db: Session = Depends(get_db)):
    runtime = _runtime_or_404(db, req.tenant_id)
    calendar_ctx = _calendar_context(runtime)
    try:
        result = get_free_slots_next_two_weeks(
            duration_minutes=req.duration_minutes,
            max_slots=req.max_slots,
            context=calendar_ctx,
            horizon_days=int(runtime["config"]["booking_horizon_days"]),
        )
        return {"ok": True, **result}
    except Exception as exc:
        fallback = get_fallback_slots_next_two_weeks(
            duration_minutes=req.duration_minutes,
            max_slots=req.max_slots,
            context=calendar_ctx,
            horizon_days=int(runtime["config"]["booking_horizon_days"]),
        )
        return {"ok": True, "degraded": True, "degraded_reason": str(exc), **fallback}


@router.post("/tools/check-meeting-slot")
async def check_meeting_slot_route(req: CheckMeetingSlotReq, db: Session = Depends(get_db)):
    runtime = _runtime_or_404(db, req.tenant_id)
    calendar_ctx = _calendar_context(runtime)
    try:
        result = check_meeting_slot(
            preferred_start_iso=req.preferred_start_iso,
            duration_minutes=req.duration_minutes,
            alternatives_limit=req.alternatives_limit,
            context=calendar_ctx,
            horizon_days=int(runtime["config"]["booking_horizon_days"]),
        )
        return {"ok": True, **result}
    except Exception as exc:
        return {
            "ok": True,
            "status": "unavailable",
            "degraded": True,
            "degraded_reason": str(exc),
            "next_slots": [],
            "day_blocks": [],
            "timezone": runtime["config"]["timezone"],
            "duration_minutes": req.duration_minutes,
        }


@router.post("/tools/validate-call-end")
async def validate_call_end_route(payload: ValidateCallEndPayload, db: Session = Depends(get_db)):
    business_context = None
    if payload.tenant_id:
        try:
            runtime = _runtime_or_404(db, payload.tenant_id)
            business_context = _business_context(runtime)
        except HTTPException:
            business_context = None
    decision = decide_call_end(payload.model_dump(), business_context=business_context)
    append_event("call_end_validation_events.jsonl", {"decision": decision, "preview": (payload.transcript or "")[:400]})
    return {"ok": True, "end_call": int(decision.get("end_call", 0) or 0)}


@router.get("/debug/meeting-last")
async def debug_meeting_last():
    return {
        "ok": True,
        "last_meeting_creation": _load_last_jsonl_record("meeting_creation_events.jsonl"),
        "last_transcript_analysis": _load_last_jsonl_record("transcript_analysis_events.jsonl"),
        "last_transcript_event": _load_last_jsonl_record("transcript_events.jsonl"),
    }
