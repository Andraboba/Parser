"""Агенты на серверах по SSH: какой агент ставить и должен ли он работать.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-05
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("workers", sa.Column("agent_kind", sa.String(16)))
    op.add_column("workers", sa.Column("desired_agent_state", sa.String(16)))


def downgrade() -> None:
    op.drop_column("workers", "desired_agent_state")
    op.drop_column("workers", "agent_kind")
