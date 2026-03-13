# routes/events.py
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, List, Dict, Any

from tools.storage import append_event
from tools.email_resend import send_email_resend
from tools.transcript_ai import analyze_transcript, decide_call_end
from tools.google_calendar import (
    BUSINESS_TIMEZONE,
    check_meeting_slot,
    create_meeting_event,
    get_fallback_slots_next_two_weeks,
    get_free_slots_next_two_weeks,
    update_calendar_event_with_zoom,
)
from tools.zoom_meetings import create_zoom_meeting

router = APIRouter()
logger = logging.getLogger("events")

OWNER_EMAIL = os.getenv("OWNER_EMAIL", "info@cod-st.com")
FROM_EMAIL = os.getenv("FROM_EMAIL", "Code Studio <noreply@code-studio.eu>")
REPLY_TO_EMAIL = os.getenv("REPLY_TO_EMAIL", "Rej Aliaj <info@code-studio.eu>")
ENABLE_LEGACY_CALL_END_EMAIL = (
    os.getenv("ENABLE_LEGACY_CALL_END_EMAIL", "false").strip().lower() == "true"
)
MEETING_OWNER_EMAIL = os.getenv("MEETING_OWNER_EMAIL", "aliajrei@gmail.com").strip()
DEFAULT_MEETING_DURATION_MINUTES = int(
    os.getenv("DEFAULT_MEETING_DURATION_MINUTES", "30").strip() or "30"
)


class CallEndPayload(BaseModel):
    tenant_id: str
    call_type: str  # sales|support|meeting|other
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
    if not lines:
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
        start_dt = datetime.fromisoformat(value)
    except ValueError:
        try:
            start_dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return ""
    end_dt = start_dt + timedelta(minutes=max(duration_minutes, 1))
    return end_dt.isoformat()


def _send_email_summary(payload: TranscriptPayload, analysis: Dict[str, Any]) -> None:
    subject = f"[AI Voice] Call summary - {analysis.get('call_intent') or 'other'}"
    html = f"""
    <h3>Call Summary</h3>
    <p><b>Summary:</b> {_to_html(str(analysis.get("summary", "")))}</p>
    <p><b>Caller:</b> {analysis.get("caller_name", "")}</p>
    <p><b>Company:</b> {analysis.get("company", "")}</p>
    <p><b>Email:</b> {analysis.get("contact_email", "")}</p>
    <p><b>Phone:</b> {analysis.get("contact_phone", "")}</p>
    <p><b>Intent:</b> {analysis.get("call_intent", "")}</p>
    <p><b>Meeting requested:</b> {analysis.get("meeting_requested", False)}</p>
    <p><b>Case reported:</b> {analysis.get("case_reported", False)}</p>
    <p><b>Preferred time:</b> {analysis.get("preferred_time_window", "")}</p>
    <p><b>Problem:</b> {_to_html(str(analysis.get("problem_description", "")))}</p>
    <p><b>Room:</b> {payload.room_name or ""}</p>
    <p><b>Caller ID:</b> {payload.caller_id or ""}</p>
    <p><b>Call timestamp (UTC):</b> {_format_timestamp(payload.timestamp)}</p>
    <hr/>
    <p><b>Full transcript:</b></p>
    <p>{_to_html(payload.transcript)}</p>
    """

    send_email_resend(
        to=OWNER_EMAIL,
        subject=subject,
        html=html,
        from_email=FROM_EMAIL,
        reply_to=REPLY_TO_EMAIL,
        tags=[{"name": "tool", "value": "email-summary"}],
    )


