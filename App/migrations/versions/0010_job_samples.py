"""Снимки прогресса заданий: по ним считаются скорость и оставшееся время.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-05
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "job_samples",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("job_id", sa.Integer, sa.ForeignKey("jobs.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("processed_ids", sa.BigInteger, nullable=False),
        sa.Column("bytes_done", sa.BigInteger, nullable=False),
    )
    op.create_index("ix_job_samples_job_at", "job_samples", ["job_id", "at"])


def downgrade() -> None:
    op.drop_table("job_samples")
