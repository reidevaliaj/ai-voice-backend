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
ALLOW_COMPLETION_AUTO_END = os.getenv("ALLOW_COMPLETION_AUTO_END", "false").strip().lower() == "true"
REQUIRE_EXPLICIT_END_CONFIRMATION = os.getenv("REQUIRE_EXPLICIT_END_CONFIRMATION", "false").strip().lower() == "true"

EXPLICIT_END_PATTERNS = (
    r"\b(that'?s all|thats all|goodbye|bye|have a good day|nothing else|no thank you|no thanks)\b",
    r"\b(arrivederci|ciao|basta cos[ìi]|tutto qui|non serve altro|grazie arrivederci)\b",
    r"\b(auf wiedersehen|tsch[uü]ss|das ist alles|sonst nichts|nein danke)\b",
    r"\b(adios|adi[oó]s|eso es todo|nada m[aá]s|no gracias)\b",
)


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


def _user_lines_from_transcript(transcript: str) -> list[str]:
    lines: list[str] = []
    for raw_line in (transcript or "").splitlines():
        line = raw_line.strip()
        if line.lower().startswith("user:"):
            lines.append(line.split(":", 1)[1].strip().lower())
    return lines


def _has_explicit_end_phrase(transcript: str) -> bool:
    user_lines = _user_lines_from_transcript(transcript)
    if not user_lines:
        return False
    combined = " ".join(user_lines)
    return any(re.search(pattern, combined, flags=re.IGNORECASE) for pattern in EXPLICIT_END_PATTERNS)


def analyze_transcript(
    transcript: str,
    messages: List[Dict[str, Any]],
    current_time_utc_iso: str = "",
    business_timezone: str = "Europe/Budapest",
    business_context: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    result = _fallback_result()
    if not transcript.strip() or not OPENAI_API_KEY:
        return result

    system_prompt = """
You are a call-analysis engine for a multi-tenant AI receptionist.
Return STRICT JSON only.

Goals:
1) Extract key information from the transcript.
2) Decide which tools should run.
3) Respect the tenant business context when deciding whether the call was a sales lead, support issue, vendor/sales solicitation, or unrelated.
4) Always provide a concise summary.

Extraction rules:
- Caller email may be spoken informally, e.g. "rei aliaj at hotmail dot com".
- Normalize such spoken email into a valid email string when possible.
- If uncertain, return empty string.
- Do not invent facts not present in the transcript.

Meeting rules:
- Set meeting_confirmed=true only if the caller and assistant clearly agreed a specific meeting date+time.
- If confirmed, provide meeting_start_iso and meeting_end_iso as ISO8601 with timezone offset.
- If time is mentioned without timezone, assume the tenant business timezone.
- If no confirmed concrete time, keep meeting_confirmed=false and leave ISO fields empty.

Required JSON fields:
summary, caller_name, company, contact_email, contact_phone, call_intent, meeting_requested, case_reported,
meeting_reason, meeting_confirmed, meeting_start_iso, meeting_end_iso, meeting_timezone,
case_reason, preferred_time_window, problem_description, confidence
""".strip()

    user_payload = {
        "transcript": transcript,
        "messages": messages,
        "current_time_utc_iso": current_time_utc_iso,
        "business_timezone": business_timezone,
        "business_context": business_context or {},
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
        if isinstance(parsed, dict):
            result.update(parsed)
    except Exception:
        logger.exception("OpenAI transcript analysis failed")
        return result

    result["contact_email"] = _normalize_email(str(result.get("contact_email", "")))
    result["meeting_requested"] = bool(result.get("meeting_requested", False))
    result["meeting_confirmed"] = bool(result.get("meeting_confirmed", False))
    result["case_reported"] = bool(result.get("case_reported", False))
    return result


def decide_call_end(payload: Dict[str, Any], business_context: Dict[str, Any] | None = None) -> Dict[str, Any]:
    fallback = {"end_call": 0}
    transcript = str(payload.get("transcript", "") or "")
    explicit_end_detected = _has_explicit_end_phrase(transcript)

    if REQUIRE_EXPLICIT_END_CONFIRMATION and not explicit_end_detected:
        return fallback

    if not OPENAI_API_KEY:
        return {"end_call": 1} if explicit_end_detected and not REQUIRE_EXPLICIT_END_CONFIRMATION else fallback

    system_prompt = """
You are a strict validator helper for a multi-tenant AI receptionist.
Return STRICT JSON only.

Approve end_call=1 only when one rule is explicitly supported by the transcript/messages.
Use the tenant business context to judge whether the caller is asking for relevant business help or something unrelated/vendor-like.
If uncertain, return end_call=0.

If the caller did not explicitly signal that the call should end, be conservative and return end_call=0.

Output JSON fields:
- end_call: integer (0 or 1)
""".strip()

    body = {
        "model": CALL_END_MODEL,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps({"payload": payload, "business_context": business_context or {}}, ensure_ascii=False)},
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
    if REQUIRE_EXPLICIT_END_CONFIRMATION:
        end_call = 1 if explicit_end_detected and end_call == 1 else 0
    elif not ALLOW_COMPLETION_AUTO_END and not explicit_end_detected:
        end_call = 0
    return {"end_call": end_call}
