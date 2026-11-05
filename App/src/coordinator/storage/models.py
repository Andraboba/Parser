from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Все временные метки — timestamptz, хранятся в UTC (ТЗ, п. 18).
TZ = DateTime(timezone=True)


class Base(DeclarativeBase):
    pass


class Worker(Base):
    __tablename__ = "workers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    provisioning_mode: Mapped[str] = mapped_column(String(16), default="docker")
    container_id: Mapped[str | None] = mapped_column(String(128))
    image: Mapped[str | None] = mapped_column(String(255))
    image_digest: Mapped[str | None] = mapped_column(String(255))
    cpu_limit: Mapped[float | None] = mapped_column(Float)
    mem_limit_mb: Mapped[int | None] = mapped_column(Integer)
    threads: Mapped[int] = mapped_column(Integer)
    storage_path: Mapped[str | None] = mapped_column(String(1024))
    status: Mapped[str] = mapped_column(String(16))
    agent_version: Mapped[str] = mapped_column(String(32))
    protocol_version: Mapped[int] = mapped_column(Integer)
    registered_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(TZ, index=True)
    agent_state: Mapped[str | None] = mapped_column(String(16))
    free_slots: Mapped[int | None] = mapped_column(Integer)
    metrics: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    counters: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    last_ip: Mapped[str | None] = mapped_column(String(64))
    # Почему узел сейчас не получает блоки: видно оператору (ТЗ, п. 6.3).
    lease_block_reason: Mapped[str | None] = mapped_column(String(32))
    # Что сообщает runner: состояние, перезапуски, OOM. Пусто — узел вне его управления.
    container_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Настройки оператора: потоки поверх заявленных агентом, отключение узла, резерв места.
    desired_threads: Mapped[int | None] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    min_free_space_gb: Mapped[float | None] = mapped_column(Float)
    rps_limit: Mapped[float | None] = mapped_column(Float)
    # Поля режима provisioning_mode=ssh. Сам ключ хранится в менеджере секретов.
    ssh_host: Mapped[str | None] = mapped_column(String(255))
    ssh_port: Mapped[int | None] = mapped_column(Integer)
    ssh_user: Mapped[str | None] = mapped_column(String(64))
    ssh_key_ref: Mapped[str | None] = mapped_column(String(255))


class WorkerMetric(Base):
    __tablename__ = "worker_metrics"
    __table_args__ = (Index("ix_worker_metrics_worker_collected", "worker_id", "collected_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    worker_id: Mapped[int] = mapped_column(ForeignKey("workers.id", ondelete="CASCADE"))
    collected_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    cpu_cores: Mapped[float | None] = mapped_column(Float)
    cpu_limit: Mapped[float | None] = mapped_column(Float)
    memory_bytes: Mapped[int | None] = mapped_column(BigInteger)
    memory_limit: Mapped[int | None] = mapped_column(BigInteger)
    disk_total_bytes: Mapped[int | None] = mapped_column(BigInteger)
    disk_free_bytes: Mapped[int | None] = mapped_column(BigInteger)
    load1: Mapped[float | None] = mapped_column(Float)
    net_rx_bps: Mapped[int | None] = mapped_column(BigInteger)
    net_tx_bps: Mapped[int | None] = mapped_column(BigInteger)
    uptime_seconds: Mapped[float | None] = mapped_column(Float)
    extra: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    counters: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    login: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    disabled_at: Mapped[datetime | None] = mapped_column(TZ)


class RoleRow(Base):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(32), unique=True)


class UserRole(Base):
    __tablename__ = "user_roles"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True,
    )
    role_id: Mapped[int] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True,
    )


class SessionRow(Base):
    __tablename__ = "sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(TZ, index=True)


