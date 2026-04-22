"""add stt language and endpointing controls

Revision ID: 20260422_0004
Revises: 20260417_0003
Create Date: 2026-04-22 17:05:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260422_0004"
down_revision = "20260417_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant_agent_configs",
        sa.Column("stt_language", sa.String(length=10), nullable=False, server_default="en"),
    )
    op.add_column(
        "tenant_agent_configs",
        sa.Column("min_endpointing_delay", sa.Float(), nullable=False, server_default="0.3"),
    )
    op.add_column(
        "tenant_agent_configs",
        sa.Column("max_endpointing_delay", sa.Float(), nullable=False, server_default="1.2"),
    )

    op.execute(
        """
        UPDATE tenant_agent_configs
        SET stt_language = COALESCE(NULLIF(assistant_language, ''), 'en')
        """
    )
    op.execute(
        """
        UPDATE tenant_agent_configs
        SET min_endpointing_delay = 0.3,
            max_endpointing_delay = 1.2
        WHERE min_endpointing_delay IS NULL OR max_endpointing_delay IS NULL
        """
    )

    op.alter_column("tenant_agent_configs", "stt_language", server_default=None)
    op.alter_column("tenant_agent_configs", "min_endpointing_delay", server_default=None)
    op.alter_column("tenant_agent_configs", "max_endpointing_delay", server_default=None)


def downgrade() -> None:
    op.drop_column("tenant_agent_configs", "max_endpointing_delay")
    op.drop_column("tenant_agent_configs", "min_endpointing_delay")
    op.drop_column("tenant_agent_configs", "stt_language")
