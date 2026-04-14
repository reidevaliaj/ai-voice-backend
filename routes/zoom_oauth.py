import base64
import json
import logging
import time
from urllib.parse import urlencode, urlparse, parse_qsl, urlunparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app_config import PUBLIC_BASE_URL, ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET, ZOOM_TOKEN_URL
from db import get_db
from services.tenants import get_tenant_by_slug, get_integration_payload, upsert_integration

router = APIRouter()
logger = logging.getLogger("zoom_oauth")


def _redirect_uri_from_request(request: Request) -> str:
    configured = str(PUBLIC_BASE_URL).rstrip("/")
    return f"{configured}/zoom/oauth/callback"


def _build_authorize_url(request: Request, tenant_slug: str) -> str:
    base = "https://zoom.us/oauth/authorize"
    if not ZOOM_CLIENT_ID:
        raise HTTPException(status_code=500, detail="CLIENT_ID_ZOOM is missing")
    params = dict(parse_qsl(urlparse(base).query))
    params.update(
        {
            "response_type": "code",
            "client_id": ZOOM_CLIENT_ID,
            "redirect_uri": _redirect_uri_from_request(request),
            "state": tenant_slug,
        }
    )
    parsed = urlparse(base)
    return urlunparse(parsed._replace(query=urlencode(params)))


@router.get("/zoom/setup", response_class=HTMLResponse)
async def zoom_setup_page(request: Request, tenant: str = "", db: Session = Depends(get_db)):
    if not tenant:
        return HTMLResponse("<h3>Pass ?tenant=your-slug to connect Zoom for a tenant.</h3>")
    tenant_obj = get_tenant_by_slug(db, tenant)
    if tenant_obj is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    integration = get_integration_payload(db, tenant_obj.id, "zoom")
    authorize_url = _build_authorize_url(request, tenant)
    html = f"""
    <html>
      <body style='font-family: Arial, sans-serif; margin: 24px;'>
        <h2>Zoom OAuth for {tenant_obj.display_name}</h2>
        <p>Status: {integration.get('status')}</p>
        <p><a href='{authorize_url}'>Connect Zoom</a></p>
      </body>
    </html>
    """
    return HTMLResponse(content=html)


@router.get("/zoom/oauth/start")
async def zoom_oauth_start(request: Request, tenant: str, db: Session = Depends(get_db)):
    tenant_obj = get_tenant_by_slug(db, tenant)
    if tenant_obj is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return RedirectResponse(url=_build_authorize_url(request, tenant), status_code=302)


@router.get("/zoom/oauth/callback")
async def zoom_oauth_callback(request: Request, code: str = "", state: str = "", db: Session = Depends(get_db)):
    if not code:
        raise HTTPException(status_code=400, detail="Missing OAuth code")
    tenant_slug = state.strip()
    tenant = get_tenant_by_slug(db, tenant_slug)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    if not ZOOM_CLIENT_ID or not ZOOM_CLIENT_SECRET:
        raise HTTPException(status_code=500, detail="Zoom client credentials missing")

    redirect_uri = _redirect_uri_from_request(request)
    basic = base64.b64encode(f"{ZOOM_CLIENT_ID}:{ZOOM_CLIENT_SECRET}".encode("utf-8")).decode("utf-8")
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
        resp = await client.post(ZOOM_TOKEN_URL, data=data, headers=headers)
    if resp.status_code >= 400:
        logger.error("Zoom token exchange failed status=%s body=%s", resp.status_code, resp.text)
        raise HTTPException(status_code=500, detail=f"Zoom token exchange failed: {resp.text}")

    payload = resp.json()
    payload.update(
        {
            "client_id": ZOOM_CLIENT_ID,
            "client_secret": ZOOM_CLIENT_SECRET,
            "token_url": ZOOM_TOKEN_URL,
            "saved_at_unix": int(time.time()),
        }
    )
    current = get_integration_payload(db, tenant.id, "zoom")
    settings = current.get("settings") or {}
    upsert_integration(db, tenant, "zoom", credentials=payload, settings=settings or {}, status="active")

    return HTMLResponse(
        content=(
            f"<h3>Zoom OAuth successful for {tenant.display_name}</h3>"
            f"<p>Return to <a href='/admin/tenants/{tenant.slug}'>tenant dashboard</a>.</p>"
        )
    )
