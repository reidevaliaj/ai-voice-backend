import logging
import base64
import json
from datetime import datetime, timezone
from urllib.parse import quote_plus

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.middleware.sessions import SessionMiddleware
from twilio.twiml.voice_response import VoiceResponse

from app_config import (
    LIVEKIT_SIP_PASSWORD,
    LIVEKIT_SIP_URI,
    LIVEKIT_SIP_USERNAME,
    PUBLIC_BASE_URL,
    SESSION_SECRET_KEY,
    TELNYX_API_BASE_URL,
    TELNYX_API_KEY,
    TELNYX_VOICE_WEBHOOK_PATH,
)
from db import db_session
from routes.admin import router as admin_router
from routes.agent import router as agent_router
from routes.events import router as events_router
from routes.tools_email import router as email_router
from routes.zoom_oauth import router as zoom_oauth_router
from services.bootstrap import ensure_bootstrap_state
from services.call_events import log_call_event
from services.tenants import (
    get_active_config,
    get_tenant_by_id,
    get_tenant_by_slug,
    normalize_phone_number,
    resolve_tenant_by_number,
)

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


def _request_is_json(request: Request) -> bool:
    content_type = str(request.headers.get("content-type") or "").lower()
    return "application/json" in content_type


async def _request_json_payload(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _sip_uri_with_headers(base_uri: str, headers: dict[str, str]) -> str:
    prefix = "&" if "?" in base_uri else "?"
    parts = [f"{quote_plus(key)}={quote_plus(value)}" for key, value in headers.items() if value]
    if not parts:
        return base_uri
    return base_uri + prefix + "&".join(parts)


def _public_url(path: str) -> str:
    return PUBLIC_BASE_URL.rstrip("/") + path


def _telnyx_webhook_url() -> str:
    path = TELNYX_VOICE_WEBHOOK_PATH if TELNYX_VOICE_WEBHOOK_PATH.startswith("/") else f"/{TELNYX_VOICE_WEBHOOK_PATH}"
    return _public_url(path)


def _telnyx_transport_protocol(sip_uri: str) -> str:
    candidate = (sip_uri or "").lower()
    if "transport=tls" in candidate:
        return "TLS"
    if "transport=tcp" in candidate:
        return "TCP"
    return "UDP"


def _encode_telnyx_client_state(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def _decode_telnyx_client_state(value: str | None) -> dict:
    text = str(value or "").strip()
    if not text:
        return {}
    try:
        decoded = base64.b64decode(text).decode("utf-8")
        payload = json.loads(decoded)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _flatten_telnyx_event(wrapper: dict) -> dict:
    data = wrapper.get("data") if isinstance(wrapper.get("data"), dict) else {}
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
    meta = wrapper.get("meta") if isinstance(wrapper.get("meta"), dict) else {}
    event_type = str(data.get("event_type") or "")
    occurred_at = str(data.get("occurred_at") or payload.get("timestamp") or "")
    normalized = {
        **payload,
        "CallSid": str(payload.get("call_control_id") or ""),
        "ParentCallSid": str(payload.get("parent_call_sid") or ""),
        "From": str(payload.get("from") or ""),
        "To": str(payload.get("to") or ""),
        "CallStatus": str(payload.get("call_status") or payload.get("state") or ""),
        "Timestamp": occurred_at,
        "CallbackSource": "telnyx_voice_api",
        "event_type": event_type,
        "event_id": str(data.get("id") or ""),
        "occurred_at": occurred_at,
        "direction": str(payload.get("direction") or ""),
        "state": str(payload.get("state") or ""),
        "meta_attempt": meta.get("attempt"),
        "raw_wrapper": wrapper,
    }
    return normalized


def _is_telnyx_voice_event(wrapper: dict) -> bool:
    data = wrapper.get("data")
    return isinstance(data, dict) and isinstance(data.get("payload"), dict) and bool(data.get("event_type"))


def _is_initial_telnyx_inbound_event(event_payload: dict) -> bool:
    return (
        str(event_payload.get("event_type") or "") == "call.initiated"
        and str(event_payload.get("direction") or "").lower() == "incoming"
    )


def _telnyx_command_id(prefix: str, call_control_id: str) -> str:
    base = call_control_id.replace(":", "_")
    return f"{prefix}-{base}"


async def _post_telnyx_command(call_control_id: str, action: str, body: dict) -> dict:
    if not TELNYX_API_KEY:
        raise RuntimeError("TELNYX_API_KEY is not configured")
    url = f"{TELNYX_API_BASE_URL.rstrip('/')}/calls/{call_control_id}/actions/{action}"
    headers = {
        "Authorization": f"Bearer {TELNYX_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    timeout = httpx.Timeout(12.0, connect=8.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, headers=headers, json=body)
        response.raise_for_status()
        payload = response.json()
    logger.info("[TELNYX] command=%s call_control_id=%s response=%s", action, call_control_id, payload)
    return payload if isinstance(payload, dict) else {"raw": payload}


def _resolve_telnyx_tenant(session, event_payload: dict):
    client_state = _decode_telnyx_client_state(str(event_payload.get("client_state") or ""))
    tenant = None
    tenant_id = str(client_state.get("tenant_id") or "")
    tenant_slug = str(client_state.get("tenant_slug") or "")
    if tenant_id:
        tenant = get_tenant_by_id(session, tenant_id)
    if tenant is None and tenant_slug:
        tenant = get_tenant_by_slug(session, tenant_slug)
    if tenant is None:
        tenant = resolve_tenant_by_number(session, str(event_payload.get("To") or event_payload.get("to") or ""))
    return tenant, client_state


async def _handle_telnyx_voice_webhook(wrapper: dict):
    event_payload = _flatten_telnyx_event(wrapper)
    call_control_id = str(event_payload.get("CallSid") or "")
    called_number = normalize_phone_number(str(event_payload.get("To") or ""))
    caller_number = normalize_phone_number(str(event_payload.get("From") or ""))
    with db_session() as session:
        tenant, client_state = _resolve_telnyx_tenant(session, event_payload)
        if client_state:
            called_number = normalize_phone_number(str(client_state.get("called_number") or called_number))
            caller_number = normalize_phone_number(str(client_state.get("caller_number") or caller_number))
            event_payload["called_number"] = called_number
            event_payload["caller_number"] = caller_number
        log_call_event(
            session,
            event_type=f"telnyx_{str(event_payload.get('event_type') or 'event').replace('.', '_')}",
            payload=event_payload,
            tenant=tenant,
        )
        config_version = ""
        tenant_id = ""
        tenant_slug = ""
        if tenant is not None:
            config = get_active_config(session, tenant.id)
            config_version = str(config.version if config else 1)
            tenant_id = tenant.id
            tenant_slug = tenant.slug

    logger.info(
        "[TELNYX] event=%s call_control_id=%s direction=%s state=%s from=%s to=%s tenant=%s",
        event_payload.get("event_type"),
        call_control_id,
        event_payload.get("direction"),
        event_payload.get("state"),
        caller_number,
        called_number,
        tenant_slug or "unresolved",
    )

    if not _is_initial_telnyx_inbound_event(event_payload):
        return JSONResponse({"ok": True, "handled": "logged"})

    if not TELNYX_API_KEY:
        logger.error("[TELNYX] TELNYX_API_KEY missing; cannot control incoming call call_control_id=%s", call_control_id)
        return JSONResponse({"ok": False, "error": "TELNYX_API_KEY missing"}, status_code=503)

    if not call_control_id:
        return JSONResponse({"ok": False, "error": "Missing call_control_id"}, status_code=400)

    client_state = _encode_telnyx_client_state(
        {
            "provider": "telnyx",
            "tenant_id": tenant_id,
            "tenant_slug": tenant_slug,
            "config_version": config_version,
            "called_number": called_number,
            "caller_number": caller_number,
        }
    )
    webhook_url = _telnyx_webhook_url()

    if not tenant_id:
        await _post_telnyx_command(
            call_control_id,
            "hangup",
            {
                "client_state": client_state,
                "command_id": _telnyx_command_id("hangup-unresolved", call_control_id),
            },
        )
        return JSONResponse({"ok": True, "handled": "hangup_unresolved"})

    await _post_telnyx_command(
        call_control_id,
        "answer",
        {
            "client_state": client_state,
            "command_id": _telnyx_command_id("answer", call_control_id),
            "webhook_url": webhook_url,
            "webhook_url_method": "POST",
        },
    )
    await _post_telnyx_command(
        call_control_id,
        "transfer",
        {
            "to": LIVEKIT_SIP_URI,
            "timeout_secs": 20,
            "sip_auth_username": LIVEKIT_SIP_USERNAME,
            "sip_auth_password": LIVEKIT_SIP_PASSWORD,
            "sip_transport_protocol": _telnyx_transport_protocol(LIVEKIT_SIP_URI),
            "command_id": _telnyx_command_id("transfer", call_control_id),
            "client_state": client_state,
            "target_leg_client_state": client_state,
            "webhook_url": webhook_url,
            "webhook_url_method": "POST",
            "custom_headers": [
                {"name": "X-Tenant-Id", "value": tenant_id},
                {"name": "X-Tenant-Slug", "value": tenant_slug},
                {"name": "X-Config-Version", "value": config_version},
                {"name": "X-Called-Number", "value": called_number},
                {"name": "X-Caller-Number", "value": caller_number},
                {"name": "X-Parent-Call-Sid", "value": call_control_id},
                {"name": "X-Call-Provider", "value": "telnyx"},
            ],
        },
    )
    return JSONResponse({"ok": True, "handled": "answer_transfer", "tenant": tenant_slug})


@app.api_route("/incoming-call", methods=["GET", "POST"])
async def incoming_call(request: Request):
    if request.method == "POST" and _request_is_json(request):
        telnyx_payload = await _request_json_payload(request)
        if _is_telnyx_voice_event(telnyx_payload):
            return await _handle_telnyx_voice_webhook(telnyx_payload)
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


@app.post(TELNYX_VOICE_WEBHOOK_PATH)
async def telnyx_voice_webhook(request: Request):
    payload = await _request_json_payload(request)
    if not _is_telnyx_voice_event(payload):
        return JSONResponse({"ok": False, "error": "Unsupported payload"}, status_code=400)
    return await _handle_telnyx_voice_webhook(payload)


@app.get("/")
async def root():
    return {"ok": True, "service": "ai-voice-assistant", "admin": "/admin"}
