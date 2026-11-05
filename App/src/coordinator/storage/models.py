from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
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


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    range_from: Mapped[int] = mapped_column(BigInteger)
    range_to: Mapped[int] = mapped_column(BigInteger)
    chunk_size: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), index=True)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(TZ)
    finished_at: Mapped[datetime | None] = mapped_column(TZ)


class RangeChunk(Base):
    __tablename__ = "range_chunks"
    __table_args__ = (
        UniqueConstraint("job_id", "range_start", "range_end"),
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
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())


class Item(Base):
    __tablename__ = "items"
    __table_args__ = (UniqueConstraint("job_id", "external_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    external_id: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16))
    error_class: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
