"""drop booking horizon config field

Revision ID: 20260511_0005
Revises: 20260422_0004
Create Date: 2026-05-11 18:10:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260511_0005"
down_revision = "20260422_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("tenant_agent_configs", "booking_horizon_days")


def downgrade() -> None:
    op.add_column(
        "tenant_agent_configs",
        sa.Column("booking_horizon_days", sa.Integer(), nullable=False, server_default="14"),
    )
    op.alter_column("tenant_agent_configs", "booking_horizon_days", server_default=None)
