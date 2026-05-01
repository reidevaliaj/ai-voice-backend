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
    provider: Mapped[str] = mapped_column(String(20), default="telnyx", nullable=False)
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
    summary_notification_targets: Mapped[str] = mapped_column(Text, default="info@cod-st.com", nullable=False)
    summary_from_email: Mapped[str] = mapped_column(String(255), default="info@cod-st.com", nullable=False)
    summary_reply_to_email: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)

    numbers: Mapped[list["OutgoingCallerNumber"]] = relationship(back_populates="profile", cascade="all, delete-orphan")
    calls: Mapped[list["OutgoingCall"]] = relationship(back_populates="profile", cascade="all, delete-orphan")
    prompt_tools: Mapped[list["OutgoingPromptTool"]] = relationship(back_populates="profile", cascade="all, delete-orphan")
    bulk_batches: Mapped[list["OutgoingBulkBatch"]] = relationship(back_populates="profile", cascade="all, delete-orphan")


class OutgoingCallerNumber(OutgoingBase):
    __tablename__ = "outgoing_caller_numbers"
    __table_args__ = (
        UniqueConstraint("tenant_id", "phone_number", name="uq_outgoing_caller_number"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    profile_id: Mapped[str] = mapped_column(ForeignKey("outgoing_tenant_profiles.id", ondelete="CASCADE"), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    tenant_slug: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(20), default="telnyx", nullable=False)
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
    provider: Mapped[str] = mapped_column(String(20), default="telnyx", nullable=False, index=True)
    target_number: Mapped[str] = mapped_column(String(50), nullable=False)
    target_name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    from_number: Mapped[str] = mapped_column(String(50), default="", nullable=False)
    opening_phrase: Mapped[str] = mapped_column(Text, default="", nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False, index=True)
    tenant_config_version: Mapped[int] = mapped_column(default=1, nullable=False)
    provider_call_sid: Mapped[str] = mapped_column(String(255), default="", nullable=False, index=True)
    telnyx_connection_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    telnyx_call_control_id: Mapped[str] = mapped_column(String(255), default="", nullable=False, index=True)
    telnyx_call_leg_id: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    telnyx_call_session_id: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    telnyx_event_type: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    telnyx_hangup_cause: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    twilio_call_sid: Mapped[str] = mapped_column(String(255), default="", nullable=False, index=True)
    twilio_event_type: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    twilio_hangup_cause: Mapped[str] = mapped_column(String(120), default="", nullable=False)
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
    bulk_items: Mapped[list["OutgoingBulkItem"]] = relationship(back_populates="outgoing_call")


class OutgoingPromptTool(OutgoingBase):
    __tablename__ = "outgoing_prompt_tools"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_outgoing_prompt_tool_name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    profile_id: Mapped[str] = mapped_column(ForeignKey("outgoing_tenant_profiles.id", ondelete="CASCADE"), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    tenant_slug: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)

    profile: Mapped["OutgoingTenantProfile"] = relationship(back_populates="prompt_tools")


class OutgoingBulkBatch(OutgoingBase):
    __tablename__ = "outgoing_bulk_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    profile_id: Mapped[str | None] = mapped_column(ForeignKey("outgoing_tenant_profiles.id", ondelete="SET NULL"), nullable=True)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    tenant_slug: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(20), default="telnyx", nullable=False)
    from_number: Mapped[str] = mapped_column(String(50), default="", nullable=False)
    source_filename: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    source_headers_json: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    max_calls: Mapped[int] = mapped_column(default=0, nullable=False)
    delay_seconds: Mapped[int] = mapped_column(default=20, nullable=False)
    total_rows: Mapped[int] = mapped_column(default=0, nullable=False)
    launched_count: Mapped[int] = mapped_column(default=0, nullable=False)
    completed_count: Mapped[int] = mapped_column(default=0, nullable=False)
    failed_count: Mapped[int] = mapped_column(default=0, nullable=False)
    stopped_count: Mapped[int] = mapped_column(default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False, index=True)
    stop_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    extra_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    profile: Mapped["OutgoingTenantProfile | None"] = relationship(back_populates="bulk_batches")
    items: Mapped[list["OutgoingBulkItem"]] = relationship(back_populates="batch", cascade="all, delete-orphan")


class OutgoingBulkItem(OutgoingBase):
    __tablename__ = "outgoing_bulk_items"
    __table_args__ = (
        UniqueConstraint("batch_id", "row_index", name="uq_outgoing_bulk_item_row"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    batch_id: Mapped[str] = mapped_column(ForeignKey("outgoing_bulk_batches.id", ondelete="CASCADE"), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    tenant_slug: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    row_index: Mapped[int] = mapped_column(nullable=False)
    target_number: Mapped[str] = mapped_column(String(50), nullable=False)
    target_name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    row_tags_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    raw_row_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False, index=True)
    outgoing_call_id: Mapped[str | None] = mapped_column(ForeignKey("outgoing_calls.id", ondelete="SET NULL"), nullable=True, index=True)
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)
    launched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    batch: Mapped["OutgoingBulkBatch"] = relationship(back_populates="items")
    outgoing_call: Mapped["OutgoingCall | None"] = relationship(back_populates="bulk_items")


class OutgoingCallEvent(OutgoingBase):
    __tablename__ = "outgoing_call_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    outgoing_call_id: Mapped[str | None] = mapped_column(ForeignKey("outgoing_calls.id", ondelete="SET NULL"), nullable=True, index=True)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    tenant_slug: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(20), default="telnyx", nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    provider_call_sid: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    telnyx_call_control_id: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    room_name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    call: Mapped["OutgoingCall | None"] = relationship(back_populates="events")
