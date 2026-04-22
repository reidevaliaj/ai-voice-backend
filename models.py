import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AdminUser(Base):
    __tablename__ = "admin_users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)

    phone_numbers: Mapped[list["TenantPhoneNumber"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    configs: Mapped[list["TenantAgentConfig"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    integrations: Mapped[list["TenantIntegration"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")


class TenantPhoneNumber(Base):
    __tablename__ = "tenant_phone_numbers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    phone_number: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String(255), default="primary", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    tenant: Mapped["Tenant"] = relationship(back_populates="phone_numbers")


class TenantAgentConfig(Base):
    __tablename__ = "tenant_agent_configs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "version", name="uq_tenant_agent_config_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    business_name: Mapped[str] = mapped_column(String(255), nullable=False)
    assistant_language: Mapped[str] = mapped_column(String(10), default="en", nullable=False)
    stt_language: Mapped[str] = mapped_column(String(10), default="en", nullable=False)
    timezone: Mapped[str] = mapped_column(String(100), nullable=False)
    greeting: Mapped[str] = mapped_column(Text, nullable=False)
    tenant_prompt: Mapped[str] = mapped_column(Text, default="", nullable=False)
    services: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    faq_notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    prompt_appendix: Mapped[str] = mapped_column(Text, default="", nullable=False)
    business_hours: Mapped[str] = mapped_column(String(50), default="09:00-17:00", nullable=False)
    business_days: Mapped[str] = mapped_column(String(50), default="1,2,3,4,5", nullable=False)
    meeting_duration_minutes: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    booking_horizon_days: Mapped[int] = mapped_column(Integer, default=14, nullable=False)
    enabled_tools: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    llm_model: Mapped[str] = mapped_column(String(100), default="gpt-4.1-mini", nullable=False)
    min_endpointing_delay: Mapped[float] = mapped_column(default=0.3, nullable=False)
    max_endpointing_delay: Mapped[float] = mapped_column(default=1.2, nullable=False)
    tts_voice: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    tts_speed: Mapped[float] = mapped_column(default=1.0, nullable=False)
    owner_name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    owner_email: Mapped[str] = mapped_column(String(320), default="", nullable=False)
    reply_to_email: Mapped[str] = mapped_column(String(320), default="", nullable=False)
    from_email: Mapped[str] = mapped_column(String(320), default="", nullable=False)
    notification_targets: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    extra_settings: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    tenant: Mapped["Tenant"] = relationship(back_populates="configs")


class TenantIntegration(Base):
    __tablename__ = "tenant_integrations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "provider", name="uq_tenant_integration_provider"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="inactive", nullable=False)
    credentials_encrypted: Mapped[str] = mapped_column(Text, default="", nullable=False)
    settings_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)

    tenant: Mapped["Tenant"] = relationship(back_populates="integrations")


class CallEvent(Base):
    __tablename__ = "call_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    call_sid: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    parent_call_sid: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    sip_call_id: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    room_name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    call_status: Mapped[str] = mapped_column(String(50), default="", nullable=False)
    sip_response_code: Mapped[str] = mapped_column(String(20), default="", nullable=False)
    caller_number: Mapped[str] = mapped_column(String(50), default="", nullable=False)
    called_number: Mapped[str] = mapped_column(String(50), default="", nullable=False)
    callback_timestamp: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)


class CallConfigSnapshot(Base):
    __tablename__ = "call_config_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    room_name: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    call_sid: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    caller_id: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)
