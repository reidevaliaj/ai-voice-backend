from __future__ import annotations

import logging
from typing import Any, Iterable
from urllib.parse import urlencode
from urllib.parse import urlparse, urlunparse

import httpx

from app_config import TWILIO_ACCOUNT_SID, TWILIO_API_BASE_URL, TWILIO_AUTH_TOKEN

logger = logging.getLogger("twilio")


def _twilio_auth() -> tuple[str, str]:
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        raise RuntimeError("TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN is not configured")
    return TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN


def normalize_recording_media_url(recording_url: str, format_hint: str = "") -> str:
    account_sid, _ = _twilio_auth()
    raw_url = str(recording_url or "").strip()
    if not raw_url:
        raise RuntimeError("Missing Twilio recording URL")

    parsed = urlparse(raw_url)
    if parsed.scheme != "https":
        raise RuntimeError("Twilio recording URL must use https")
    hostname = (parsed.hostname or "").lower()
    if not hostname.endswith("twilio.com"):
        raise RuntimeError("Twilio recording URL host is not allowed")

    expected_fragment = f"/Accounts/{account_sid}/Recordings/"
    if expected_fragment not in parsed.path:
        raise RuntimeError("Twilio recording URL does not belong to the configured account")

    normalized_path = parsed.path
    if format_hint == "mp3" and not normalized_path.endswith(".mp3"):
        normalized_path = normalized_path[:-4] + ".mp3" if normalized_path.endswith(".wav") else f"{normalized_path}.mp3"
    elif format_hint == "wav" and not normalized_path.endswith(".wav"):
        normalized_path = normalized_path[:-4] + ".wav" if normalized_path.endswith(".mp3") else f"{normalized_path}.wav"

    return urlunparse((parsed.scheme, parsed.netloc, normalized_path, "", parsed.query, ""))


async def fetch_recording_media(recording_url: str, format_hint: str = "") -> tuple[bytes, str]:
    account_sid, auth_token = _twilio_auth()
    url = normalize_recording_media_url(recording_url, format_hint=format_hint)
    timeout = httpx.Timeout(30.0, connect=8.0)
    async with httpx.AsyncClient(timeout=timeout, auth=(account_sid, auth_token), follow_redirects=True) as client:
        response = await client.get(url, headers={"Accept": "*/*"})
        if response.is_error:
            detail = _extract_twilio_error(response)
            logger.error("[TWILIO] recording_media status=%s url=%s error=%s", response.status_code, url, detail)
            raise RuntimeError(detail)
        content_type = str(response.headers.get("content-type") or "").strip() or "application/octet-stream"
        return response.content, content_type


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
    if code == "21216":
        parts.append(
            "Twilio blocked the outbound call before connecting it. "
            "For +1 destinations this often means the account needs a valid Primary Customer Profile in Trust Hub, "
            "or Twilio flagged the destination/range as high risk."
        )
    return " | ".join(parts) if parts else str(payload)


async def _post_twilio_request(path: str, data: dict[str, Any] | Iterable[tuple[str, Any]]) -> dict[str, Any]:
    account_sid, auth_token = _twilio_auth()
    url = f"{TWILIO_API_BASE_URL.rstrip('/')}/Accounts/{account_sid}/{path.lstrip('/')}"
    timeout = httpx.Timeout(15.0, connect=8.0)
    items = list(data.items()) if isinstance(data, dict) else list(data)
    body = urlencode(items, doseq=True)
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    async with httpx.AsyncClient(timeout=timeout, auth=(account_sid, auth_token)) as client:
        response = await client.post(url, headers=headers, content=body)
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
