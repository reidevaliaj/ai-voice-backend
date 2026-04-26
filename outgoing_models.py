import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from outgoing_db import OutgoingBase


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OutgoingTenantProfile(OutgoingBase):
    __tablename__ = "outgoing_tenant_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False, index=True)
    tenant_slug: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="inactive", nullable=False)
    telnyx_connection_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    assistant_language: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    stt_language: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    llm_model: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    tts_voice: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    tts_speed: Mapped[float] = mapped_column(default=1.0, nullable=False)
    min_endpointing_delay: Mapped[float] = mapped_column(default=0.3, nullable=False)
    max_endpointing_delay: Mapped[float] = mapped_column(default=1.2, nullable=False)
    opening_phrase: Mapped[str] = mapped_column(Text, default="", nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, default="", nullable=False)
    caller_display_name: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)

    numbers: Mapped[list["OutgoingCallerNumber"]] = relationship(back_populates="profile", cascade="all, delete-orphan")
    calls: Mapped[list["OutgoingCall"]] = relationship(back_populates="profile", cascade="all, delete-orphan")


class OutgoingCallerNumber(OutgoingBase):
    __tablename__ = "outgoing_caller_numbers"
    __table_args__ = (
        UniqueConstraint("tenant_id", "phone_number", name="uq_outgoing_caller_number"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    profile_id: Mapped[str] = mapped_column(ForeignKey("outgoing_tenant_profiles.id", ondelete="CASCADE"), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    tenant_slug: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    phone_number: Mapped[str] = mapped_column(String(50), nullable=False)
    label: Mapped[str] = mapped_column(String(255), default="primary", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)

    profile: Mapped["OutgoingTenantProfile"] = relationship(back_populates="numbers")


class OutgoingCall(OutgoingBase):
    __tablename__ = "outgoing_calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    profile_id: Mapped[str | None] = mapped_column(ForeignKey("outgoing_tenant_profiles.id", ondelete="SET NULL"), nullable=True)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    tenant_slug: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    tenant_display_name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    target_number: Mapped[str] = mapped_column(String(50), nullable=False)
    target_name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    from_number: Mapped[str] = mapped_column(String(50), default="", nullable=False)
    opening_phrase: Mapped[str] = mapped_column(Text, default="", nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False, index=True)
    tenant_config_version: Mapped[int] = mapped_column(default=1, nullable=False)
    telnyx_connection_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    telnyx_call_control_id: Mapped[str] = mapped_column(String(255), default="", nullable=False, index=True)
    telnyx_call_leg_id: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    telnyx_call_session_id: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    telnyx_event_type: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    telnyx_hangup_cause: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    livekit_room_name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    transcript_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    transcript_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    extra_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    bridged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    profile: Mapped["OutgoingTenantProfile | None"] = relationship(back_populates="calls")
    events: Mapped[list["OutgoingCallEvent"]] = relationship(back_populates="call", cascade="all, delete-orphan")


class OutgoingCallEvent(OutgoingBase):
    __tablename__ = "outgoing_call_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    outgoing_call_id: Mapped[str | None] = mapped_column(ForeignKey("outgoing_calls.id", ondelete="SET NULL"), nullable=True, index=True)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    tenant_slug: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    telnyx_call_control_id: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    room_name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    call: Mapped["OutgoingCall | None"] = relationship(back_populates="events")
