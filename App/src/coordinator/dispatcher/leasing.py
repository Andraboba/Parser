"""Аренда блоков: выдача, продление, прогресс, закрытие, отказ и возврат в очередь."""
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import exists, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.domain.retry import CRITICAL_ERROR_CLASSES, retry_delay
from coordinator.domain.statuses import ChunkStatus, JobAction, JobStatus
from coordinator.errors import InvalidResults, LeaseConflict
from coordinator.storage import events
from coordinator.storage.jobs import add_chunks, apply_action, is_exhausted, lock_job, settle_job
from coordinator.storage.models import Job, JobWorker, RangeChunk, Worker
from coordinator.storage.results import ResultRow, claim_idempotency_key, save_results

# Атомарная выдача: строка блокируется, параллельные запросы её пропускают
# (SKIP LOCKED), поэтому один блок не достаётся двум агентам (ТЗ, п. 16).
_LEASE_SQL = text("""
    UPDATE range_chunks
       SET status = :leased, leased_by = :worker_id, leased_at = now(),
           lease_expires_at = now() + make_interval(secs => :ttl),
           attempt = attempt + 1, updated_at = now()
     WHERE id = (
        SELECT c.id
          FROM range_chunks c
          JOIN jobs j ON j.id = c.job_id
         WHERE c.status = :pending AND j.status = :running
           AND (c.not_before IS NULL OR c.not_before <= now())
           AND (NOT EXISTS (SELECT 1 FROM job_workers w WHERE w.job_id = j.id)
                OR EXISTS (SELECT 1 FROM job_workers w
                            WHERE w.job_id = j.id AND w.worker_id = :worker_id))
         ORDER BY c.job_id, c.retry_round, c.range_start
         LIMIT 1
           FOR UPDATE OF c SKIP LOCKED)
    RETURNING id, job_id, range_start, range_end, next_id, lease_expires_at, attempt
""")


@dataclass(frozen=True)
class Lease:
    chunk_id: int
    job_id: int
    range_start: int
    range_end: int
    next_id: int
    lease_expires_at: datetime
    attempt: int


async def lease_chunk(session: AsyncSession, worker_id: int, ttl: int) -> Lease | None:
    lease = await _try_lease(session, worker_id, ttl)
    if lease is None and await _top_up_chunks(session, worker_id):
        lease = await _try_lease(session, worker_id, ttl)
    return lease


async def _try_lease(session: AsyncSession, worker_id: int, ttl: int) -> Lease | None:
    row = (await session.execute(_LEASE_SQL, {
        "leased": ChunkStatus.LEASED, "pending": ChunkStatus.PENDING,
        "running": JobStatus.RUNNING, "worker_id": worker_id, "ttl": ttl,
    })).one_or_none()
    if row is None:
        await session.commit()
        return None
    lease = Lease(*row)
    await events.emit(
        session, "chunk.lease", "leased",
        f"блок {lease.range_start}..{lease.range_end}, с {lease.next_id}, попытка {lease.attempt}",
        job_id=lease.job_id, worker_id=worker_id, chunk_id=lease.chunk_id,
    )
    await session.commit()
    return lease


async def _top_up_chunks(session: AsyncSession, worker_id: int) -> bool:
    """Очередь пуста: создаёт новые блоки для первого подходящего задания.

    Задание, которое уже пополняет другой запрос, пропускается: в худшем случае
    узел получит 204 и спросит снова, зато выдача не ждёт чужую транзакцию.
    """
    assigned = exists().where(JobWorker.job_id == Job.id)
    mine = exists().where(JobWorker.job_id == Job.id, JobWorker.worker_id == worker_id)
    candidates = await session.scalars(
        select(Job).where(Job.status == JobStatus.RUNNING, or_(~assigned, mine))
        .order_by(Job.id).with_for_update(of=Job, skip_locked=True)
    )
    for job in candidates:
        if not is_exhausted(job) and await add_chunks(session, job):
            await session.commit()
            return True
    await session.rollback()
    return False


async def release_worker_leases(session: AsyncSession, worker_id: int) -> None:
    """Агент зарегистрировался заново: прежний запуск блоки уже не доделает, контрольные
    точки сохранены. Ждать истечения аренды незачем."""
    await session.execute(
        update(RangeChunk)
        .where(RangeChunk.leased_by == worker_id, RangeChunk.status == ChunkStatus.LEASED)
        .values(status=ChunkStatus.PENDING, lease_expires_at=None, updated_at=func.now())
    )


