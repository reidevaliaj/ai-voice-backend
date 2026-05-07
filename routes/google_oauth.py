import logging
import secrets
import time
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app_config import GOOGLE_CALENDAR_ID, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, PUBLIC_BASE_URL
from db import get_db
from models import AdminUser
from services.tenants import get_active_config, get_integration_payload, get_tenant_by_slug, upsert_integration

router = APIRouter()
logger = logging.getLogger("google_oauth")

GOOGLE_OAUTH_STATE_SESSION_KEY = "google_oauth_state"
GOOGLE_OAUTH_SCOPE = "https://www.googleapis.com/auth/calendar"
GOOGLE_OAUTH_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"


def _current_admin(request: Request, session: Session) -> AdminUser | None:
    admin_id = request.session.get("admin_user_id")
    if not admin_id:
        return None
    return session.get(AdminUser, admin_id)


def _require_admin(request: Request, session: Session) -> AdminUser:
    admin = _current_admin(request, session)
    if admin is None or not admin.is_active:
        raise HTTPException(status_code=303, headers={"Location": "/admin/login"})
    return admin


def _redirect_uri() -> str:
    return f"{str(PUBLIC_BASE_URL).rstrip('/')}/google/oauth/callback"


def _build_authorize_url(*, state: str) -> str:
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise HTTPException(status_code=500, detail="Google OAuth client credentials are missing")
    params = dict(parse_qsl(urlparse(GOOGLE_OAUTH_AUTHORIZE_URL).query))
    params.update(
        {
            "client_id": GOOGLE_CLIENT_ID,
            "redirect_uri": _redirect_uri(),
            "response_type": "code",
            "scope": GOOGLE_OAUTH_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
    )
    parsed = urlparse(GOOGLE_OAUTH_AUTHORIZE_URL)
    return urlunparse(parsed._replace(query=urlencode(params)))


def _calendar_settings_for_tenant(tenant, active_config, current_settings: dict[str, object] | None) -> dict[str, object]:
    settings = dict(current_settings or {})
    if active_config is not None:
        settings["business_timezone"] = active_config.timezone
        settings["business_hours"] = active_config.business_hours
        settings["business_days"] = active_config.business_days
    return settings


def _calendar_credentials_for_storage(
    *,
    token_payload: dict[str, object],
    existing_credentials: dict[str, object] | None,
) -> dict[str, object]:
    current = dict(existing_credentials or {})
    refresh_token = str(token_payload.get("refresh_token") or current.get("refresh_token") or "").strip()
    calendar_id = str(current.get("calendar_id") or GOOGLE_CALENDAR_ID or "primary").strip() or "primary"
    if not refresh_token:
        raise RuntimeError("Google OAuth completed but no refresh token was returned")
    return {
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "refresh_token": refresh_token,
        "calendar_id": calendar_id,
        "scope": str(token_payload.get("scope") or current.get("scope") or GOOGLE_OAUTH_SCOPE),
        "token_type": str(token_payload.get("token_type") or current.get("token_type") or "Bearer"),
        "connected_via_oauth": True,
        "saved_at_unix": int(time.time()),
    }


@router.get("/google/oauth/start")
async def google_oauth_start(request: Request, tenant: str = "", db: Session = Depends(get_db)):
    admin = _require_admin(request, db)
    tenant_slug = str(tenant or "").strip()
    tenant_obj = get_tenant_by_slug(db, tenant_slug)
    if tenant_obj is None:
        raise HTTPException(status_code=404, detail="Tenant not found")

    state = secrets.token_urlsafe(24)
    request.session[GOOGLE_OAUTH_STATE_SESSION_KEY] = {
        "state": state,
        "tenant_slug": tenant_obj.slug,
        "admin_user_id": admin.id,
        "created_at": int(time.time()),
    }
    return RedirectResponse(url=_build_authorize_url(state=state), status_code=302)


@router.get("/google/oauth/callback", response_class=HTMLResponse)
async def google_oauth_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
    db: Session = Depends(get_db),
):
    if error:
        description = str(error_description or error).strip()
        return HTMLResponse(
            content=(
                "<h3>Google Calendar connection was not completed.</h3>"
                f"<p>{description}</p>"
                "<p>Return to the tenant dashboard and try again.</p>"
            ),
            status_code=400,
        )

    oauth_state = dict(request.session.get(GOOGLE_OAUTH_STATE_SESSION_KEY) or {})
    request.session.pop(GOOGLE_OAUTH_STATE_SESSION_KEY, None)
    expected_state = str(oauth_state.get("state") or "").strip()
    tenant_slug = str(oauth_state.get("tenant_slug") or "").strip()

    if not code:
        raise HTTPException(status_code=400, detail="Missing Google OAuth code")
    if not expected_state or state.strip() != expected_state:
        raise HTTPException(status_code=400, detail="Invalid Google OAuth state")

    tenant = get_tenant_by_slug(db, tenant_slug)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise HTTPException(status_code=500, detail="Google OAuth client credentials are missing")

    data = {
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": _redirect_uri(),
        "grant_type": "authorization_code",
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(GOOGLE_OAUTH_TOKEN_URL, data=data, headers=headers)
    if response.status_code >= 400:
        logger.error("Google token exchange failed status=%s body=%s", response.status_code, response.text)
        raise HTTPException(status_code=500, detail=f"Google token exchange failed: {response.text}")

    token_payload = response.json()
    current = get_integration_payload(db, tenant.id, "google_calendar")
    active_config = get_active_config(db, tenant.id)
    credentials = _calendar_credentials_for_storage(
        token_payload=token_payload,
        existing_credentials=current.get("credentials") or {},
    )
    settings = _calendar_settings_for_tenant(
        tenant=tenant,
        active_config=active_config,
        current_settings=current.get("settings") or {},
    )
    upsert_integration(
        db,
        tenant,
        "google_calendar",
        credentials=credentials,
        settings=settings,
        status="active",
        last_error="",
        mark_validated=True,
    )

    return HTMLResponse(
        content=(
            f"<h3>Google Calendar connected for {tenant.display_name}</h3>"
            f"<p>The calendar connection was saved to the tenant integration database.</p>"
            f"<p>Return to <a href='/admin/tenants/{tenant.slug}'>tenant dashboard</a>.</p>"
        )
    )
