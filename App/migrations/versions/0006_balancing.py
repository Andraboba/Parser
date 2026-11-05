"""Причина отказа в выдаче блока и счётчики узла во временном ряду.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-05
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("workers", sa.Column("lease_block_reason", sa.String(32)))
    op.add_column("worker_metrics", sa.Column("counters", JSONB))


def downgrade() -> None:
    op.drop_column("worker_metrics", "counters")
    op.drop_column("workers", "lease_block_reason")
