import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app_config import PUBLIC_BASE_URL
from db import get_db
from models import AdminUser, CallEvent, Tenant, TenantPhoneNumber
from security import decrypt_json, mask_secret, verify_password
from services.tenants import (
    build_runtime_context,
    config_form_payload,
    create_config_version,
    create_tenant,
    get_active_config,
    get_integration_payload,
    get_tenant_by_slug,
    integration_form_payload,
    normalize_phone_number,
    parse_lines,
    upsert_integration,
    upsert_phone_number,
)
from tools.google_calendar import CalendarContext, validate_calendar_context
from tools.zoom_meetings import ZoomContext, validate_zoom_context

router = APIRouter()
templates = Jinja2Templates(directory="templates")


def _flash(request: Request, level: str, message: str) -> None:
    request.session["flash"] = {"level": level, "message": message}


def _consume_flash(request: Request) -> dict[str, str] | None:
    return request.session.pop("flash", None)


def _current_admin(request: Request, session: Session) -> AdminUser | None:
    admin_id = request.session.get("admin_user_id")
    if not admin_id:
        return None
    return session.get(AdminUser, admin_id)


def require_admin(request: Request, session: Session) -> AdminUser:
    admin = _current_admin(request, session)
    if admin is None or not admin.is_active:
        raise HTTPException(status_code=303, headers={"Location": "/admin/login"})
    return admin


def _integration_summary(session: Session, tenant_id: str) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for provider in ("google_calendar", "zoom", "email"):
        payload = get_integration_payload(session, tenant_id, provider)
        credentials = payload.get("credentials") or {}
        masked = {key: mask_secret(str(value)) for key, value in credentials.items() if isinstance(value, (str, int))}
        summary[provider] = {
            "status": payload.get("status", "missing"),
            "settings": payload.get("settings", {}),
            "credentials_masked": masked,
            "last_validated_at": payload.get("last_validated_at", ""),
            "last_error": payload.get("last_error", ""),
        }
    return summary


@router.get("/admin/login")
async def admin_login_page(request: Request):
    return templates.TemplateResponse(
        request,
        "admin/login.html",
        {
            "page_title": "Admin Login",
            "flash": _consume_flash(request),
        },
    )


