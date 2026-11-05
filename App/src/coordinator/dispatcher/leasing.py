"""Выдача блоков агентам и закрытие блоков с результатами."""
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.domain.statuses import ChunkStatus, JobStatus
from coordinator.storage.models import Item, Job, RangeChunk

# Атомарная выдача: строка блокируется, параллельные запросы её пропускают
# (SKIP LOCKED), поэтому один блок не достаётся двум агентам (ТЗ, п. 16).
_LEASE_SQL = text("""
    UPDATE range_chunks
       SET status = :leased, leased_by = :worker_id,
           lease_expires_at = now() + make_interval(secs => :ttl),
           attempt = attempt + 1, updated_at = now()
     WHERE id = (
        SELECT c.id
          FROM range_chunks c
          JOIN jobs j ON j.id = c.job_id
         WHERE c.status = :pending AND j.status = :running
         ORDER BY c.job_id, c.range_start
         LIMIT 1
           FOR UPDATE OF c SKIP LOCKED)
    RETURNING id, job_id, range_start, range_end, next_id, lease_expires_at
""")


@dataclass(frozen=True)
class Lease:
    chunk_id: int
    job_id: int
    range_start: int
    range_end: int
    next_id: int
    lease_expires_at: datetime


class LeaseConflict(Exception):
    """Блок не арендован этим узлом — закрывать его нельзя."""


class InvalidResults(Exception):
    """Результаты не соответствуют блоку."""


async def lease_chunk(session: AsyncSession, worker_id: int, ttl: int) -> Lease | None:
    row = (await session.execute(_LEASE_SQL, {
        "leased": ChunkStatus.LEASED, "pending": ChunkStatus.PENDING,
        "running": JobStatus.RUNNING, "worker_id": worker_id, "ttl": ttl,
    })).one_or_none()
    await session.commit()
    return Lease(*row) if row else None


@dataclass(frozen=True)
class ResultRow:
    external_id: int
    status: str
    error_class: str | None = None


async def complete_chunk(
    session: AsyncSession, worker_id: int, chunk_id: int, next_id: int, results: list[ResultRow],
) -> None:
    chunk = await session.get(RangeChunk, chunk_id, with_for_update=True)
    if chunk is None:
        raise LeaseConflict(f"блок {chunk_id} не найден")
    # Блокировка задания сериализует закрытия блоков одного задания, иначе два
    # параллельных complete не увидят друг друга и задание не завершится.
    job = await session.get(Job, chunk.job_id, with_for_update=True)
    assert job is not None

    if chunk.status == ChunkStatus.DONE and chunk.leased_by == worker_id:
        await session.rollback()  # повтор уже принятого закрытия
        return
    if chunk.status != ChunkStatus.LEASED or chunk.leased_by != worker_id:
        raise LeaseConflict(f"блок {chunk_id} не арендован узлом {worker_id}")
    if next_id != chunk.range_end + 1:
        raise InvalidResults(f"complete ожидает next_id={chunk.range_end + 1}, получено {next_id}")
    bad = [r.external_id for r in results
           if not chunk.range_start <= r.external_id <= chunk.range_end]
    if bad:
        raise InvalidResults(f"идентификаторы вне блока: {bad[:5]}")

    if results:
        stmt = insert(Item).values([
            {"job_id": chunk.job_id, "external_id": r.external_id, "status": r.status,
             "error_class": r.error_class}
            for r in results
        ])
        await session.execute(stmt.on_conflict_do_update(
            index_elements=[Item.job_id, Item.external_id],
            set_={"status": stmt.excluded.status, "error_class": stmt.excluded.error_class,
                  "updated_at": func.now()},
        ))

    chunk.next_id = next_id
    chunk.status = ChunkStatus.DONE
    chunk.lease_expires_at = None
    chunk.updated_at = func.now()

    remaining = await session.scalar(
        select(func.count()).select_from(RangeChunk).where(
            RangeChunk.job_id == job.id, RangeChunk.status != ChunkStatus.DONE,
            RangeChunk.id != chunk.id,
        )
    )
    if remaining == 0:
        await session.execute(
            update(Job).where(Job.id == job.id)
            .values(status=JobStatus.COMPLETED, finished_at=func.now())
        )
    await session.commit()
