"""add tenant prompt and language

Revision ID: 20260417_0002
Revises: 20260415_0001
Create Date: 2026-04-17 17:20:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260417_0002"
down_revision = "20260415_0001"
branch_labels = None
depends_on = None


DEFAULT_TENANT_PROMPT = (
    "You are the receptionist for this business. Help callers understand the business, "
    "answer only with the configured services and notes, collect accurate details, and "
    "guide the caller to the next useful step."
)


def upgrade() -> None:
    op.add_column(
        "tenant_agent_configs",
        sa.Column("assistant_language", sa.String(length=10), nullable=False, server_default="en"),
    )
    op.add_column(
        "tenant_agent_configs",
        sa.Column("tenant_prompt", sa.Text(), nullable=False, server_default=DEFAULT_TENANT_PROMPT),
    )


def downgrade() -> None:
    op.drop_column("tenant_agent_configs", "tenant_prompt")
    op.drop_column("tenant_agent_configs", "assistant_language")
