"""Ядро схемы: узлы, задания, блоки диапазона, элементы.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-05
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "workers",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("provisioning_mode", sa.String(16), nullable=False),
        sa.Column("container_id", sa.String(128)),
        sa.Column("image", sa.String(255)),
        sa.Column("image_digest", sa.String(255)),
        sa.Column("cpu_limit", sa.Float),
        sa.Column("mem_limit_mb", sa.Integer),
        sa.Column("threads", sa.Integer, nullable=False),
        sa.Column("storage_path", sa.String(1024)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("agent_version", sa.String(32), nullable=False),
        sa.Column("protocol_version", sa.Integer, nullable=False),
        sa.Column("registered_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("last_heartbeat_at", TZ),
    )
    op.create_index("ix_workers_last_heartbeat_at", "workers", ["last_heartbeat_at"])

    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("range_from", sa.BigInteger, nullable=False),
        sa.Column("range_to", sa.BigInteger, nullable=False),
        sa.Column("chunk_size", sa.Integer, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", TZ),
        sa.Column("finished_at", TZ),
    )
    op.create_index("ix_jobs_status", "jobs", ["status"])

    op.create_table(
        "range_chunks",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("job_id", sa.Integer, sa.ForeignKey("jobs.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("range_start", sa.BigInteger, nullable=False),
        sa.Column("range_end", sa.BigInteger, nullable=False),
        sa.Column("next_id", sa.BigInteger, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("leased_by", sa.Integer, sa.ForeignKey("workers.id", ondelete="SET NULL")),
        sa.Column("lease_expires_at", TZ),
        sa.Column("attempt", sa.Integer, nullable=False, server_default="0"),
        sa.Column("updated_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("job_id", "range_start", "range_end"),
    )
    op.create_index("ix_range_chunks_job_status", "range_chunks", ["job_id", "status"])

    op.create_table(
        "items",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("job_id", sa.Integer, sa.ForeignKey("jobs.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("external_id", sa.BigInteger, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error_class", sa.String(64)),
        sa.Column("updated_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("job_id", "external_id"),
    )


def downgrade() -> None:
    op.drop_table("items")
    op.drop_table("range_chunks")
    op.drop_table("jobs")
    op.drop_table("workers")
