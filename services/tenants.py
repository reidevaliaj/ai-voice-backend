import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app_config import (
    DATA_DIR,
    DEFAULT_BOOKING_HORIZON_DAYS,
    DEFAULT_BUSINESS_DAYS,
    DEFAULT_BUSINESS_HOURS,
    DEFAULT_BUSINESS_TIMEZONE,
    DEFAULT_FROM_EMAIL,
    DEFAULT_INBOUND_PHONE_NUMBER,
    DEFAULT_LLM_MODEL,
    DEFAULT_MEETING_DURATION_MINUTES,
    DEFAULT_MEETING_OWNER_EMAIL,
    DEFAULT_OWNER_EMAIL,
    DEFAULT_REPLY_TO_EMAIL,
    DEFAULT_TENANT_SLUG,
    DEFAULT_TTS_VOICE,
    GOOGLE_CALENDAR_ID,
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
    GOOGLE_REFRESH_TOKEN,
    ZOOM_CLIENT_ID,
    ZOOM_CLIENT_SECRET,
    ZOOM_OWNER_EMAIL,
    ZOOM_TOKEN_URL,
)
from models import CallConfigSnapshot, Tenant, TenantAgentConfig, TenantIntegration, TenantPhoneNumber
from security import decrypt_json, encrypt_json


def normalize_phone_number(value: str | None) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    text = text.replace("tel:", "").replace("sip:", "")
    text = text.replace("sip_", "")
    text = text.split("@", 1)[0]
    cleaned = []
    for index, char in enumerate(text):
        if char.isdigit():
            cleaned.append(char)
        elif char == "+" and index == 0:
            cleaned.append(char)
    return "".join(cleaned)


def parse_lines(value: str | list[str] | None) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if not value:
        return []
    return [line.strip() for line in str(value).replace("\r", "").split("\n") if line.strip()]


def services_to_text(services: list[str]) -> str:
    if not services:
        return ""
    return "\n".join(f"- {item}" for item in services)


def default_enabled_tools() -> dict[str, bool]:
    return {
        "email_summary": True,
        "meeting_creation": True,
        "case_creation": True,
        "calendar_lookup": True,
        "zoom_meetings": True,
    }


def default_config_payload(display_name: str = "Code Studio") -> dict[str, Any]:
    return {
        "business_name": display_name,
        "timezone": DEFAULT_BUSINESS_TIMEZONE,
        "greeting": f"Thanks for calling {display_name}. How may we help you today?",
        "services": [
            "Web Design",
            "WordPress, TYPO3, Shopify",
            "Headless CMS",
            "Web applications",
            "AI integration and agents creation",
            "SEO",
        ],
        "faq_notes": "",
        "prompt_appendix": "",
        "business_hours": DEFAULT_BUSINESS_HOURS,
        "business_days": DEFAULT_BUSINESS_DAYS,
        "meeting_duration_minutes": DEFAULT_MEETING_DURATION_MINUTES,
        "booking_horizon_days": DEFAULT_BOOKING_HORIZON_DAYS,
        "enabled_tools": default_enabled_tools(),
        "llm_model": DEFAULT_LLM_MODEL,
        "tts_voice": DEFAULT_TTS_VOICE,
        "owner_name": "Rey",
        "owner_email": DEFAULT_OWNER_EMAIL,
        "reply_to_email": DEFAULT_REPLY_TO_EMAIL,
        "from_email": DEFAULT_FROM_EMAIL,
        "notification_targets": [DEFAULT_OWNER_EMAIL],
        "extra_settings": {
            "meeting_owner_email": DEFAULT_MEETING_OWNER_EMAIL,
            "call_types": [
                "sales_lead",
                "support_issue",
                "vendor_sales",
                "unrelated",
            ],
        },
    }


def get_tenant_by_slug(session: Session, slug: str) -> Tenant | None:
    return session.scalar(select(Tenant).where(Tenant.slug == slug))


def get_tenant_by_id(session: Session, tenant_id: str) -> Tenant | None:
    return session.scalar(select(Tenant).where(Tenant.id == tenant_id))


def resolve_tenant_by_number(session: Session, called_number: str | None) -> Tenant | None:
    normalized = normalize_phone_number(called_number)
    if normalized:
        stmt = (
            select(Tenant)
            .join(TenantPhoneNumber, TenantPhoneNumber.tenant_id == Tenant.id)
            .where(
                Tenant.status == "active",
                TenantPhoneNumber.status == "active",
                TenantPhoneNumber.phone_number == normalized,
            )
        )
        tenant = session.scalar(stmt)
        if tenant is not None:
            return tenant
    if DEFAULT_TENANT_SLUG:
        tenant = get_tenant_by_slug(session, DEFAULT_TENANT_SLUG)
        if tenant is not None and tenant.status == "active":
            return tenant
    tenants = list(session.scalars(select(Tenant).where(Tenant.status == "active")))
    if len(tenants) == 1:
        return tenants[0]
    return None