@router.post("/admin/login")
async def admin_login(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    email = str(form.get("email") or "").strip().lower()
    password = str(form.get("password") or "")
    admin = db.scalar(select(AdminUser).where(AdminUser.email == email))
    if admin is None or not verify_password(password, admin.password_hash):
        _flash(request, "error", "Invalid email or password")
        return RedirectResponse(url="/admin/login", status_code=303)
    request.session["admin_user_id"] = admin.id
    _flash(request, "success", "Welcome back.")
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/admin/logout")
async def admin_logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/admin/login", status_code=303)


@router.get("/admin")
async def admin_home(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    tenants = list(db.scalars(select(Tenant).order_by(Tenant.created_at.desc())))
    cards = []
    for tenant in tenants:
        config = get_active_config(db, tenant.id)
        cards.append(
            {
                "tenant": tenant,
                "phone_numbers": list(db.scalars(select(TenantPhoneNumber).where(TenantPhoneNumber.tenant_id == tenant.id))),
                "config": config,
            }
        )
    return templates.TemplateResponse(
        request,
        "admin/dashboard.html",
        {
            "page_title": "Tenant Dashboard",
            "flash": _consume_flash(request),
            "tenant_cards": cards,
            "public_base_url": PUBLIC_BASE_URL,
        },
    )


@router.post("/admin/tenants")
async def create_tenant_action(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    form = await request.form()
    slug = str(form.get("slug") or "").strip().lower()
    display_name = str(form.get("display_name") or "").strip()
    phone_number = str(form.get("phone_number") or "").strip()
    notes = str(form.get("notes") or "").strip()
    if not slug or not display_name:
        _flash(request, "error", "Slug and display name are required")
        return RedirectResponse(url="/admin", status_code=303)
    if get_tenant_by_slug(db, slug):
        _flash(request, "error", f"Tenant '{slug}' already exists")
        return RedirectResponse(url="/admin", status_code=303)
    tenant = create_tenant(db, slug, display_name, notes=notes)
    if phone_number:
        upsert_phone_number(db, tenant, phone_number)
    _flash(request, "success", f"Tenant '{display_name}' created")
    return RedirectResponse(url=f"/admin/tenants/{tenant.slug}", status_code=303)


@router.get("/admin/tenants/{slug}")
async def tenant_detail(slug: str, request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    tenant = get_tenant_by_slug(db, slug)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    config = get_active_config(db, tenant.id)
    phone_numbers = list(db.scalars(select(TenantPhoneNumber).where(TenantPhoneNumber.tenant_id == tenant.id).order_by(TenantPhoneNumber.created_at.asc())))
    recent_events = list(
        db.scalars(
            select(CallEvent).where(CallEvent.tenant_id == tenant.id).order_by(CallEvent.created_at.desc()).limit(30)
        )
    )
    integration_summary = _integration_summary(db, tenant.id)
    runtime = build_runtime_context(db, tenant) if config else None
    return templates.TemplateResponse(
        request,
        "admin/tenant_detail.html",
        {
            "page_title": f"Tenant {tenant.display_name}",
            "tenant": tenant,
            "config": config,
            "config_form": config_form_payload(config),
            "phone_numbers": phone_numbers,
            "integration_summary": integration_summary,
            "integration_forms": {
                provider: integration_form_payload(get_integration_payload(db, tenant.id, provider))
                for provider in ("google_calendar", "zoom", "email")
            },
            "recent_events": recent_events,
            "runtime": runtime,
            "flash": _consume_flash(request),
        },
    )


@router.post("/admin/tenants/{slug}/config")
async def update_tenant_config(slug: str, request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    tenant = get_tenant_by_slug(db, slug)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    form = await request.form()
    enabled_tools = {
        "email_summary": form.get("tool_email_summary") == "on",
        "meeting_creation": form.get("tool_meeting_creation") == "on",
        "case_creation": form.get("tool_case_creation") == "on",
        "calendar_lookup": form.get("tool_calendar_lookup") == "on",
        "zoom_meetings": form.get("tool_zoom_meetings") == "on",
    }
    try:
        extra_settings = json.loads(str(form.get("extra_settings") or "{}"))
    except json.JSONDecodeError:
        _flash(request, "error", "Extra settings must be valid JSON")
        return RedirectResponse(url=f"/admin/tenants/{slug}", status_code=303)

    payload = {
        "business_name": str(form.get("business_name") or tenant.display_name).strip(),
        "timezone": str(form.get("timezone") or "Europe/Budapest").strip(),
        "greeting": str(form.get("greeting") or "").strip(),
        "services": parse_lines(str(form.get("services") or "")),
        "faq_notes": str(form.get("faq_notes") or "").strip(),
        "prompt_appendix": str(form.get("prompt_appendix") or "").strip(),
        "business_hours": str(form.get("business_hours") or "09:00-17:00").strip(),
        "business_days": str(form.get("business_days") or "1,2,3,4,5").strip(),
        "meeting_duration_minutes": int(str(form.get("meeting_duration_minutes") or 30).strip() or 30),
        "booking_horizon_days": int(str(form.get("booking_horizon_days") or 14).strip() or 14),
        "enabled_tools": enabled_tools,
        "llm_model": str(form.get("llm_model") or "gpt-4.1-mini").strip(),
        "tts_voice": str(form.get("tts_voice") or "").strip(),
        "owner_name": str(form.get("owner_name") or "").strip(),
        "owner_email": str(form.get("owner_email") or "").strip(),
        "reply_to_email": str(form.get("reply_to_email") or "").strip(),
        "from_email": str(form.get("from_email") or "").strip(),
        "notification_targets": parse_lines(str(form.get("notification_targets") or "")),
        "extra_settings": extra_settings,
    }
    create_config_version(db, tenant, payload)
    _flash(request, "success", "New configuration version saved")
    return RedirectResponse(url=f"/admin/tenants/{slug}", status_code=303)


@router.post("/admin/tenants/{slug}/phone-numbers")
async def add_phone_number(slug: str, request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    tenant = get_tenant_by_slug(db, slug)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    form = await request.form()
    phone_number = normalize_phone_number(str(form.get("phone_number") or ""))
    label = str(form.get("label") or "primary").strip() or "primary"
    if not phone_number:
        _flash(request, "error", "Phone number is required")
        return RedirectResponse(url=f"/admin/tenants/{slug}", status_code=303)
    upsert_phone_number(db, tenant, phone_number, label=label)
    _flash(request, "success", f"Phone number {phone_number} saved")
    return RedirectResponse(url=f"/admin/tenants/{slug}", status_code=303)


@router.post("/admin/tenants/{slug}/integrations/{provider}")
async def save_integration(slug: str, provider: str, request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    tenant = get_tenant_by_slug(db, slug)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    form = await request.form()
    try:
        credentials = json.loads(str(form.get("credentials") or "{}"))
        settings = json.loads(str(form.get("settings") or "{}"))
    except json.JSONDecodeError:
        _flash(request, "error", f"{provider} credentials/settings must be valid JSON")
        return RedirectResponse(url=f"/admin/tenants/{slug}", status_code=303)

    status = str(form.get("status") or "active").strip() or "active"
    upsert_integration(db, tenant, provider, credentials=credentials, settings=settings, status=status)
    _flash(request, "success", f"{provider} integration saved")
    return RedirectResponse(url=f"/admin/tenants/{slug}", status_code=303)


@router.post("/admin/tenants/{slug}/integrations/{provider}/validate")
async def validate_integration(slug: str, provider: str, request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    tenant = get_tenant_by_slug(db, slug)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    payload = get_integration_payload(db, tenant.id, provider)
    try:
        updated_credentials = None
        if provider == "google_calendar":
            result = validate_calendar_context(CalendarContext(payload["credentials"], payload["settings"]))
        elif provider == "zoom":
            result = validate_zoom_context(ZoomContext(payload["credentials"], payload["settings"]))
            updated_credentials = result.pop("updated_credentials", None)
        elif provider == "email":
            settings = payload.get("settings") or {}
            if not settings.get("from_email"):
                raise RuntimeError("Email integration requires from_email in settings")
            result = {"ok": True, "from_email": settings.get("from_email"), "notification_targets": settings.get("notification_targets", [])}
        else:
            raise RuntimeError(f"Unsupported provider: {provider}")

        if updated_credentials:
            upsert_integration(
                db,
                tenant,
                provider,
                credentials=updated_credentials,
                settings=payload.get("settings") or {},
                status="active",
                mark_validated=True,
            )
        else:
            upsert_integration(
                db,
                tenant,
                provider,
                credentials=payload.get("credentials") or {},
                settings=payload.get("settings") or {},
                status="active",
                mark_validated=True,
            )
        summary = ", ".join(f"{key}={value}" for key, value in result.items() if key != "ok")
        _flash(request, "success", f"{provider} validation passed. {summary}")
    except Exception as exc:
        upsert_integration(
            db,
            tenant,
            provider,
            credentials=payload.get("credentials") or {},
            settings=payload.get("settings") or {},
            status="error",
            last_error=str(exc),
        )
        _flash(request, "error", f"{provider} validation failed: {exc}")
    return RedirectResponse(url=f"/admin/tenants/{slug}", status_code=303)
