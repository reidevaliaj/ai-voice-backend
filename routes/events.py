# routes/events.py
import json
import logging
import os
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, List, Dict, Any

from tools.storage import append_event
from tools.email_resend import send_email_resend
from tools.transcript_ai import analyze_transcript

router = APIRouter()
logger = logging.getLogger("events")

OWNER_EMAIL = os.getenv("OWNER_EMAIL", "info@cod-st.com")
FROM_EMAIL = os.getenv("FROM_EMAIL", "Code Studio <noreply@code-studio.eu>")
REPLY_TO_EMAIL = os.getenv("REPLY_TO_EMAIL", "Rej Aliaj <info@code-studio.eu>")


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

def _format_timestamp(ts: Optional[int]) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _to_html(text: str) -> str:
    return (text or "").replace("\n", "<br/>")


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
    append_event(
        "meeting_creation_events.jsonl",
        {
            "tenant_id": payload.tenant_id,
            "room_name": payload.room_name,
            "timestamp": payload.timestamp,
            "caller_name": analysis.get("caller_name", ""),
            "contact_email": analysis.get("contact_email", ""),
            "contact_phone": analysis.get("contact_phone", ""),
            "preferred_time_window": analysis.get("preferred_time_window", ""),
            "meeting_reason": analysis.get("meeting_reason", ""),
        },
    )
    logger.info(
        "[TOOL meeting-creation] queued room=%s caller=%s",
        payload.room_name,
        analysis.get("caller_name", ""),
    )


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
        # 1) store event (jsonl)
        append_event("call_end_events.jsonl", payload.model_dump())

        # 2) email notification (simple first version)
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
        analysis = analyze_transcript(payload.transcript, payload.messages)
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

        if bool(analysis.get("case_reported", False)):
            _run_case_creation(payload, analysis)
            tools_run.append("case-creation")

        return {"ok": True, "tools_run": tools_run}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
