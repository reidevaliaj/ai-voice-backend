import base64
import json
import logging
import os
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qsl, urlunparse

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter()
logger = logging.getLogger("zoom_oauth")

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
ZOOM_TOKEN_FILE = DATA_DIR / "zoom_tokens.json"


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()


def _mask_token(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 10:
        return "*" * len(value)
    return value[:5] + "..." + value[-5:]


def _redirect_uri_from_request(request: Request) -> str:
    configured = _env("ZOOM_REDIRECT_URI", "")
    if configured:
        return configured
    return str(request.url_for("zoom_oauth_callback"))


def _build_authorize_url(request: Request) -> str:
    base = _env("AUTHORIZATION_URL_ZOOM", "https://zoom.us/oauth/authorize")
    client_id = _env("CLIENT_ID_ZOOM", "")
    if not client_id:
        raise HTTPException(status_code=500, detail="CLIENT_ID_ZOOM is missing")

    params = dict(parse_qsl(urlparse(base).query))
    params.update({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": _redirect_uri_from_request(request),
    })
    parsed = urlparse(base)
    return urlunparse(parsed._replace(query=urlencode(params)))


def _save_tokens(payload: dict) -> None:
    data = {
        "saved_at_unix": int(time.time()),
        "saved_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "zoom_tokens": payload,
    }
    ZOOM_TOKEN_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _token_file_preview() -> dict:
    if not ZOOM_TOKEN_FILE.exists():
        return {"exists": False}
    try:
        raw = json.loads(ZOOM_TOKEN_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"exists": True, "parse_error": True}

    tokens = raw.get("zoom_tokens", {})
    return {
        "exists": True,
        "saved_at_iso": raw.get("saved_at_iso", ""),
        "token_type": tokens.get("token_type", ""),
        "scope": tokens.get("scope", ""),
        "expires_in": tokens.get("expires_in", ""),
        "access_token_masked": _mask_token(str(tokens.get("access_token", ""))),
        "refresh_token_masked": _mask_token(str(tokens.get("refresh_token", ""))),
    }


@router.get("/zoom/setup", response_class=HTMLResponse)
async def zoom_setup_page(request: Request):
    preview = _token_file_preview()
    authorize_url = _build_authorize_url(request)

    token_status = "No tokens saved yet."
    if preview.get("exists"):
        token_status = (
            f"Saved at: {preview.get('saved_at_iso', '')}<br/>"
            f"Access: {preview.get('access_token_masked', '')}<br/>"
            f"Refresh: {preview.get('refresh_token_masked', '')}<br/>"
            f"Scope: {preview.get('scope', '')}<br/>"
            f"Expires in: {preview.get('expires_in', '')}"
        )

    html = f"""
    <html>
      <head><title>Zoom OAuth Setup</title></head>
      <body style="font-family: Arial, sans-serif; margin: 24px;">
        <h2>Zoom OAuth Setup</h2>
        <p>Use this once to authorize and save access/refresh tokens locally.</p>
        <p><a href="{authorize_url}"><button style="padding: 10px 16px;">Connect Zoom</button></a></p>
        <h3>Token File Status</h3>
        <p>{token_status}</p>
      </body>
    </html>
    """
    return HTMLResponse(content=html)


@router.get("/zoom/oauth/start")
async def zoom_oauth_start(request: Request):
    return RedirectResponse(url=_build_authorize_url(request), status_code=302)


@router.get("/zoom/oauth/callback")
async def zoom_oauth_callback(request: Request, code: str = ""):
    if not code:
        raise HTTPException(status_code=400, detail="Missing OAuth code")

    client_id = _env("CLIENT_ID_ZOOM", "")
    client_secret = _env("CLIENT_SECRET_ZOOM", "")
    if not client_id or not client_secret:
        raise HTTPException(status_code=500, detail="Zoom client credentials missing")

    token_url = _env("ZOOM_TOKEN_URL", "https://zoom.us/oauth/token")
    redirect_uri = _redirect_uri_from_request(request)
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("utf-8")

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }
    headers = {
        "Authorization": f"Basic {basic}",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(token_url, data=data, headers=headers)

    if resp.status_code >= 400:
        logger.error("Zoom token exchange failed status=%s body=%s", resp.status_code, resp.text)
        raise HTTPException(status_code=500, detail=f"Zoom token exchange failed: {resp.text}")

    payload = resp.json()
    _save_tokens(payload)

    return HTMLResponse(
        content=(
            "<h3>Zoom OAuth successful</h3>"
            f"<p>Tokens saved to: <code>{ZOOM_TOKEN_FILE}</code></p>"
            "<p>You can now proceed with Zoom meeting creation logic.</p>"
        )
    )
