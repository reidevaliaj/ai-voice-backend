import json
import time
from typing import Any
from urllib import parse, request

from app_config import CARTESIA_API_KEY, CARTESIA_VERSION

CARTESIA_API_BASE = "https://api.cartesia.ai"
VOICE_CACHE_TTL_SECONDS = 900

_VOICE_CACHE: dict[str, dict[str, Any]] = {}


def _headers() -> dict[str, str]:
    if not CARTESIA_API_KEY:
        raise RuntimeError("CARTESIA_API_KEY is not configured on the backend")
    return {
        "Authorization": f"Bearer {CARTESIA_API_KEY}",
        "Cartesia-Version": CARTESIA_VERSION,
    }


def _request_json(path: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
    query_string = f"?{parse.urlencode(query, doseq=True)}" if query else ""
    req = request.Request(
        f"{CARTESIA_API_BASE}{path}{query_string}",
        headers=_headers(),
        method="GET",
    )
    with request.urlopen(req, timeout=8) as resp:
        raw = resp.read().decode("utf-8")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError("Unexpected Cartesia response")
    return parsed


def _voice_payload(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(item.get("id") or ""),
        "name": str(item.get("name") or "Unnamed voice"),
        "description": str(item.get("description") or "").strip(),
        "language": str(item.get("language") or "").strip(),
        "gender": str(item.get("gender") or "").strip(),
        "is_owner": bool(item.get("is_owner", False)),
        "preview_file_url": item.get("preview_file_url"),
    }


def _fetch_voice_list(language: str) -> list[dict[str, Any]]:
    payload = _request_json(
        "/voices",
        query={
            "language": language,
            "limit": 100,
            "expand[]": "preview_file_url",
        },
    )
    voices = payload.get("data") or []
    if not isinstance(voices, list):
        return []
    normalized = [_voice_payload(item) for item in voices if isinstance(item, dict) and item.get("id")]
    normalized.sort(key=lambda item: (not item.get("is_owner", False), str(item.get("name") or "").lower()))
    return normalized


def get_cartesia_voice_options(language: str, *, selected_voice: str = "") -> list[dict[str, Any]]:
    language_code = (language or "").strip().lower() or "en"
    now = time.time()
    cached = _VOICE_CACHE.get(language_code)
    voices: list[dict[str, Any]]
    if cached and now - float(cached.get("ts", 0)) < VOICE_CACHE_TTL_SECONDS:
        voices = list(cached.get("voices") or [])
    else:
        voices = _fetch_voice_list(language_code)
        _VOICE_CACHE[language_code] = {"ts": now, "voices": voices}

    selected = (selected_voice or "").strip()
    if selected and not any(item["id"] == selected for item in voices):
        try:
            current_voice = _request_json(f"/voices/{selected}", query={"expand[]": "preview_file_url"})
            voices = [_voice_payload(current_voice), *voices]
        except Exception:
            voices = [
                {
                    "id": selected,
                    "name": f"Current selection ({selected[:8]})",
                    "description": "Saved on the tenant config but not currently present in the filtered voice list.",
                    "language": language_code,
                    "gender": "",
                    "is_owner": False,
                    "preview_file_url": None,
                },
                *voices,
            ]
    return voices
