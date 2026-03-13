import json
import logging
import os
import re
from typing import Any, Dict, List
from urllib import request

logger = logging.getLogger("transcript_ai")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("TRANSCRIPT_DECISION_MODEL", "gpt-4.1-mini")
CALL_END_MODEL = os.getenv("CALL_END_DECISION_MODEL", OPENAI_MODEL or "gpt-4.1-mini")
OPENAI_URL = "https://api.openai.com/v1/chat/completions"


def _normalize_email(value: str) -> str:
    if not value:
        return ""
    text = value.strip().lower()
    text = re.sub(r"\s+", " ", text)
    text = text.replace(" at ", "@")
    text = text.replace("(at)", "@")
    text = text.replace(" dot ", ".")
    text = text.replace("(dot)", ".")
    text = text.replace(" underscore ", "_")
    text = text.replace(" dash ", "-")
    text = text.replace(" ", "")

    if re.fullmatch(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", text):
        return text
    return ""


def _fallback_result() -> Dict[str, Any]:
    return {
        "summary": "",
        "caller_name": "",
        "company": "",
        "contact_email": "",
        "contact_phone": "",
        "call_intent": "other",
        "meeting_requested": False,
        "case_reported": False,
        "meeting_reason": "",
        "meeting_confirmed": False,
        "meeting_start_iso": "",
        "meeting_end_iso": "",
        "meeting_timezone": "",
        "case_reason": "",
        "preferred_time_window": "",
        "problem_description": "",
        "confidence": 0.0,
    }


def analyze_transcript(
    transcript: str,
    messages: List[Dict[str, Any]],
    current_time_utc_iso: str = "",
    business_timezone: str = "Europe/Budapest",
) -> Dict[str, Any]:
    result = _fallback_result()
    if not transcript.strip():
        return result
    if not OPENAI_API_KEY:
        logger.warning("OPENAI_API_KEY missing, using fallback transcript analysis")
        return result

    system_prompt = """
You are a call-analysis engine for an AI receptionist.
Return STRICT JSON only.

Goals:
1) Extract key information from the transcript.
2) Decide which tools should run:
   - meeting_requested: true only if caller asked for a meeting/call scheduling.
   - case_reported: true only if caller reported an issue/problem with an existing product/service.
3) Always provide a concise summary.

Important extraction rules:
- Caller email may be spoken informally, e.g. "rei aliaj at hotmail dot com".
- Normalize such spoken email into a valid email string when possible.
- If uncertain, return empty string.
- Do not invent facts not present in the transcript.

Required JSON fields:
summary, caller_name, company, contact_email, contact_phone, call_intent, meeting_requested, case_reported,
meeting_reason, meeting_confirmed, meeting_start_iso, meeting_end_iso, meeting_timezone,
case_reason, preferred_time_window, problem_description, confidence

Meeting extraction rules:
- Set meeting_confirmed=true only if the caller and assistant clearly agreed a specific meeting date+time.
- If confirmed, provide meeting_start_iso and meeting_end_iso as ISO8601 with timezone offset.
- If time is mentioned without timezone, assume business timezone.
- If no confirmed concrete time, keep meeting_confirmed=false and leave ISO fields empty.
""".strip()

    user_payload = {
        "transcript": transcript,
        "messages": messages,
        "current_time_utc_iso": current_time_utc_iso,
        "business_timezone": business_timezone,
    }

    body = {
        "model": OPENAI_MODEL,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
    }

    req = request.Request(
        OPENAI_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
        data = json.loads(raw)
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            return result
        result.update(parsed)
    except Exception:
        logger.exception("OpenAI transcript analysis failed")
        return result

    result["contact_email"] = _normalize_email(str(result.get("contact_email", "")))
    result["meeting_requested"] = bool(result.get("meeting_requested", False))
    result["meeting_confirmed"] = bool(result.get("meeting_confirmed", False))
    result["case_reported"] = bool(result.get("case_reported", False))
    return result


def decide_call_end(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Second-layer validator for call ending.
    Returns:
      {
        "end_call": 0|1
      }
    """
    fallback = {"end_call": 0}
    if not OPENAI_API_KEY:
        logger.warning("OPENAI_API_KEY missing, using fallback call-end validator")
        return fallback

    system_prompt = """
You are a strict validator helper for an Ai receptionist You help him by deciding to end the call or not.
Return STRICT JSON only.

Approve end_call=1 only when one rule is explicitly supported by evidence in transcript/messages.

Rules on how to decide. 
If the assistant has said have a nice day and the user has no more requests.
In some cases the call can be a sales call from the user or user is not being serious or asking things that are not related to our busines which is web design , digital  marketing , developement and ai agents.
In this cases return end_call=1
If uncertain, return end_call=0.


Output JSON fields:
- end_call: integer (0 or 1)
""".strip()

    body = {
        "model": CALL_END_MODEL,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
    }

    req = request.Request(
        OPENAI_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with request.urlopen(req, timeout=35) as resp:
            raw = resp.read().decode("utf-8")
        data = json.loads(raw)
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            return fallback
    except Exception:
        logger.exception("OpenAI call-end validation failed")
        return fallback

    end_call_val = parsed.get("end_call", 0)
    end_call = 1 if str(end_call_val).strip().lower() in ("1", "true", "yes") else 0
    return {"end_call": end_call}
