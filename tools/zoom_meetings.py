import base64
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List
from urllib import parse, request
from urllib.error import HTTPError

logger = logging.getLogger("zoom_meetings")

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
ZOOM_TOKEN_FILE = DATA_DIR / "zoom_tokens.json"

CLIENT_ID_ZOOM = (os.getenv("CLIENT_ID_ZOOM", "") or "").strip()
CLIENT_SECRET_ZOOM = (os.getenv("CLIENT_SECRET_ZOOM", "") or "").strip()
ZOOM_TOKEN_URL = (os.getenv("ZOOM_TOKEN_URL", "https://zoom.us/oauth/token") or "").strip()
ZOOM_OWNER_EMAIL = (os.getenv("ZOOM_OWNER_EMAIL", "aliajrei@gmail.com") or "").strip()


def _parse_iso(value: str) -> datetime:
    v = (value or "").strip()
    if not v:
        raise ValueError("empty datetime")
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    return datetime.fromisoformat(v)


def _load_token_store() -> Dict[str, Any]:
    if not ZOOM_TOKEN_FILE.exists():
        raise RuntimeError("Zoom token file not found. Complete /zoom/setup first.")
    data = json.loads(ZOOM_TOKEN_FILE.read_text(encoding="utf-8"))
    if "zoom_tokens" not in data:
        raise RuntimeError("Zoom token file is invalid")
    return data


def _save_token_store(tokens: Dict[str, Any]) -> None:
    payload = {
        "saved_at_unix": int(time.time()),
        "saved_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "zoom_tokens": tokens,
    }
    ZOOM_TOKEN_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _is_expired(store: Dict[str, Any], skew_seconds: int = 60) -> bool:
    saved_at = int(store.get("saved_at_unix", 0))
    expires_in = int(store.get("zoom_tokens", {}).get("expires_in", 0))
    if not saved_at or not expires_in:
        return True
    return time.time() >= (saved_at + expires_in - skew_seconds)


def _refresh_tokens(store: Dict[str, Any]) -> Dict[str, Any]:
    if not CLIENT_ID_ZOOM or not CLIENT_SECRET_ZOOM:
        raise RuntimeError("CLIENT_ID_ZOOM/CLIENT_SECRET_ZOOM missing")

    refresh_token = str(store.get("zoom_tokens", {}).get("refresh_token", "")).strip()
    if not refresh_token:
        raise RuntimeError("Zoom refresh_token missing in token file")

    basic = base64.b64encode(f"{CLIENT_ID_ZOOM}:{CLIENT_SECRET_ZOOM}".encode("utf-8")).decode("utf-8")
    body = parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
    ).encode("utf-8")
    req = request.Request(
        ZOOM_TOKEN_URL,
        data=body,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with request.urlopen(req, timeout=20) as resp:
        refreshed = json.loads(resp.read().decode("utf-8"))
    if "access_token" not in refreshed:
        raise RuntimeError(f"Zoom token refresh failed: {refreshed}")
    _save_token_store(refreshed)
    logger.info("[ZOOM] token refreshed successfully")
    return refreshed


def _valid_access_token() -> Dict[str, Any]:
    store = _load_token_store()
    if _is_expired(store):
        tokens = _refresh_tokens(store)
        return tokens
    return store["zoom_tokens"]


def _zoom_api_base(tokens: Dict[str, Any]) -> str:
    api_url = str(tokens.get("api_url", "")).strip()
    if api_url:
        return api_url.rstrip("/")
    return "https://api.zoom.us"


def _post_zoom_meeting(tokens: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    access_token = str(tokens.get("access_token", "")).strip()
    if not access_token:
        raise RuntimeError("Zoom access_token missing")

    url = f"{_zoom_api_base(tokens)}/v2/users/me/meetings"
    req = request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def create_zoom_meeting(
    start_iso: str,
    end_iso: str,
    topic: str,
    agenda: str,
    client_email: str = "",
    timezone_name: str = "Europe/Budapest",
) -> Dict[str, Any]:
    start_dt = _parse_iso(start_iso)
    end_dt = _parse_iso(end_iso)
    duration = int((end_dt - start_dt).total_seconds() // 60)
    if duration <= 0:
        duration = 30

    invitees: List[str] = [ZOOM_OWNER_EMAIL]
    if client_email.strip():
        invitees.append(client_email.strip())
    invitees = [e for e in dict.fromkeys(invitees) if "@" in e]

    payload = {
        "topic": topic[:180] if topic else "Client Meeting",
        "type": 2,
        "start_time": start_dt.strftime("%Y-%m-%dT%H:%M:%S"),
        "duration": duration,
        "timezone": timezone_name,
        "agenda": (agenda or "")[:1900],
        "settings": {
            "host_video": True,
            "participant_video": True,
            "join_before_host": False,
            "waiting_room": True,
            # Best-effort; some Zoom account/app setups may ignore this.
            "meeting_invitees": [{"email": e} for e in invitees],
        },
    }

    tokens = _valid_access_token()
    try:
        created = _post_zoom_meeting(tokens, payload)
    except HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8")
        except Exception:
            body = ""
        # Retry once after refresh on auth failures.
        if e.code in (401, 403):
            logger.warning("[ZOOM] create meeting failed auth-like status=%s, retrying after refresh", e.code)
            refreshed = _refresh_tokens({"zoom_tokens": tokens})
            created = _post_zoom_meeting(refreshed, payload)
        else:
            raise RuntimeError(f"Zoom create meeting failed: HTTP {e.code} {body}") from e

    result = {
        "created": True,
        "meeting_id": created.get("id"),
        "join_url": created.get("join_url"),
        "start_url": created.get("start_url"),
        "password": created.get("password"),
        "invitees": invitees,
    }
    logger.info("[ZOOM] meeting created id=%s invitees=%s", result["meeting_id"], len(invitees))
    return result
