from __future__ import annotations

from typing import Any


def build_twilio_recording_urls(payload: dict[str, Any]) -> dict[str, str]:
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


def build_telnyx_recording_urls(payload: dict[str, Any]) -> dict[str, str]:
    for key in ("public_recording_urls", "recording_urls", "download_urls"):
        value = payload.get(key)
        if isinstance(value, dict):
            return {str(name): str(url) for name, url in value.items() if str(url or "").strip()}
    return {}
