import logging
from datetime import datetime, timezone
from urllib.parse import quote_plus

from fastapi import FastAPI, Request
from fastapi.responses import Response
from starlette.middleware.sessions import SessionMiddleware
from twilio.twiml.voice_response import VoiceResponse

from app_config import (
    LIVEKIT_SIP_PASSWORD,
    LIVEKIT_SIP_URI,
    LIVEKIT_SIP_USERNAME,
    PUBLIC_BASE_URL,
    SESSION_SECRET_KEY,
)
from db import db_session
from routes.admin import router as admin_router
from routes.agent import router as agent_router
from routes.events import router as events_router
from routes.tools_email import router as email_router
from routes.zoom_oauth import router as zoom_oauth_router
from services.bootstrap import ensure_bootstrap_state
from services.call_events import log_call_event
from services.tenants import get_active_config, normalize_phone_number, resolve_tenant_by_number

logger = logging.getLogger("main")
logging.basicConfig(level=logging.INFO)

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET_KEY, same_site="lax", https_only=False)

app.include_router(email_router)
app.include_router(events_router)
app.include_router(zoom_oauth_router)
app.include_router(agent_router)
app.include_router(admin_router)


@app.on_event("startup")
def startup() -> None:
    with db_session() as session:
        ensure_bootstrap_state(session)


async def _request_payload(request: Request) -> dict:
    payload: dict = {}
    if request.method == "POST":
        form = await request.form()
        payload.update(dict(form))
    payload.update(dict(request.query_params))
    return payload


def _sip_uri_with_headers(base_uri: str, headers: dict[str, str]) -> str:
    prefix = "&" if "?" in base_uri else "?"
    parts = [f"{quote_plus(key)}={quote_plus(value)}" for key, value in headers.items() if value]
    if not parts:
        return base_uri
    return base_uri + prefix + "&".join(parts)


@app.api_route("/incoming-call", methods=["GET", "POST"])
async def incoming_call(request: Request):
    payload = await _request_payload(request)
    called_number = normalize_phone_number(str(payload.get("To") or payload.get("CalledVia") or payload.get("ForwardedFrom") or ""))
    caller_number = normalize_phone_number(str(payload.get("From") or payload.get("Caller") or ""))
    with db_session() as session:
        tenant = resolve_tenant_by_number(session, called_number)
        if tenant is not None:
            config = get_active_config(session, tenant.id)
            config_version = str(config.version if config else 1)
            log_call_event(
                session,
                event_type="incoming_call",
                payload={
                    **payload,
                    "called_number": called_number,
                    "caller_number": caller_number,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
                tenant=tenant,
            )
        else:
            config = None
            config_version = ""
            log_call_event(
                session,
                event_type="incoming_call_unresolved",
                payload={
                    **payload,
                    "called_number": called_number,
                    "caller_number": caller_number,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
                tenant=None,
            )

    if tenant is None:
        vr = VoiceResponse()
        vr.say("We are unable to route your call right now. Please try again later.")
        vr.hangup()
        return Response(content=str(vr), media_type="application/xml")

    sip_uri = _sip_uri_with_headers(
        LIVEKIT_SIP_URI,
        {
            "x-tenant-id": tenant.id,
            "x-tenant-slug": tenant.slug,
            "x-config-version": config_version,
            "x-called-number": called_number,
            "x-parent-call-sid": str(payload.get("CallSid") or ""),
            "x-caller-number": caller_number,
        },
    )

    vr = VoiceResponse()
    dial = vr.dial(answer_on_bridge=True, timeout=20)
    dial.sip(
        sip_uri,
        username=LIVEKIT_SIP_USERNAME,
        password=LIVEKIT_SIP_PASSWORD,
        status_callback=f"{PUBLIC_BASE_URL.rstrip('/')}/sip-status",
        status_callback_method="POST",
        status_callback_event="initiated ringing answered completed",
    )
    return Response(content=str(vr), media_type="application/xml")


@app.post("/sip-status")
async def sip_status(request: Request):
    form = await request.form()
    payload = dict(form)
    called_number = normalize_phone_number(str(payload.get("CalledVia") or payload.get("ForwardedFrom") or payload.get("To") or ""))
    with db_session() as session:
        tenant = resolve_tenant_by_number(session, called_number)
        log_call_event(session, event_type="sip_status", payload=payload, tenant=tenant)
    logger.info(">>> SIP STATUS: %s", payload)
    return Response("OK")


@app.get("/")
async def root():
    return {"ok": True, "service": "ai-voice-assistant", "admin": "/admin"}
