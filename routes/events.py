# routes/events.py
import json
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, List, Dict, Any

from tools.storage import append_event
from tools.email_resend import send_email_resend

router = APIRouter()

OWNER_EMAIL = "info@code-studio.eu"  # change if you want


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
            from_email="Code Studio <noreply@code-studio.eu>",
            reply_to="Rej Aliaj <info@code-studio.eu>",
        )

        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/events/transcript")
async def transcript_event(payload: TranscriptPayload):
    try:
        print(
            ">>> TRANSCRIPT EVENT:",
            json.dumps(payload.model_dump(), ensure_ascii=False),
            flush=True,
        )
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
