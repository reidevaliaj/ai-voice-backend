from __future__ import annotations

from typing import Any

from services.recording_urls import build_telnyx_recording_urls, build_twilio_recording_urls


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


def incoming_event_lookup_keys(event: Any) -> set[str]:
    payload = dict(getattr(event, "payload_json", {}) or {})
    keys: set[str] = set()
    for value in (
        getattr(event, "call_sid", ""),
        getattr(event, "parent_call_sid", ""),
        payload.get("CallSid"),
        payload.get("ParentCallSid"),
        payload.get("call_control_id"),
        payload.get("parent_call_sid"),
        payload.get("call_session_id"),
        payload.get("RecordingSid"),
        payload.get("recording_id"),
    ):
        text = str(value or "").strip()
        if text:
            keys.add(text)
    return keys


def build_incoming_event_rows(events: list[Any]) -> list[dict[str, Any]]:
    recordings_by_call: dict[str, dict[str, Any]] = {}
    for event in events:
        payload = dict(getattr(event, "payload_json", {}) or {})
        provider = infer_incoming_provider(event)
        recording_urls = build_telnyx_recording_urls(payload) if provider == "telnyx" else build_twilio_recording_urls(payload)
        if not recording_urls:
            continue
        lookup_keys = incoming_event_lookup_keys(event)
        if not lookup_keys:
            continue
        for key in lookup_keys:
            existing = recordings_by_call.get(key)
            if existing is None or getattr(event, "created_at", None) >= existing.get("created_at"):
                recordings_by_call[key] = {
                    "provider": provider,
                    "recording_urls": recording_urls,
                    "created_at": getattr(event, "created_at", None),
                }

    rows: list[dict[str, Any]] = []
    for event in events:
        lookup_keys = incoming_event_lookup_keys(event)
        recording_state = {}
        for key in lookup_keys:
            if key in recordings_by_call:
                recording_state = recordings_by_call[key]
                break
        rows.append(
            {
                "event": event,
                "provider": infer_incoming_provider(event),
                "call_key": incoming_call_key(event),
                "lookup_keys": sorted(lookup_keys),
                "recording_urls": recording_state.get("recording_urls") or {},
            }
        )
    return rows
