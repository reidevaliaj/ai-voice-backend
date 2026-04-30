from __future__ import annotations

import base64
import json
import logging
from typing import Any
from urllib.parse import urlencode

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
    return await _request_telnyx_json("POST", path, body=body)


async def get_telnyx_request(path: str) -> dict[str, Any]:
    return await _request_telnyx_json("GET", path)


async def patch_telnyx_request(path: str, body: dict[str, Any]) -> dict[str, Any]:
    return await _request_telnyx_json("PATCH", path, body=body)


async def _request_telnyx_json(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout: httpx.Timeout | None = None,
) -> dict[str, Any]:
    if not TELNYX_API_KEY:
        raise RuntimeError("TELNYX_API_KEY is not configured")
    url = f"{TELNYX_API_BASE_URL.rstrip('/')}/{path.lstrip('/')}"
    if params:
        query = urlencode([(key, value) for key, value in params.items() if value not in (None, "")], doseq=True)
        if query:
            url = f"{url}?{query}"
    headers = {
        "Authorization": f"Bearer {TELNYX_API_KEY}",
        "Accept": "application/json",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    request_timeout = timeout or httpx.Timeout(15.0, connect=8.0)
    async with httpx.AsyncClient(timeout=request_timeout) as client:
        response = await client.request(method.upper(), url, headers=headers, json=body)
        if response.is_error:
            detail = _extract_telnyx_error(response)
            logger.error("[TELNYX] method=%s path=%s status=%s error=%s", method.upper(), path, response.status_code, detail)
            raise RuntimeError(detail)
        payload = response.json()
    logger.info("[TELNYX] method=%s path=%s response=%s", method.upper(), path, payload)
    return payload if isinstance(payload, dict) else {"raw": payload}


async def dial_call(body: dict[str, Any]) -> dict[str, Any]:
    return await post_telnyx_request("/calls", body)


async def get_call_details(call_control_id: str) -> dict[str, Any]:
    return await get_telnyx_request(f"/calls/{call_control_id}")


async def list_credential_connections(page_size: int = 100) -> list[dict[str, Any]]:
    payload = await _request_telnyx_json(
        "GET",
        "/credential_connections",
        params={"page[size]": page_size, "sort": "-created_at"},
        timeout=httpx.Timeout(8.0, connect=4.0),
    )
    data = payload.get("data")
    return data if isinstance(data, list) else []


async def get_credential_connection(connection_id: str) -> dict[str, Any]:
    payload = await get_telnyx_request(f"/credential_connections/{connection_id}")
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


async def get_outbound_voice_profile(profile_id: str) -> dict[str, Any]:
    payload = await get_telnyx_request(f"/outbound_voice_profiles/{profile_id}")
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


async def update_outbound_voice_profile(profile_id: str, body: dict[str, Any]) -> dict[str, Any]:
    payload = await patch_telnyx_request(f"/outbound_voice_profiles/{profile_id}", body)
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


async def ensure_outbound_recording_for_connection(
    *,
    sip_username: str,
    caller_number: str,
    recording_format: str = "mp3",
    recording_channels: str = "single",
) -> dict[str, Any]:
    normalized_username = str(sip_username or "").strip()
    normalized_number = str(caller_number or "").strip()
    if not normalized_username:
        raise RuntimeError("Missing Telnyx SIP username for outbound recording configuration")
    if not normalized_number:
        raise RuntimeError("Missing caller number for outbound recording configuration")

    connection = None
    for item in await list_credential_connections():
        if str(item.get("user_name") or "").strip() == normalized_username:
            connection = item
            break
    if connection is None:
        raise RuntimeError(f"Could not find a Telnyx credential connection for SIP username '{normalized_username}'")

    outbound = connection.get("outbound") if isinstance(connection.get("outbound"), dict) else {}
    outbound_voice_profile_id = str(outbound.get("outbound_voice_profile_id") or "").strip()
    if not outbound_voice_profile_id:
        raise RuntimeError(f"Credential connection '{normalized_username}' has no outbound voice profile")

    profile = await get_outbound_voice_profile(outbound_voice_profile_id)
    call_recording = profile.get("call_recording") if isinstance(profile.get("call_recording"), dict) else {}
    existing_numbers = [
        str(item or "").strip()
        for item in call_recording.get("call_recording_caller_phone_numbers") or []
        if str(item or "").strip()
    ]
    recording_type = str(call_recording.get("call_recording_type") or "").strip().lower()
    existing_channels = str(call_recording.get("call_recording_channels") or "").strip().lower()
    existing_format = str(call_recording.get("call_recording_format") or "").strip().lower()

    if recording_type and recording_type not in {"none", "by_caller_phone_number"}:
        return {
            "connection_id": str(connection.get("id") or ""),
            "connection_name": str(connection.get("connection_name") or ""),
            "outbound_voice_profile_id": outbound_voice_profile_id,
            "recording_enabled": True,
            "updated": False,
            "call_recording": call_recording,
        }

    desired_numbers = sorted({*existing_numbers, normalized_number})
    needs_update = not (
        recording_type == "by_caller_phone_number"
        and normalized_number in existing_numbers
        and existing_channels == str(recording_channels).strip().lower()
        and existing_format == str(recording_format).strip().lower()
    )

    updated_profile = profile
    if needs_update:
        updated_profile = await update_outbound_voice_profile(
            outbound_voice_profile_id,
            {
                "call_recording": {
                    "call_recording_type": "by_caller_phone_number",
                    "call_recording_caller_phone_numbers": desired_numbers,
                    "call_recording_channels": recording_channels,
                    "call_recording_format": recording_format,
                }
            },
        )

    return {
        "connection_id": str(connection.get("id") or ""),
        "connection_name": str(connection.get("connection_name") or ""),
        "outbound_voice_profile_id": outbound_voice_profile_id,
        "recording_enabled": True,
        "updated": needs_update,
        "call_recording": updated_profile.get("call_recording") if isinstance(updated_profile, dict) else {},
    }


async def list_call_recordings(page_size: int = 50) -> list[dict[str, Any]]:
    payload = await _request_telnyx_json(
        "GET",
        "/recordings",
        params={"page[size]": page_size, "sort": "-created_at"},
        timeout=httpx.Timeout(8.0, connect=4.0),
    )
    data = payload.get("data")
    return data if isinstance(data, list) else []


async def transfer_call(call_control_id: str, body: dict[str, Any]) -> dict[str, Any]:
    return await post_telnyx_request(f"/calls/{call_control_id}/actions/transfer", body)


async def start_recording(call_control_id: str, body: dict[str, Any]) -> dict[str, Any]:
    return await post_telnyx_request(f"/calls/{call_control_id}/actions/record_start", body)


async def hangup_call(call_control_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    return await post_telnyx_request(f"/calls/{call_control_id}/actions/hangup", body or {})