async def renew_leases(
    session: AsyncSession, worker_id: int, ttl: int, active: list[int] | None = None,
    grace_seconds: int = 0,
) -> list[int]:
    """Heartbeat продлевает аренду. Возвращает блоки, которые агенту надо прекратить.

    Агент сообщает, над чем работает. Блок, про который он не знает, возвращается в очередь:
    иначе потерянный из-за сбоя блок продлевался бы вечно, пока узел жив. Только что выданный
    блок не трогаем: heartbeat мог уйти раньше, чем агент получил ответ на lease.
    """
    if active is not None:
        await session.execute(
            update(RangeChunk)
            .where(
                RangeChunk.leased_by == worker_id, RangeChunk.status == ChunkStatus.LEASED,
                RangeChunk.id.not_in(active),
                RangeChunk.leased_at < func.now() - timedelta(seconds=grace_seconds),
            )
            .values(status=ChunkStatus.PENDING, lease_expires_at=None, updated_at=func.now())
        )
    await session.execute(
        update(RangeChunk)
        .where(RangeChunk.leased_by == worker_id, RangeChunk.status == ChunkStatus.LEASED)
        .values(lease_expires_at=func.now() + timedelta(seconds=ttl))
    )
    return list(await session.scalars(
        select(RangeChunk.id).join(Job, Job.id == RangeChunk.job_id).where(
            RangeChunk.leased_by == worker_id, RangeChunk.status == ChunkStatus.LEASED,
            Job.status == JobStatus.STOPPING,
        )
    ))


async def active_job(session: AsyncSession, worker_id: int) -> int | None:
    """Задание, над которым узел работает сейчас: от него зависит число потоков."""
    return await session.scalar(
        select(RangeChunk.job_id)
        .where(RangeChunk.leased_by == worker_id, RangeChunk.status == ChunkStatus.LEASED)
        .order_by(RangeChunk.leased_at.desc()).limit(1)
    )


async def _lock_chunk(session: AsyncSession, chunk_id: int) -> RangeChunk:
    chunk = await session.get(RangeChunk, chunk_id, with_for_update=True)
    if chunk is None:
        raise LeaseConflict(f"блок {chunk_id} не найден")
    return chunk


def _require_owner(chunk: RangeChunk, worker_id: int) -> None:
    """Закончившаяся аренда не повод терять работу, если блок никто не забрал: результаты
    идемпотентны, а узел мог просто не успеть из-за сбоя сети или координатора."""
    if chunk.leased_by != worker_id or chunk.status == ChunkStatus.DONE:
        raise LeaseConflict(f"блок {chunk.id} не арендован узлом {worker_id}")


def _check_results(chunk: RangeChunk, next_id: int, results: list[ResultRow]) -> None:
    if not chunk.range_start <= next_id <= chunk.range_end + 1:
        raise InvalidResults(
            f"next_id={next_id} вне блока {chunk.range_start}..{chunk.range_end + 1}"
        )
    bad = [r.external_id for r in results
           if not chunk.range_start <= r.external_id <= chunk.range_end]
    if bad:
        raise InvalidResults(f"идентификаторы вне блока: {bad[:5]}")


async def _report_rejected(
    session: AsyncSession, chunk: RangeChunk, worker: Worker, rejected: list[int],
) -> None:
    for external_id in rejected:
        await events.emit(
            session, "file.rejected", "rejected", "путь к файлу вне каталога хранения узла",
            level="error", job_id=chunk.job_id, worker_id=worker.id, chunk_id=chunk.id,
            external_id=external_id, error_code="invalid_path",
        )


@dataclass(frozen=True)
class ProgressOutcome:
    cancel: bool
    rejected: list[int]