def _run_meeting_creation(payload: TranscriptPayload, analysis: Dict[str, Any]) -> None:
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
        "meeting_timezone": analysis.get("meeting_timezone", BUSINESS_TIMEZONE),
        "status": "pending",
    }

    # LLM sometimes returns only meeting_start_iso. Infer end time deterministically.
    if (
        record["meeting_confirmed"]
        and record["meeting_start_iso"]
        and not record["meeting_end_iso"]
    ):
        inferred_end = _infer_end_iso(
            str(record["meeting_start_iso"]),
            DEFAULT_MEETING_DURATION_MINUTES,
        )
        if inferred_end:
            record["meeting_end_iso"] = inferred_end
            record["meeting_end_inferred"] = True
            logger.info(
                "[TOOL meeting-creation] inferred missing meeting_end_iso room=%s start=%s end=%s",
                payload.room_name,
                record["meeting_start_iso"],
                record["meeting_end_iso"],
            )

    try:
        if not record["meeting_confirmed"] or not record["meeting_start_iso"] or not record["meeting_end_iso"]:
            append_event("meeting_creation_events.jsonl", record)
            print(
                ">>> MEETING CREATION:",
                json.dumps(record, ensure_ascii=False),
                flush=True,
            )
            logger.info(
                "[TOOL meeting-creation] pending (no confirmed slot) room=%s caller=%s",
                payload.room_name,
                analysis.get("caller_name", ""),
            )
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
        attendees: List[str] = [MEETING_OWNER_EMAIL]
        if "@" in client_email:
            attendees.append(client_email)
        attendees = list(dict.fromkeys([a for a in attendees if "@" in a]))

        create_resp = create_meeting_event(
            title=title,
            description=description,
            start_iso=str(record["meeting_start_iso"]),
            end_iso=str(record["meeting_end_iso"]),
            attendees=attendees,
        )
        record["calendar_result"] = create_resp

        if not create_resp.get("created"):
            record["status"] = "skipped"
            append_event("meeting_creation_events.jsonl", record)
            print(
                ">>> MEETING CREATION:",
                json.dumps(record, ensure_ascii=False),
                flush=True,
            )
            logger.info(
                "[TOOL meeting-creation] status=%s room=%s result=%s",
                record["status"],
                payload.room_name,
                json.dumps({"calendar": create_resp}, ensure_ascii=False),
            )
            return

        zoom_result: Dict[str, Any] = {"created": False}
        calendar_zoom_update: Dict[str, Any] = {"updated": False}
        try:
            zoom_result = create_zoom_meeting(
                start_iso=str(record["meeting_start_iso"]),
                end_iso=str(record["meeting_end_iso"]),
                topic=title,
                agenda=description,
                client_email=client_email,
                timezone_name=str(record.get("meeting_timezone") or BUSINESS_TIMEZONE),
            )
            if zoom_result.get("created") and create_resp.get("event_id"):
                calendar_zoom_update = update_calendar_event_with_zoom(
                    event_id=str(create_resp.get("event_id")),
                    zoom_join_url=str(zoom_result.get("join_url") or ""),
                    attendees=attendees,
                )
        except Exception as e:
            logger.exception("[TOOL meeting-creation] zoom step failed room=%s", payload.room_name)
            zoom_result = {"created": False, "error": str(e)}

        record["zoom_result"] = zoom_result
        record["calendar_zoom_update"] = calendar_zoom_update
        record["status"] = "created_with_zoom" if zoom_result.get("created") else "created_without_zoom"
        append_event("meeting_creation_events.jsonl", record)
        print(
            ">>> MEETING CREATION:",
            json.dumps(record, ensure_ascii=False),
            flush=True,
        )
        logger.info(
            "[TOOL meeting-creation] status=%s room=%s result=%s",
            record["status"],
            payload.room_name,
            json.dumps(
                {
                    "calendar": create_resp,
                    "zoom": zoom_result,
                    "calendar_zoom_update": calendar_zoom_update,
                },
                ensure_ascii=False,
            ),
        )
    except Exception as e:
        record["status"] = "error"
        record["error"] = str(e)
        append_event("meeting_creation_events.jsonl", record)
        print(
            ">>> MEETING CREATION ERROR:",
            json.dumps(record, ensure_ascii=False),
            flush=True,
        )
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
    logger.info(
        "[TOOL case-creation] queued room=%s caller=%s",
        payload.room_name,
        analysis.get("caller_name", ""),
    )


@router.post("/events/call-end")
async def call_end(payload: CallEndPayload):
    try:
        logger.info(
            "[CALL_END_EVENT] received tenant_id=%s room=%s call_type=%s",
            payload.tenant_id,
            payload.room_name,
            payload.call_type,
        )
        # 1) store event (jsonl)
        append_event("call_end_events.jsonl", payload.model_dump())

        # 2) Legacy email path is disabled by default to avoid duplicate emails.
        if ENABLE_LEGACY_CALL_END_EMAIL:
            subject = f"[Code Studio] Call summary ({payload.call_type})"
            notes_html = (payload.notes or "").replace("\n", "<br/>")
            html = f"""
            <h3>Call Summary</h3>
            <p><b>Type:</b> {payload.call_type}</p>
            <p><b>Name:</b> {payload.name}</p>
            <p><b>Company:</b> {payload.company}</p>
            <p><b>Email:</b> {payload.contact_email}</p>
            <p><b>Phone:</b> {payload.contact_phone}</p>
            <p><b>Topic:</b> {payload.topic}</p>
            <p><b>Urgency:</b> {payload.urgency}</p>
            <p><b>Preferred time:</b> {payload.preferred_time_window}</p>
            <p><b>Caller ID:</b> {payload.caller_id}</p>
            <p><b>Room:</b> {payload.room_name}</p>
            <hr/>
            <p><b>Notes:</b></p>
            <p>{notes_html}</p>
            """

            send_email_resend(
                to=OWNER_EMAIL,
                subject=subject,
                html=html,
                from_email=FROM_EMAIL,
                reply_to=REPLY_TO_EMAIL,
            )
            logger.info("[CALL_END_EVENT] legacy email sent room=%s", payload.room_name)
        else:
            logger.info(
                "[CALL_END_EVENT] legacy email skipped room=%s (ENABLE_LEGACY_CALL_END_EMAIL=false)",
                payload.room_name,
            )

        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/events/transcript")
