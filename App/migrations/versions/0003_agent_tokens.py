"""Токены агентов и статусы элементов из ТЗ, п. 7.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-05
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "agent_tokens",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("worker_name", sa.String(100)),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", TZ),
        sa.Column("revoked_at", TZ),
    )
    op.execute("UPDATE items SET status = 'failed' WHERE status = 'error'")


def downgrade() -> None:
    op.execute("UPDATE items SET status = 'error' WHERE status = 'failed'")
    op.drop_table("agent_tokens")
