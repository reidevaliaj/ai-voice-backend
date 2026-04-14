"""initial multitenant control plane"""

from alembic import op
import sqlalchemy as sa


revision = "20260415_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_users",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=512), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_table(
        "tenant_phone_numbers",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("phone_number", sa.String(length=50), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("phone_number"),
    )
    op.create_table(
        "tenant_agent_configs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("business_name", sa.String(length=255), nullable=False),
        sa.Column("timezone", sa.String(length=100), nullable=False),
        sa.Column("greeting", sa.Text(), nullable=False),
        sa.Column("services", sa.JSON(), nullable=False),
        sa.Column("faq_notes", sa.Text(), nullable=False),
        sa.Column("prompt_appendix", sa.Text(), nullable=False),
        sa.Column("business_hours", sa.String(length=50), nullable=False),
        sa.Column("business_days", sa.String(length=50), nullable=False),
        sa.Column("meeting_duration_minutes", sa.Integer(), nullable=False),
        sa.Column("booking_horizon_days", sa.Integer(), nullable=False),
        sa.Column("enabled_tools", sa.JSON(), nullable=False),
        sa.Column("llm_model", sa.String(length=100), nullable=False),
        sa.Column("tts_voice", sa.String(length=255), nullable=False),
        sa.Column("owner_name", sa.String(length=255), nullable=False),
        sa.Column("owner_email", sa.String(length=320), nullable=False),
        sa.Column("reply_to_email", sa.String(length=320), nullable=False),
        sa.Column("from_email", sa.String(length=320), nullable=False),
        sa.Column("notification_targets", sa.JSON(), nullable=False),
        sa.Column("extra_settings", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "version", name="uq_tenant_agent_config_version"),
    )
    op.create_table(
        "tenant_integrations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("credentials_encrypted", sa.Text(), nullable=False),
        sa.Column("settings_json", sa.JSON(), nullable=False),
        sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "provider", name="uq_tenant_integration_provider"),
    )
    op.create_table(
        "call_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=True),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("call_sid", sa.String(length=64), nullable=False),
        sa.Column("parent_call_sid", sa.String(length=64), nullable=False),
        sa.Column("sip_call_id", sa.String(length=255), nullable=False),
        sa.Column("room_name", sa.String(length=255), nullable=False),
        sa.Column("call_status", sa.String(length=50), nullable=False),
        sa.Column("sip_response_code", sa.String(length=20), nullable=False),
        sa.Column("caller_number", sa.String(length=50), nullable=False),
        sa.Column("called_number", sa.String(length=50), nullable=False),
        sa.Column("callback_timestamp", sa.String(length=100), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "call_config_snapshots",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("room_name", sa.String(length=255), nullable=False),
        sa.Column("call_sid", sa.String(length=64), nullable=False),
        sa.Column("caller_id", sa.String(length=100), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("snapshot_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_call_config_snapshots_room_name", "call_config_snapshots", ["room_name"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_call_config_snapshots_room_name", table_name="call_config_snapshots")
    op.drop_table("call_config_snapshots")
    op.drop_table("call_events")
    op.drop_table("tenant_integrations")
    op.drop_table("tenant_agent_configs")
    op.drop_table("tenant_phone_numbers")
    op.drop_table("tenants")
    op.drop_table("admin_users")
