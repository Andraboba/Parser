"""Открытые диапазоны, параметры задания (ТЗ, п. 4) и блоки, создаваемые по мере работы.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-05
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.alter_column("jobs", "range_to", nullable=True)
    op.add_column("jobs", sa.Column("next_chunk_start", sa.BigInteger))
    op.execute("UPDATE jobs SET next_chunk_start = range_to + 1")
    op.alter_column("jobs", "next_chunk_start", nullable=False)
    op.add_column("jobs", sa.Column("threads", sa.Integer))
    op.add_column("jobs", sa.Column("request_timeout_seconds", sa.Float, nullable=False,
                                    server_default="20"))
    op.add_column("jobs", sa.Column("max_retries", sa.Integer, nullable=False, server_default="4"))
    op.add_column("jobs", sa.Column("global_rate_limit_rps", sa.Float))
    op.add_column("jobs", sa.Column("max_ids", sa.BigInteger))
    op.add_column("jobs", sa.Column("max_duration_seconds", sa.Integer))
    op.add_column("jobs", sa.Column("stop_on_consecutive_missing", sa.Integer))
    op.add_column("jobs", sa.Column("finish_reason", sa.String(64)))
    op.add_column("jobs", sa.Column("run_attempt", sa.Integer, nullable=False, server_default="1"))
    op.add_column("jobs", sa.Column("stop_requested_at", TZ))

    op.create_table(
        "job_workers",
        sa.Column("job_id", sa.Integer, sa.ForeignKey("jobs.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("worker_id", sa.Integer, sa.ForeignKey("workers.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("threads", sa.Integer),
    )

    op.add_column("range_chunks", sa.Column("created_at", TZ, nullable=False,
                                            server_default=sa.func.now()))
    op.add_column("range_chunks", sa.Column("leased_at", TZ))
    # Повтор ошибок создаёт новые блоки внутри уже обработанного диапазона.
    op.add_column("range_chunks", sa.Column("retry_round", sa.Integer, nullable=False,
                                            server_default="0"))
    op.add_column("range_chunks", sa.Column("not_before", TZ))
    op.add_column("range_chunks", sa.Column("error_class", sa.String(64)))
    op.drop_constraint("range_chunks_job_id_range_start_range_end_key", "range_chunks")
    op.create_unique_constraint(
        "uq_range_chunks_job_range_round", "range_chunks",
        ["job_id", "range_start", "range_end", "retry_round"],
    )


def downgrade() -> None:
    op.execute("DELETE FROM range_chunks WHERE retry_round > 0")
    op.drop_constraint("uq_range_chunks_job_range_round", "range_chunks")
    op.create_unique_constraint(
        "range_chunks_job_id_range_start_range_end_key", "range_chunks",
        ["job_id", "range_start", "range_end"],
    )
    for column in ("error_class", "not_before", "retry_round", "leased_at", "created_at"):
        op.drop_column("range_chunks", column)
    op.drop_table("job_workers")
    for column in (
        "stop_requested_at", "run_attempt", "finish_reason", "stop_on_consecutive_missing",
        "max_duration_seconds", "max_ids", "global_rate_limit_rps", "max_retries",
        "request_timeout_seconds", "threads", "next_chunk_start",
    ):
        op.drop_column("jobs", column)
    op.execute("DELETE FROM jobs WHERE range_to IS NULL")
    op.alter_column("jobs", "range_to", nullable=False)
