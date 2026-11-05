"""Состояние контейнера узла по данным runner-а.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-05
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("workers", sa.Column("container_state", JSONB))


def downgrade() -> None:
    op.drop_column("workers", "container_state")