async def record_progress(
    session: AsyncSession, worker: Worker, chunk_id: int, next_id: int,
    results: list[ResultRow], ttl: int,
) -> ProgressOutcome:
    chunk = await _lock_chunk(session, chunk_id)
    _require_owner(chunk, worker.id)
    _check_results(chunk, next_id, results)
    rejected = await save_results(session, chunk.job_id, worker, results)
    chunk.next_id = max(chunk.next_id, next_id)
    job = await session.get(Job, chunk.job_id, populate_existing=True)
    assert job is not None
    cancel = job.status != JobStatus.RUNNING and job.status != JobStatus.PAUSING
    if chunk.status == ChunkStatus.LEASED or not cancel:
        chunk.status = ChunkStatus.LEASED
        chunk.lease_expires_at = func.now() + timedelta(seconds=ttl)
    chunk.updated_at = func.now()
    await _report_rejected(session, chunk, worker, rejected)
    await session.commit()
    return ProgressOutcome(cancel=cancel, rejected=rejected)


@dataclass(frozen=True)
class CompleteOutcome:
    rejected: list[int]


async def complete_chunk(
    session: AsyncSession, worker: Worker, chunk_id: int, next_id: int,
    results: list[ResultRow], idempotency_key: str | None = None,
) -> CompleteOutcome:
    """Закрывает блок. Если next_id не дошёл до конца, блок возвращается в очередь с контрольной
    точкой: так агент отдаёт недоделанную работу при остановке (ТЗ, п. 17)."""
    chunk = await _lock_chunk(session, chunk_id)
    # Блокировка задания сериализует закрытия блоков одного задания, иначе два
    # параллельных complete не увидят друг друга и задание не завершится.
    job = await lock_job(session, chunk.job_id)
    if chunk.status == ChunkStatus.DONE and chunk.leased_by == worker.id:
        await session.rollback()  # повтор уже принятого закрытия
        return CompleteOutcome(rejected=[])
    _require_owner(chunk, worker.id)
    _check_results(chunk, next_id, results)
    if idempotency_key and not await claim_idempotency_key(
        session, worker.id, f"{idempotency_key}#{chunk.attempt}",
    ):
        await session.rollback()
        return CompleteOutcome(rejected=[])

    rejected = await save_results(session, chunk.job_id, worker, results)
    chunk.next_id = max(chunk.next_id, next_id)
    chunk.status = (
        ChunkStatus.DONE if chunk.next_id > chunk.range_end else ChunkStatus.PENDING
    )
    chunk.lease_expires_at = None
    chunk.updated_at = func.now()
    await events.emit(
        session, "chunk.complete", chunk.status, f"контрольная точка {chunk.next_id}",
        job_id=chunk.job_id, worker_id=worker.id, chunk_id=chunk.id,
    )
    await _report_rejected(session, chunk, worker, rejected)
    await settle_job(session, job)
    await session.commit()
    return CompleteOutcome(rejected=rejected)


async def fail_chunk(
    session: AsyncSession, worker: Worker, chunk_id: int, error_class: str, next_id: int | None,
    max_attempts: int, idempotency_key: str | None = None, message: str = "",
) -> None:
    """Отказ агента от блока (ТЗ, п. 16): повтор с задержкой, после исчерпания попыток — failed."""
    chunk = await _lock_chunk(session, chunk_id)
    job = await lock_job(session, chunk.job_id)
    _require_owner(chunk, worker.id)
    # В ключ агента не входит номер попытки, а один блок может отказать несколько раз подряд.
    if idempotency_key and not await claim_idempotency_key(
        session, worker.id, f"{idempotency_key}#{chunk.attempt}",
    ):
        await session.rollback()
        return
    if next_id is not None:
        _check_results(chunk, next_id, [])
        chunk.next_id = max(chunk.next_id, next_id)

    chunk.error_class = error_class
    chunk.lease_expires_at = None
    chunk.updated_at = func.now()
    if error_class in CRITICAL_ERROR_CLASSES:
        chunk.status = ChunkStatus.FAILED
        if job.status in (JobStatus.RUNNING, JobStatus.PAUSING):
            apply_action(job, JobAction.FAIL)
            job.finished_at = func.now()
            job.finish_reason = error_class
    elif chunk.attempt >= max_attempts:
        chunk.status = ChunkStatus.FAILED
    else:
        chunk.status = ChunkStatus.PENDING
        chunk.not_before = func.now() + timedelta(seconds=retry_delay(chunk.attempt - 1))
    await events.emit(
        session, "chunk.fail", chunk.status, message or None, level="warning",
        job_id=chunk.job_id, worker_id=worker.id, chunk_id=chunk.id, error_code=error_class,
    )
    await settle_job(session, job)
    await session.commit()
