from __future__ import annotations

import base64
import json
import logging
from typing import Any

import httpx

from app_config import TELNYX_API_BASE_URL, TELNYX_API_KEY

logger = logging.getLogger("telnyx")


def _extract_telnyx_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except Exception:
        text = (response.text or "").strip()
        return text or f"HTTP {response.status_code}"

    if not isinstance(payload, dict):
        return str(payload)

    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0] if isinstance(errors[0], dict) else {}
        detail = str(first.get("detail") or first.get("title") or "").strip()
        code = str(first.get("code") or "").strip()
        telnyx_error = payload.get("telnyx_error") if isinstance(payload.get("telnyx_error"), dict) else {}
        telnyx_code = str(telnyx_error.get("error_code") or "").strip()
        parts = [part for part in [detail, f"Telnyx code {telnyx_code}" if telnyx_code else "", f"Error {code}" if code else ""] if part]
        if parts:
            return " | ".join(parts)

    return str(payload)


def encode_client_state(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def decode_client_state(value: str | None) -> dict[str, Any]:
    text = str(value or "").strip()
    if not text:
        return {}
    try:
        decoded = base64.b64decode(text).decode("utf-8")
        payload = json.loads(decoded)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def telnyx_command_id(prefix: str, unique_value: str) -> str:
    base = str(unique_value or "").replace(":", "_")
    return f"{prefix}-{base}"


def flatten_voice_event(wrapper: dict[str, Any]) -> dict[str, Any]:
    data = wrapper.get("data") if isinstance(wrapper.get("data"), dict) else {}
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
    meta = wrapper.get("meta") if isinstance(wrapper.get("meta"), dict) else {}
    event_type = str(data.get("event_type") or "")
    occurred_at = str(data.get("occurred_at") or payload.get("timestamp") or "")
    normalized = {
        **payload,
        "CallSid": str(payload.get("call_control_id") or ""),
        "ParentCallSid": str(payload.get("parent_call_sid") or ""),
        "From": str(payload.get("from") or ""),
        "To": str(payload.get("to") or ""),
        "CallStatus": str(payload.get("call_status") or payload.get("state") or ""),
        "Timestamp": occurred_at,
        "CallbackSource": "telnyx_voice_api",
        "event_type": event_type,
        "event_id": str(data.get("id") or ""),
        "occurred_at": occurred_at,
        "direction": str(payload.get("direction") or ""),
        "state": str(payload.get("state") or ""),
        "meta_attempt": meta.get("attempt"),
        "raw_wrapper": wrapper,
    }
    return normalized


def is_voice_event(wrapper: dict[str, Any]) -> bool:
    data = wrapper.get("data")
    return isinstance(data, dict) and isinstance(data.get("payload"), dict) and bool(data.get("event_type"))


async def post_telnyx_request(path: str, body: dict[str, Any]) -> dict[str, Any]:
    if not TELNYX_API_KEY:
        raise RuntimeError("TELNYX_API_KEY is not configured")
    url = f"{TELNYX_API_BASE_URL.rstrip('/')}/{path.lstrip('/')}"
    headers = {
        "Authorization": f"Bearer {TELNYX_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    timeout = httpx.Timeout(15.0, connect=8.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, headers=headers, json=body)
        if response.is_error:
            detail = _extract_telnyx_error(response)
            logger.error("[TELNYX] path=%s status=%s error=%s", path, response.status_code, detail)
            raise RuntimeError(detail)
        payload = response.json()
    logger.info("[TELNYX] path=%s response=%s", path, payload)
    return payload if isinstance(payload, dict) else {"raw": payload}


async def dial_call(body: dict[str, Any]) -> dict[str, Any]:
    return await post_telnyx_request("/calls", body)


async def transfer_call(call_control_id: str, body: dict[str, Any]) -> dict[str, Any]:
    return await post_telnyx_request(f"/calls/{call_control_id}/actions/transfer", body)


async def start_recording(call_control_id: str, body: dict[str, Any]) -> dict[str, Any]:
    return await post_telnyx_request(f"/calls/{call_control_id}/actions/record_start", body)


async def hangup_call(call_control_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    return await post_telnyx_request(f"/calls/{call_control_id}/actions/hangup", body or {})