def resolve_tenant_by_recent_caller(session: Session, caller_id: str | None, minutes: int = 20) -> Tenant | None:
    from models import CallEvent

    normalized = normalize_phone_number(caller_id)
    if not normalized:
        return None
    threshold = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    stmt = (
        select(CallEvent)
        .where(CallEvent.caller_number == normalized, CallEvent.created_at >= threshold)
        .order_by(CallEvent.created_at.desc())
    )
    event = session.scalars(stmt).first()
    if event and event.tenant_id:
        return get_tenant_by_id(session, event.tenant_id)
    return None


def get_active_config(session: Session, tenant_id: str) -> TenantAgentConfig | None:
    stmt = (
        select(TenantAgentConfig)
        .where(TenantAgentConfig.tenant_id == tenant_id, TenantAgentConfig.is_active.is_(True))
        .order_by(TenantAgentConfig.version.desc())
    )
    return session.scalars(stmt).first()


def get_config_by_version(session: Session, tenant_id: str, version: int) -> TenantAgentConfig | None:
    stmt = select(TenantAgentConfig).where(
        TenantAgentConfig.tenant_id == tenant_id,
        TenantAgentConfig.version == version,
    )
    return session.scalar(stmt)


def create_tenant(session: Session, slug: str, display_name: str, notes: str = "") -> Tenant:
    tenant = Tenant(slug=slug, display_name=display_name, notes=notes, status="active")
    session.add(tenant)
    session.flush()
    create_config_version(session, tenant, default_config_payload(display_name))
    return tenant


def create_config_version(session: Session, tenant: Tenant, payload: dict[str, Any]) -> TenantAgentConfig:
    current = get_active_config(session, tenant.id)
    next_version = 1 if current is None else current.version + 1
    if current is not None:
        current.is_active = False
    config = TenantAgentConfig(
        tenant_id=tenant.id,
        version=next_version,
        is_active=True,
        business_name=str(payload.get("business_name") or tenant.display_name),
        timezone=str(payload.get("timezone") or DEFAULT_BUSINESS_TIMEZONE),
        greeting=str(payload.get("greeting") or f"Thanks for calling {tenant.display_name}. How may we help you today?"),
        services=parse_lines(payload.get("services")),
        faq_notes=str(payload.get("faq_notes") or ""),
        prompt_appendix=str(payload.get("prompt_appendix") or ""),
        business_hours=str(payload.get("business_hours") or DEFAULT_BUSINESS_HOURS),
        business_days=str(payload.get("business_days") or DEFAULT_BUSINESS_DAYS),
        meeting_duration_minutes=int(payload.get("meeting_duration_minutes") or DEFAULT_MEETING_DURATION_MINUTES),
        booking_horizon_days=int(payload.get("booking_horizon_days") or DEFAULT_BOOKING_HORIZON_DAYS),
        enabled_tools=dict(payload.get("enabled_tools") or default_enabled_tools()),
        llm_model=str(payload.get("llm_model") or DEFAULT_LLM_MODEL),
        tts_voice=str(payload.get("tts_voice") or DEFAULT_TTS_VOICE),
        owner_name=str(payload.get("owner_name") or ""),
        owner_email=str(payload.get("owner_email") or DEFAULT_OWNER_EMAIL),
        reply_to_email=str(payload.get("reply_to_email") or DEFAULT_REPLY_TO_EMAIL),
        from_email=str(payload.get("from_email") or DEFAULT_FROM_EMAIL),
        notification_targets=parse_lines(payload.get("notification_targets")),
        extra_settings=dict(payload.get("extra_settings") or {}),
    )
    session.add(config)
    session.flush()
    return config


def upsert_phone_number(session: Session, tenant: Tenant, phone_number: str, label: str = "primary") -> TenantPhoneNumber:
    normalized = normalize_phone_number(phone_number)
    stmt = select(TenantPhoneNumber).where(TenantPhoneNumber.phone_number == normalized)
    record = session.scalar(stmt)
    if record is None:
        record = TenantPhoneNumber(tenant_id=tenant.id, phone_number=normalized, label=label, status="active")
        session.add(record)
    else:
        record.tenant_id = tenant.id
        record.label = label
        record.status = "active"
    session.flush()
    return record


