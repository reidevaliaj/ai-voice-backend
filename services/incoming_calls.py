from __future__ import annotations

from typing import Any


def infer_incoming_provider(event: Any) -> str:
    event_type = str(getattr(event, "event_type", "") or "").strip().lower()
    payload = dict(getattr(event, "payload_json", {}) or {})
    callback_source = str(payload.get("CallbackSource") or payload.get("callback_source") or "").strip().lower()
    if event_type.startswith("telnyx_") or callback_source == "telnyx_voice_api":
        return "telnyx"
    return "twilio"


def incoming_call_key(event: Any) -> str:
    parent = str(getattr(event, "parent_call_sid", "") or "").strip()
    call_sid = str(getattr(event, "call_sid", "") or "").strip()
    return parent or call_sid


def _twilio_recording_urls(payload: dict[str, Any]) -> dict[str, str]:
    base_url = str(payload.get("RecordingUrl") or payload.get("recording_url") or "").strip()
    if not base_url:
        return {}
    if base_url.endswith(".mp3"):
        mp3_url = base_url
        wav_url = base_url[:-4] + ".wav"
    elif base_url.endswith(".wav"):
        wav_url = base_url
        mp3_url = base_url[:-4] + ".mp3"
    else:
        mp3_url = f"{base_url}.mp3"
        wav_url = f"{base_url}.wav"
    return {"mp3": mp3_url, "wav": wav_url}


def _telnyx_recording_urls(payload: dict[str, Any]) -> dict[str, str]:
    for key in ("public_recording_urls", "recording_urls", "download_urls"):
        value = payload.get(key)
        if isinstance(value, dict):
            return {str(name): str(url) for name, url in value.items() if str(url or "").strip()}
    return {}


def build_incoming_event_rows(events: list[Any]) -> list[dict[str, Any]]:
    recordings_by_call: dict[str, dict[str, Any]] = {}
    for event in events:
        key = incoming_call_key(event)
        if not key:
            continue
        payload = dict(getattr(event, "payload_json", {}) or {})
        provider = infer_incoming_provider(event)
        recording_urls = _telnyx_recording_urls(payload) if provider == "telnyx" else _twilio_recording_urls(payload)
        if not recording_urls:
            continue
        existing = recordings_by_call.get(key)
        if existing is None or getattr(event, "created_at", None) >= existing.get("created_at"):
            recordings_by_call[key] = {
                "provider": provider,
                "recording_urls": recording_urls,
                "created_at": getattr(event, "created_at", None),
            }

    rows: list[dict[str, Any]] = []
    for event in events:
        key = incoming_call_key(event)
        recording_state = recordings_by_call.get(key) or {}
        rows.append(
            {
                "event": event,
                "provider": infer_incoming_provider(event),
                "call_key": key,
                "recording_urls": recording_state.get("recording_urls") or {},
            }
        )
    return rows
