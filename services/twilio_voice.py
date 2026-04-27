from __future__ import annotations

import logging
from typing import Any, Iterable

import httpx

from app_config import TWILIO_ACCOUNT_SID, TWILIO_API_BASE_URL, TWILIO_AUTH_TOKEN

logger = logging.getLogger("twilio")


def _twilio_auth() -> tuple[str, str]:
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        raise RuntimeError("TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN is not configured")
    return TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN


def _extract_twilio_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except Exception:
        text = (response.text or "").strip()
        return text or f"HTTP {response.status_code}"

    if not isinstance(payload, dict):
        return str(payload)

    message = str(payload.get("message") or "").strip()
    code = str(payload.get("code") or "").strip()
    more_info = str(payload.get("more_info") or "").strip()
    parts = [part for part in [message, f"Twilio code {code}" if code else "", more_info] if part]
    return " | ".join(parts) if parts else str(payload)


async def _post_twilio_request(path: str, data: dict[str, Any] | Iterable[tuple[str, Any]]) -> dict[str, Any]:
    account_sid, auth_token = _twilio_auth()
    url = f"{TWILIO_API_BASE_URL.rstrip('/')}/Accounts/{account_sid}/{path.lstrip('/')}"
    timeout = httpx.Timeout(15.0, connect=8.0)
    headers = {"Accept": "application/json"}
    async with httpx.AsyncClient(timeout=timeout, auth=(account_sid, auth_token)) as client:
        response = await client.post(url, headers=headers, data=data)
        if response.is_error:
            detail = _extract_twilio_error(response)
            logger.error("[TWILIO] path=%s status=%s error=%s", path, response.status_code, detail)
            raise RuntimeError(detail)
        payload = response.json()
    logger.info("[TWILIO] path=%s response=%s", path, payload)
    return payload if isinstance(payload, dict) else {"raw": payload}


async def dial_call(*, to: str, from_number: str, url: str, status_callback: str) -> dict[str, Any]:
    data: list[tuple[str, Any]] = [
        ("To", to),
        ("From", from_number),
        ("Url", url),
        ("Method", "POST"),
        ("StatusCallback", status_callback),
        ("StatusCallbackMethod", "POST"),
        ("StatusCallbackEvent", "initiated"),
        ("StatusCallbackEvent", "ringing"),
        ("StatusCallbackEvent", "answered"),
        ("StatusCallbackEvent", "completed"),
    ]
    return await _post_twilio_request("Calls.json", data)


async def hangup_call(call_sid: str) -> dict[str, Any]:
    return await _post_twilio_request(f"Calls/{call_sid}.json", {"Status": "completed"})