def _zoom_token_file_payload() -> dict[str, Any]:
    token_file = DATA_DIR / "zoom_tokens.json"
    if not token_file.exists():
        return {}
    try:
        raw = json.loads(token_file.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return dict(raw.get("zoom_tokens") or {})


def upsert_integration(
    session: Session,
    tenant: Tenant,
    provider: str,
    credentials: dict[str, Any],
    settings: dict[str, Any] | None = None,
    status: str = "active",
    *,
    last_error: str = "",
    mark_validated: bool = False,
) -> TenantIntegration:
    stmt = select(TenantIntegration).where(
        TenantIntegration.tenant_id == tenant.id,
        TenantIntegration.provider == provider,
    )
    integration = session.scalar(stmt)
    if integration is None:
        integration = TenantIntegration(tenant_id=tenant.id, provider=provider)
        session.add(integration)
    integration.status = status
    integration.credentials_encrypted = encrypt_json(credentials)
    integration.settings_json = dict(settings or {})
    integration.last_error = last_error
    if mark_validated:
        integration.last_validated_at = datetime.now(timezone.utc)
    session.flush()
    return integration


def get_integration_payload(session: Session, tenant_id: str, provider: str) -> dict[str, Any]:
    stmt = select(TenantIntegration).where(
        TenantIntegration.tenant_id == tenant_id,
        TenantIntegration.provider == provider,
    )
    integration = session.scalar(stmt)
    if integration is None:
        return {"provider": provider, "status": "missing", "credentials": {}, "settings": {}}
    return {
        "provider": provider,
        "status": integration.status,
        "credentials": decrypt_json(integration.credentials_encrypted),
        "settings": dict(integration.settings_json or {}),
        "last_validated_at": integration.last_validated_at.isoformat() if integration.last_validated_at else "",
        "last_error": integration.last_error or "",
    }


def build_runtime_context(session: Session, tenant: Tenant, config_version: int | None = None) -> dict[str, Any]:
    config = get_config_by_version(session, tenant.id, config_version) if config_version else get_active_config(session, tenant.id)
    if config is None:
        raise RuntimeError(f"Tenant {tenant.slug} has no active configuration")
    integrations = {
        provider: get_integration_payload(session, tenant.id, provider)
        for provider in ("google_calendar", "zoom", "email")
    }
    return {
        "tenant": {
            "id": tenant.id,
            "slug": tenant.slug,
            "display_name": tenant.display_name,
            "status": tenant.status,
            "notes": tenant.notes,
        },
        "config": {
            "version": config.version,
            "business_name": config.business_name,
            "timezone": config.timezone,
            "greeting": config.greeting,
            "services": list(config.services or []),
            "faq_notes": config.faq_notes,
            "prompt_appendix": config.prompt_appendix,
            "business_hours": config.business_hours,
            "business_days": config.business_days,
            "meeting_duration_minutes": config.meeting_duration_minutes,
            "booking_horizon_days": config.booking_horizon_days,
            "enabled_tools": dict(config.enabled_tools or {}),
            "llm_model": config.llm_model,
            "tts_voice": config.tts_voice,
            "owner_name": config.owner_name,
            "owner_email": config.owner_email,
            "reply_to_email": config.reply_to_email,
            "from_email": config.from_email,
            "notification_targets": list(config.notification_targets or []),
            "extra_settings": dict(config.extra_settings or {}),
        },
        "integrations": integrations,
    }


def create_or_update_snapshot(
    session: Session,
    *,
    tenant_id: str,
    room_name: str,
    call_sid: str,
    caller_id: str,
    config_version: int,
    snapshot: dict[str, Any],
) -> CallConfigSnapshot:
    stmt = select(CallConfigSnapshot).where(CallConfigSnapshot.room_name == room_name)
    record = session.scalar(stmt)
    if record is None:
        record = CallConfigSnapshot(
            tenant_id=tenant_id,
            room_name=room_name,
            call_sid=call_sid,
            caller_id=caller_id,
            config_version=config_version,
            snapshot_json=snapshot,
        )
        session.add(record)
    else:
        record.tenant_id = tenant_id
        record.call_sid = call_sid
        record.caller_id = caller_id
        record.config_version = config_version
        record.snapshot_json = snapshot
    session.flush()
    return record


def resolve_session_config(
    session: Session,
    *,
    tenant_id: str = "",
    tenant_slug: str = "",
    config_version: int | None = None,
    room_name: str = "",
    caller_id: str = "",
    called_number: str = "",
    call_sid: str = "",
) -> dict[str, Any]:
    tenant = None
    if tenant_id:
        tenant = get_tenant_by_id(session, tenant_id)
    if tenant is None and tenant_slug:
        tenant = get_tenant_by_slug(session, tenant_slug)
    if tenant is None and called_number:
        tenant = resolve_tenant_by_number(session, called_number)
    if tenant is None and caller_id:
        tenant = resolve_tenant_by_recent_caller(session, caller_id)
    if tenant is None:
        raise RuntimeError("Unable to resolve tenant for call")

    runtime = build_runtime_context(session, tenant, config_version=config_version)
    snapshot = {
        "tenant": runtime["tenant"],
        "config": runtime["config"],
        "resolved_at": datetime.now(timezone.utc).isoformat(),
        "room_name": room_name,
        "caller_id": caller_id,
        "called_number": normalize_phone_number(called_number),
        "call_sid": call_sid,
    }
    create_or_update_snapshot(
        session,
        tenant_id=tenant.id,
        room_name=room_name,
        call_sid=call_sid,
        caller_id=caller_id,
        config_version=int(runtime["config"]["version"]),
        snapshot=snapshot,
    )
    return snapshot


def seed_default_tenant(session: Session) -> Tenant:
    tenant = get_tenant_by_slug(session, DEFAULT_TENANT_SLUG)
    if tenant is None:
        tenant = create_tenant(session, DEFAULT_TENANT_SLUG, "Code Studio", notes="Seeded from legacy single-tenant setup")
    if DEFAULT_INBOUND_PHONE_NUMBER:
        upsert_phone_number(session, tenant, DEFAULT_INBOUND_PHONE_NUMBER)

    active_config = get_active_config(session, tenant.id)
    if active_config is None:
        create_config_version(session, tenant, default_config_payload(tenant.display_name))

    if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and GOOGLE_REFRESH_TOKEN:
        upsert_integration(
            session,
            tenant,
            "google_calendar",
            credentials={
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "refresh_token": GOOGLE_REFRESH_TOKEN,
                "calendar_id": GOOGLE_CALENDAR_ID or "primary",
            },
            settings={
                "business_timezone": DEFAULT_BUSINESS_TIMEZONE,
                "business_hours": DEFAULT_BUSINESS_HOURS,
                "business_days": DEFAULT_BUSINESS_DAYS,
            },
        )

    zoom_tokens = _zoom_token_file_payload()
    if ZOOM_CLIENT_ID and ZOOM_CLIENT_SECRET:
        upsert_integration(
            session,
            tenant,
            "zoom",
            credentials={
                "client_id": ZOOM_CLIENT_ID,
                "client_secret": ZOOM_CLIENT_SECRET,
                "token_url": ZOOM_TOKEN_URL,
                **zoom_tokens,
            },
            settings={
                "owner_email": ZOOM_OWNER_EMAIL,
            },
        )

    upsert_integration(
        session,
        tenant,
        "email",
        credentials={},
        settings={
            "from_email": DEFAULT_FROM_EMAIL,
            "reply_to_email": DEFAULT_REPLY_TO_EMAIL,
            "notification_targets": [DEFAULT_OWNER_EMAIL],
        },
    )
    session.flush()
    return tenant


def config_form_payload(config: TenantAgentConfig | None) -> dict[str, Any]:
    if config is None:
        return default_config_payload()
    return {
        "business_name": config.business_name,
        "timezone": config.timezone,
        "greeting": config.greeting,
        "services": "\n".join(config.services or []),
        "faq_notes": config.faq_notes,
        "prompt_appendix": config.prompt_appendix,
        "business_hours": config.business_hours,
        "business_days": config.business_days,
        "meeting_duration_minutes": config.meeting_duration_minutes,
        "booking_horizon_days": config.booking_horizon_days,
        "enabled_tools": dict(config.enabled_tools or {}),
        "llm_model": config.llm_model,
        "tts_voice": config.tts_voice,
        "owner_name": config.owner_name,
        "owner_email": config.owner_email,
        "reply_to_email": config.reply_to_email,
        "from_email": config.from_email,
        "notification_targets": "\n".join(config.notification_targets or []),
        "extra_settings": json.dumps(config.extra_settings or {}, indent=2, ensure_ascii=False),
    }


def integration_form_payload(integration: dict[str, Any]) -> dict[str, Any]:
    credentials = integration.get("credentials") or {}
    settings = integration.get("settings") or {}
    return {
        "status": integration.get("status", "inactive"),
        "credentials": json.dumps(credentials, indent=2, ensure_ascii=False),
        "settings": json.dumps(settings, indent=2, ensure_ascii=False),
        "last_error": integration.get("last_error", ""),
        "last_validated_at": integration.get("last_validated_at", ""),
    }
