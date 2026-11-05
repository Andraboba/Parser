"""Метрики и настройки узлов, результаты с метаданными и файлами, ключи идемпотентности.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-05
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column("workers", sa.Column("agent_state", sa.String(16)))
    op.add_column("workers", sa.Column("free_slots", sa.Integer))
    op.add_column("workers", sa.Column("metrics", JSONB))
    op.add_column("workers", sa.Column("counters", JSONB))
    op.add_column("workers", sa.Column("last_ip", sa.String(64)))
    # Настройки оператора: число потоков поверх заявленного агентом, отключение узла, резерв места.
    op.add_column("workers", sa.Column("desired_threads", sa.Integer))
    op.add_column("workers", sa.Column("enabled", sa.Boolean, nullable=False,
                                       server_default=sa.true()))
    op.add_column("workers", sa.Column("min_free_space_gb", sa.Float))
    op.add_column("workers", sa.Column("rps_limit", sa.Float))
    # Поля режима provisioning_mode=ssh (ТЗ, п. 4). Сам ключ хранится в менеджере секретов.
    op.add_column("workers", sa.Column("ssh_host", sa.String(255)))
    op.add_column("workers", sa.Column("ssh_port", sa.Integer))
    op.add_column("workers", sa.Column("ssh_user", sa.String(64)))
    op.add_column("workers", sa.Column("ssh_key_ref", sa.String(255)))

    op.create_table(
        "worker_metrics",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("worker_id", sa.Integer, sa.ForeignKey("workers.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("collected_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("cpu_cores", sa.Float),
        sa.Column("cpu_limit", sa.Float),
        sa.Column("memory_bytes", sa.BigInteger),
        sa.Column("memory_limit", sa.BigInteger),
        sa.Column("disk_total_bytes", sa.BigInteger),
        sa.Column("disk_free_bytes", sa.BigInteger),
        sa.Column("load1", sa.Float),
        sa.Column("net_rx_bps", sa.BigInteger),
        sa.Column("net_tx_bps", sa.BigInteger),
        sa.Column("uptime_seconds", sa.Float),
        sa.Column("extra", JSONB),
    )
    op.create_index("ix_worker_metrics_worker_collected", "worker_metrics",
                    ["worker_id", "collected_at"])

    op.add_column("items", sa.Column("worker_id", sa.Integer,
                                     sa.ForeignKey("workers.id", ondelete="SET NULL")))
    op.add_column("items", sa.Column("http_status", sa.Integer))
    op.add_column("items", sa.Column("attempts", sa.Integer, nullable=False, server_default="1"))
    op.add_column("items", sa.Column("created_at", TZ, nullable=False,
                                     server_default=sa.func.now()))
    op.create_index("ix_items_job_status", "items", ["job_id", "status"])

    op.create_table(
        "media_metadata",
        sa.Column("item_id", sa.BigInteger, sa.ForeignKey("items.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("title", sa.Text),
        sa.Column("page_url", sa.Text),
        sa.Column("download_url", sa.Text),
        sa.Column("size", sa.BigInteger),
        sa.Column("mime", sa.String(128)),
        sa.Column("checksum", sa.String(128)),
        sa.Column("discovered_at", TZ, nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "downloads",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("item_id", sa.BigInteger, sa.ForeignKey("items.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("worker_id", sa.Integer, sa.ForeignKey("workers.id", ondelete="SET NULL")),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("bytes_done", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("resumed", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("finished_at", TZ),
        sa.UniqueConstraint("item_id", name="uq_downloads_item"),
    )

    op.create_table(
        "files",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("download_id", sa.BigInteger, sa.ForeignKey("downloads.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("storage_path", sa.String(1024), nullable=False),
        sa.Column("size", sa.BigInteger, nullable=False),
        sa.Column("checksum", sa.String(128)),
        sa.Column("mime", sa.String(128)),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("download_id", "storage_path", name="uq_files_download_path"),
    )

    op.create_table(
        "idempotency_keys",
        sa.Column("worker_id", sa.Integer, sa.ForeignKey("workers.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("key", sa.String(200), primary_key=True),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_idempotency_keys_created", "idempotency_keys", ["created_at"])


def downgrade() -> None:
    op.drop_table("idempotency_keys")
    op.drop_table("files")
    op.drop_table("downloads")
    op.drop_table("media_metadata")
    op.drop_index("ix_items_job_status", "items")
    for column in ("created_at", "attempts", "http_status", "worker_id"):
        op.drop_column("items", column)
    op.drop_table("worker_metrics")
    for column in (
        "ssh_key_ref", "ssh_user", "ssh_port", "ssh_host", "rps_limit", "min_free_space_gb",
        "enabled", "desired_threads", "last_ip", "counters", "metrics", "free_slots",
        "agent_state",
    ):
        op.drop_column("workers", column)