async def transcript_event(payload: TranscriptPayload):
    try:
        append_event("transcript_events.jsonl", payload.model_dump())
        print(
            ">>> TRANSCRIPT EVENT:",
            json.dumps(payload.model_dump(), ensure_ascii=False),
            flush=True,
        )
        logger.info(
            "[TRANSCRIPT_EVENT] received tenant_id=%s room=%s caller_id=%s reason=%s messages=%s chars=%s",
            payload.tenant_id,
            payload.room_name,
            payload.caller_id,
            payload.shutdown_reason,
            len(payload.messages),
            len(payload.transcript or ""),
        )
        logger.info(
            "[TRANSCRIPT_EVENT] preview=%s",
            (payload.transcript or "")[:500].replace("\n", " | "),
        )
        logger.info(
            "[TRANSCRIPT_EVENT] raw=%s",
            json.dumps(payload.model_dump(), ensure_ascii=False),
        )
        analysis = analyze_transcript(
            payload.transcript,
            payload.messages,
            current_time_utc_iso=_format_timestamp(payload.timestamp),
            business_timezone=BUSINESS_TIMEZONE,
        )
        append_event("transcript_analysis_events.jsonl", analysis)
        logger.info("[TRANSCRIPT_DECISION] %s", json.dumps(analysis, ensure_ascii=False))

        tools_run: List[str] = []

        # Default tool: always run email-summary.
        _send_email_summary(payload, analysis)
        tools_run.append("email-summary")
        logger.info("[TOOL email-summary] sent owner=%s room=%s", OWNER_EMAIL, payload.room_name)

        if bool(analysis.get("meeting_requested", False)):
            _run_meeting_creation(payload, analysis)
            tools_run.append("meeting-creation")
        else:
            logger.info(
                "[TOOL meeting-creation] skipped (meeting_requested=false) room=%s",
                payload.room_name,
            )

        if bool(analysis.get("case_reported", False)):
            _run_case_creation(payload, analysis)
            tools_run.append("case-creation")

        return {"ok": True, "tools_run": tools_run}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/tools/check-availability")
async def check_availability(req: CheckAvailabilityReq):
    try:
        result = get_free_slots_next_two_weeks(
            duration_minutes=req.duration_minutes,
            max_slots=req.max_slots,
        )

        logger.info(
            "[TOOL check-availability] tenant_id=%s slots=%s duration=%s",
            req.tenant_id,
            len(result.get("slots", [])),
            req.duration_minutes,
        )
        return {"ok": True, **result}
    except Exception as e:
        logger.exception(
            "[TOOL check-availability] calendar lookup failed tenant_id=%s duration=%s",
            req.tenant_id,
            req.duration_minutes,
        )
        fallback = get_fallback_slots_next_two_weeks(
            duration_minutes=req.duration_minutes,
            max_slots=req.max_slots,
        )
        logger.warning(
            "[TOOL check-availability] using fallback slots tenant_id=%s slots=%s error=%s",
            req.tenant_id,
            len(fallback.get("slots", [])),
            str(e),
        )
        return {
            "ok": True,
            "degraded": True,
            "degraded_reason": str(e),
            **fallback,
        }


@router.post("/tools/check-meeting-slot")
async def check_meeting_slot_route(req: CheckMeetingSlotReq):
    try:
        result = check_meeting_slot(
            preferred_start_iso=req.preferred_start_iso,
            duration_minutes=req.duration_minutes,
            alternatives_limit=req.alternatives_limit,
        )
        logger.info(
            "[TOOL check-meeting-slot] tenant_id=%s status=%s preferred=%s duration=%s",
            req.tenant_id,
            result.get("status"),
            req.preferred_start_iso,
            req.duration_minutes,
        )
        return {"ok": True, **result}
    except Exception as e:
        logger.exception(
            "[TOOL check-meeting-slot] failed tenant_id=%s preferred=%s duration=%s",
            req.tenant_id,
            req.preferred_start_iso,
            req.duration_minutes,
        )
        return {
            "ok": True,
            "status": "unavailable",
            "degraded": True,
            "degraded_reason": str(e),
            "next_slots": [],
            "day_blocks": [],
            "timezone": BUSINESS_TIMEZONE,
            "duration_minutes": req.duration_minutes,
        }


@router.post("/tools/validate-call-end")
async def validate_call_end_route(payload: ValidateCallEndPayload):
    try:
        decision = decide_call_end(payload.model_dump())
        event = {
            "decision": decision,
            "preview": (payload.transcript or "")[:400],
        }
        append_event("call_end_validation_events.jsonl", event)
        logger.info(
            "[TOOL validate-call-end] end_call=%s",
            decision.get("end_call", 0),
        )
        return {"ok": True, "end_call": int(decision.get("end_call", 0) or 0)}
    except Exception:
        logger.exception("[TOOL validate-call-end] failed")
        return {
            "ok": True,
            "end_call": 0,
        }


@router.get("/debug/meeting-last")
async def debug_meeting_last():
    return {
        "ok": True,
        "last_meeting_creation": _load_last_jsonl_record("meeting_creation_events.jsonl"),
        "last_transcript_analysis": _load_last_jsonl_record("transcript_analysis_events.jsonl"),
        "last_transcript_event": _load_last_jsonl_record("transcript_events.jsonl"),
    }