class AuditEntry(Base):
    """Только добавление: изменение и удаление запрещены триггером в базе (раздел 4 плана)."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    at: Mapped[datetime] = mapped_column(TZ, server_default=func.now(), index=True)
    actor: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(128))
    object: Mapped[str | None] = mapped_column(String(255))
    result: Mapped[str] = mapped_column(String(32))
    ip: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_job_at", "job_id", "at"),
        Index("ix_events_at", "at"),
        Index("ix_events_worker_at", "worker_id", "at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    level: Mapped[str] = mapped_column(String(16))
    component: Mapped[str] = mapped_column(String(32))
    job_id: Mapped[int | None] = mapped_column(Integer)
    worker_id: Mapped[int | None] = mapped_column(Integer)
    chunk_id: Mapped[int | None] = mapped_column(BigInteger)
    external_id: Mapped[int | None] = mapped_column(BigInteger)
    operation: Mapped[str] = mapped_column(String(64))
    result: Mapped[str] = mapped_column(String(32))
    error_code: Mapped[str | None] = mapped_column(String(64))
    message: Mapped[str | None] = mapped_column(Text)


class AgentToken(Base):
    __tablename__ = "agent_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Пустое имя — общий токен парка: им может представиться любой узел.
    worker_name: Mapped[str | None] = mapped_column(String(100))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(TZ)
    revoked_at: Mapped[datetime | None] = mapped_column(TZ)

    def allows(self, name: str) -> bool:
        return self.worker_name is None or self.worker_name == name


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    range_from: Mapped[int] = mapped_column(BigInteger)
    # Пустая верхняя граница — диапазон без конца (ТЗ, п. 3 и 10).
    range_to: Mapped[int | None] = mapped_column(BigInteger)
    chunk_size: Mapped[int] = mapped_column(Integer)
    # Первый идентификатор, для которого блок ещё не создан.
    next_chunk_start: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), index=True)
    threads: Mapped[int | None] = mapped_column(Integer)
    request_timeout_seconds: Mapped[float] = mapped_column(Float, default=20.0)
    max_retries: Mapped[int] = mapped_column(Integer, default=4)
    global_rate_limit_rps: Mapped[float | None] = mapped_column(Float)
    max_ids: Mapped[int | None] = mapped_column(BigInteger)
    max_duration_seconds: Mapped[int | None] = mapped_column(Integer)
    stop_on_consecutive_missing: Mapped[int | None] = mapped_column(Integer)
    finish_reason: Mapped[str | None] = mapped_column(String(64))
    run_attempt: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(TZ)
    finished_at: Mapped[datetime | None] = mapped_column(TZ)
    stop_requested_at: Mapped[datetime | None] = mapped_column(TZ)
    no_workers_since: Mapped[datetime | None] = mapped_column(TZ)


class JobSample(Base):
    """Снимок прогресса: по разнице снимков считаются скорость и оставшееся время."""

    __tablename__ = "job_samples"
    __table_args__ = (Index("ix_job_samples_job_at", "job_id", "at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    processed_ids: Mapped[int] = mapped_column(BigInteger)
    bytes_done: Mapped[int] = mapped_column(BigInteger)


class JobWorker(Base):
    """Узлы задания и индивидуальное число потоков. Нет строк — задание для всех узлов."""

    __tablename__ = "job_workers"

    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    worker_id: Mapped[int] = mapped_column(
        ForeignKey("workers.id", ondelete="CASCADE"), primary_key=True,
    )
    threads: Mapped[int | None] = mapped_column(Integer)


class RangeChunk(Base):
    __tablename__ = "range_chunks"
    __table_args__ = (
        UniqueConstraint(
            "job_id", "range_start", "range_end", "retry_round",
            name="uq_range_chunks_job_range_round",
        ),
        Index("ix_range_chunks_job_status", "job_id", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    range_start: Mapped[int] = mapped_column(BigInteger)
    range_end: Mapped[int] = mapped_column(BigInteger)
    # Контрольная точка: первый ещё не обработанный идентификатор (ТЗ, п. 17).
    next_id: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16))
    leased_by: Mapped[int | None] = mapped_column(ForeignKey("workers.id", ondelete="SET NULL"))
    lease_expires_at: Mapped[datetime | None] = mapped_column(TZ)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    # 0 — основной проход, больше нуля — блоки повтора ошибок (тикет 08).
    retry_round: Mapped[int] = mapped_column(Integer, default=0)
    not_before: Mapped[datetime | None] = mapped_column(TZ)
    error_class: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    leased_at: Mapped[datetime | None] = mapped_column(TZ)
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())


class Item(Base):
    __tablename__ = "items"
    __table_args__ = (
        UniqueConstraint("job_id", "external_id"),
        Index("ix_items_job_status", "job_id", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    external_id: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16))
    error_class: Mapped[str | None] = mapped_column(String(64))
    worker_id: Mapped[int | None] = mapped_column(ForeignKey("workers.id", ondelete="SET NULL"))
    http_status: Mapped[int | None] = mapped_column(Integer)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())


class MediaMetadata(Base):
    __tablename__ = "media_metadata"

    item_id: Mapped[int] = mapped_column(
        ForeignKey("items.id", ondelete="CASCADE"), primary_key=True,
    )
    title: Mapped[str | None] = mapped_column(Text)
    page_url: Mapped[str | None] = mapped_column(Text)
    download_url: Mapped[str | None] = mapped_column(Text)
    size: Mapped[int | None] = mapped_column(BigInteger)
    mime: Mapped[str | None] = mapped_column(String(128))
    checksum: Mapped[str | None] = mapped_column(String(128))
    discovered_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())


class Download(Base):
    __tablename__ = "downloads"
    __table_args__ = (UniqueConstraint("item_id", name="uq_downloads_item"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"))
    worker_id: Mapped[int | None] = mapped_column(ForeignKey("workers.id", ondelete="SET NULL"))
    state: Mapped[str] = mapped_column(String(16))
    bytes_done: Mapped[int] = mapped_column(BigInteger, default=0)
    resumed: Mapped[bool] = mapped_column(Boolean, default=False)
    finished_at: Mapped[datetime | None] = mapped_column(TZ)


class File(Base):
    __tablename__ = "files"
    __table_args__ = (
        UniqueConstraint("download_id", "storage_path", name="uq_files_download_path"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    download_id: Mapped[int] = mapped_column(ForeignKey("downloads.id", ondelete="CASCADE"))
    storage_path: Mapped[str] = mapped_column(String(1024))
    size: Mapped[int] = mapped_column(BigInteger)
    checksum: Mapped[str | None] = mapped_column(String(128))
    mime: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    worker_id: Mapped[int] = mapped_column(
        ForeignKey("workers.id", ondelete="CASCADE"), primary_key=True,
    )
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now(), index=True)
