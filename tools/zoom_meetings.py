import base64
import json
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Tuple
from urllib import parse, request
from urllib.error import HTTPError

logger = logging.getLogger("zoom_meetings")


class ZoomContext:
    def __init__(self, credentials: dict[str, Any], settings: dict[str, Any] | None = None):
        creds = dict(credentials or {})
        options = dict(settings or {})
        self.client_id = str(creds.get("client_id") or "").strip()
        self.client_secret = str(creds.get("client_secret") or "").strip()
        self.token_url = str(creds.get("token_url") or "https://zoom.us/oauth/token").strip()
        self.access_token = str(creds.get("access_token") or "").strip()
        self.refresh_token = str(creds.get("refresh_token") or "").strip()
        self.api_url = str(creds.get("api_url") or "https://api.zoom.us").strip().rstrip("/")
        self.expires_in = int(creds.get("expires_in") or 0)
        self.saved_at_unix = int(creds.get("saved_at_unix") or 0)
        self.owner_email = str(options.get("owner_email") or "").strip()

    def export_credentials(self, tokens: dict[str, Any] | None = None) -> dict[str, Any]:
        current = dict(tokens or {})
        if not current:
            current = {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "token_url": self.token_url,
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "api_url": self.api_url,
                "expires_in": self.expires_in,
                "saved_at_unix": self.saved_at_unix,
            }
        current.setdefault("client_id", self.client_id)
        current.setdefault("client_secret", self.client_secret)
        current.setdefault("token_url", self.token_url)
        current.setdefault("api_url", self.api_url)
        return current


def _parse_iso(value: str) -> datetime:
    v = (value or "").strip()
    if not v:
        raise ValueError("empty datetime")
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    return datetime.fromisoformat(v)


def _is_expired(context: ZoomContext, skew_seconds: int = 60) -> bool:
    if not context.access_token or not context.saved_at_unix or not context.expires_in:
        return True
    return time.time() >= (context.saved_at_unix + context.expires_in - skew_seconds)


def _refresh_tokens(context: ZoomContext) -> dict[str, Any]:
    if not context.client_id or not context.client_secret:
        raise RuntimeError("Zoom client_id/client_secret missing")
    if not context.refresh_token:
        raise RuntimeError("Zoom refresh_token missing")

    basic = base64.b64encode(f"{context.client_id}:{context.client_secret}".encode("utf-8")).decode("utf-8")
    body = parse.urlencode({"grant_type": "refresh_token", "refresh_token": context.refresh_token}).encode("utf-8")
    req = request.Request(
        context.token_url,
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
    refreshed["client_id"] = context.client_id
    refreshed["client_secret"] = context.client_secret
    refreshed["token_url"] = context.token_url
    refreshed.setdefault("api_url", context.api_url)
    refreshed["saved_at_unix"] = int(time.time())
    return refreshed


def _valid_access_token(context: ZoomContext) -> Tuple[dict[str, Any], dict[str, Any] | None]:
    if _is_expired(context):
        refreshed = _refresh_tokens(context)
        return refreshed, refreshed
    return context.export_credentials(), None


def _post_zoom_meeting(access_token: str, api_url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    req = request.Request(
        f"{api_url.rstrip('/')}/v2/users/me/meetings",
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
    context: ZoomContext | None = None,
) -> Dict[str, Any]:
    if context is None:
        raise RuntimeError("Zoom context is required")
    start_dt = _parse_iso(start_iso)
    end_dt = _parse_iso(end_iso)
    duration = int((end_dt - start_dt).total_seconds() // 60)
    if duration <= 0:
        duration = 30

    invitees: List[str] = [context.owner_email] if context.owner_email else []
    if client_email.strip():
        invitees.append(client_email.strip())
    invitees = [email for email in dict.fromkeys(invitees) if "@" in email]

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
            "meeting_invitees": [{"email": email} for email in invitees],
        },
    }

    tokens, updated_credentials = _valid_access_token(context)
    try:
        created = _post_zoom_meeting(str(tokens.get("access_token") or ""), str(tokens.get("api_url") or context.api_url), payload)
    except HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8")
        except Exception:
            body = ""
        if exc.code in (401, 403):
            refreshed = _refresh_tokens(context)
            updated_credentials = refreshed
            created = _post_zoom_meeting(str(refreshed.get("access_token") or ""), str(refreshed.get("api_url") or context.api_url), payload)
        else:
            raise RuntimeError(f"Zoom create meeting failed: HTTP {exc.code} {body}") from exc

    return {
        "created": True,
        "meeting_id": created.get("id"),
        "join_url": created.get("join_url"),
        "start_url": created.get("start_url"),
        "password": created.get("password"),
        "invitees": invitees,
        "updated_credentials": updated_credentials,
    }


def validate_zoom_context(context: ZoomContext) -> Dict[str, Any]:
    tokens, updated = _valid_access_token(context)
    access_token = str(tokens.get("access_token") or "")
    if not access_token:
        raise RuntimeError("Zoom access token missing")
    req = request.Request(
        f"{str(tokens.get('api_url') or context.api_url).rstrip('/')}/v2/users/me",
        headers={"Authorization": f"Bearer {access_token}"},
        method="GET",
    )
    with request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return {
        "ok": True,
        "email": data.get("email"),
        "display_name": data.get("display_name"),
        "updated_credentials": updated,
    }
