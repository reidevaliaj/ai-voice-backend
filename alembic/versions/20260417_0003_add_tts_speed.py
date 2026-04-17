"""add tenant tts speed

Revision ID: 20260417_0003
Revises: 20260417_0002
Create Date: 2026-04-17 19:05:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260417_0003"
down_revision = "20260417_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant_agent_configs",
        sa.Column("tts_speed", sa.Float(), nullable=False, server_default="1.0"),
    )


def downgrade() -> None:
    op.drop_column("tenant_agent_configs", "tts_speed")
