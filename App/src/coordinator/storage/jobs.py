"""Создание заданий, управление их жизненным циклом и чтение состояния для интерфейса."""
from dataclasses import dataclass
from itertools import batched

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.domain.chunks import chunks_needed, next_batch, split_range
from coordinator.domain.ranges import RangeSpec
from coordinator.domain.retry import RETRYABLE_ITEM_STATUSES
from coordinator.domain.statuses import ChunkStatus, ItemStatus, JobAction, JobStatus, next_status
from coordinator.errors import InvalidJob, JobNotFound, JobStateConflict
from coordinator.storage import events
from coordinator.storage.models import Item, Job, JobWorker, RangeChunk, Worker

# Сколько блоков создавать за раз: небольшая очередь, новые появятся по мере разбора.
CHUNK_BATCH = 50


@dataclass(frozen=True)
class JobParams:
    """Параметры задания из ТЗ, п. 4. Значения по умолчанию заданы в схеме API."""

    range: RangeSpec
    chunk_size: int
    threads: int | None = None
    request_timeout_seconds: float = 20.0
    max_retries: int = 4
    global_rate_limit_rps: float | None = None
    max_ids: int | None = None
    max_duration_seconds: int | None = None
    stop_on_consecutive_missing: int | None = None
    worker_threads: tuple[tuple[str, int | None], ...] = ()


async def create_job(session: AsyncSession, params: JobParams) -> int:
    job = Job(
        range_from=params.range.start, range_to=params.range.end, chunk_size=params.chunk_size,
        next_chunk_start=params.range.start, status=JobStatus.CREATED, threads=params.threads,
        request_timeout_seconds=params.request_timeout_seconds, max_retries=params.max_retries,
        global_rate_limit_rps=params.global_rate_limit_rps, max_ids=params.max_ids,
        max_duration_seconds=params.max_duration_seconds,
        stop_on_consecutive_missing=params.stop_on_consecutive_missing,
    )
    session.add(job)
    await session.flush()
    await _assign_workers(session, job.id, params.worker_threads)
    await session.commit()
    return job.id


async def _assign_workers(
    session: AsyncSession, job_id: int, worker_threads: tuple[tuple[str, int | None], ...],
) -> None:
    if not worker_threads:
        return
    names = [name for name, _ in worker_threads]
    ids = dict((await session.execute(
        select(Worker.name, Worker.id).where(Worker.name.in_(names))
    )).all())
    unknown = sorted(set(names) - set(ids))
    if unknown:
        raise InvalidJob(f"неизвестные узлы: {', '.join(unknown)}")
    await session.execute(insert(JobWorker), [
        {"job_id": job_id, "worker_id": ids[name], "threads": threads}
        for name, threads in worker_threads
    ])


def effective_end(job: Job) -> int | None:
    """Последний идентификатор с учётом предела max_ids (ТЗ, п. 10). None — границы нет."""
    limit = None if job.max_ids is None else job.range_from + job.max_ids - 1
    if job.range_to is None:
        return limit
    return job.range_to if limit is None else min(job.range_to, limit)


def is_exhausted(job: Job) -> bool:
    """Для всего диапазона уже созданы блоки."""
    end = effective_end(job)
    return end is not None and job.next_chunk_start > end


async def add_chunks(session: AsyncSession, job: Job, count: int = CHUNK_BATCH) -> int:
    """Создаёт следующую порцию блоков. Вызывающий держит блокировку строки задания."""
    bounds = next_batch(job.next_chunk_start, effective_end(job), job.chunk_size, count)
    if not bounds:
        return 0
    await session.execute(insert(RangeChunk), [
        {"job_id": job.id, "range_start": b.start, "range_end": b.end, "next_id": b.start,
         "status": ChunkStatus.PENDING, "attempt": 0}
        for b in bounds
    ])
    job.next_chunk_start = bounds[-1].end + 1
    return len(bounds)


async def lock_job(session: AsyncSession, job_id: int) -> Job:
    job = await session.get(Job, job_id, with_for_update=True, populate_existing=True)
    if job is None:
        raise JobNotFound(f"задание {job_id} не найдено")
    return job


def apply_action(job: Job, action: JobAction) -> None:
    status = next_status(job.status, action)
    if status is None:
        raise JobStateConflict(
            f"задание {job.id} в статусе {job.status}, действие {action.value} недоступно"
        )
    job.status = status


async def apply_settings(session: AsyncSession, job_id: int, changes: dict[str, object]) -> None:
    """Смена потоков и лимитов на ходу. Обязательные поля пустыми быть не могут."""
    job = await lock_job(session, job_id)
    for field, value in changes.items():
        if value is None and field in ("request_timeout_seconds", "max_retries"):
            continue
        setattr(job, field, value)
    await session.commit()


async def start_job(session: AsyncSession, job_id: int) -> None:
    job = await lock_job(session, job_id)
    apply_action(job, JobAction.START)
    apply_action(job, JobAction.VALIDATED)
    job.started_at = func.now()
    await add_chunks(session, job)
    await events.emit(session, "job.start", job.status, "задание запущено", job_id=job.id)
    await session.commit()


async def pause_job(session: AsyncSession, job_id: int) -> None:
    job = await lock_job(session, job_id)
    apply_action(job, JobAction.PAUSE)
    await events.emit(session, "job.pause", job.status, "мягкая пауза", job_id=job.id)
    await settle_job(session, job)
    await session.commit()


async def resume_job(session: AsyncSession, job_id: int) -> None:
    job = await lock_job(session, job_id)
    apply_action(job, JobAction.RESUME)
    await events.emit(session, "job.resume", job.status, "задание возобновлено", job_id=job.id)
    await session.commit()


async def stop_job(session: AsyncSession, job_id: int) -> None:
    job = await lock_job(session, job_id)
    apply_action(job, JobAction.STOP)
    job.stop_requested_at = func.now()
    await events.emit(session, "job.stop", job.status, "немедленная остановка", job_id=job.id)
    await settle_job(session, job)
    await session.commit()


async def restart_job(session: AsyncSession, job_id: int) -> None:
    """Новая попытка: контрольные точки сохраняются, упавшие блоки снова идут в очередь."""
    job = await lock_job(session, job_id)
    apply_action(job, JobAction.RESTART)
    job.run_attempt += 1
    job.finished_at = None
    job.finish_reason = None
    job.stop_requested_at = None
    await session.execute(
        update(RangeChunk)
        .where(RangeChunk.job_id == job_id, RangeChunk.status == ChunkStatus.FAILED)
        .values(status=ChunkStatus.PENDING, attempt=0, not_before=None, error_class=None,
                updated_at=func.now())
    )
    await events.emit(
        session, "job.restart", job.status, f"новая попытка № {job.run_attempt}", job_id=job.id,
    )
    await settle_job(session, job)
    await session.commit()


_ERROR_ISLANDS_SQL = text("""
    SELECT min(external_id), max(external_id)
      FROM (SELECT external_id,
                   external_id - row_number() OVER (ORDER BY external_id) AS island
              FROM items
             WHERE job_id = :job_id AND status = ANY(:statuses)) AS numbered
     GROUP BY island
     ORDER BY 1
""")


@dataclass(frozen=True)
class RetryOutcome:
    items_requeued: int
    chunks_created: int
    chunks_requeued: int


async def retry_errors(session: AsyncSession, job_id: int) -> RetryOutcome:
    """Создаёт блоки только для элементов с ошибками: подряд идущие идентификаторы — один блок.
    Завершённое задание возвращается в работу как новая попытка (ТЗ, п. 19)."""
    job = await lock_job(session, job_id)
    if job.status == JobStatus.CREATED:
        raise JobStateConflict(f"задание {job_id} ещё не запускалось")
    islands = (await session.execute(_ERROR_ISLANDS_SQL, {
        "job_id": job_id, "statuses": list(RETRYABLE_ITEM_STATUSES),
    })).all()
    retry_round = (await session.scalar(
        select(func.coalesce(func.max(RangeChunk.retry_round), 0))
        .where(RangeChunk.job_id == job_id)
    )) + 1
    bounds = [b for low, high in islands for b in split_range(low, high, job.chunk_size)]
    for part in batched(bounds, 1000, strict=False):
        await session.execute(insert(RangeChunk), [
            {"job_id": job_id, "range_start": b.start, "range_end": b.end, "next_id": b.start,
             "status": ChunkStatus.PENDING, "attempt": 0, "retry_round": retry_round}
            for b in part
        ])
    items = await session.execute(
        update(Item)
        .where(Item.job_id == job_id, Item.status.in_(RETRYABLE_ITEM_STATUSES))
        .values(status=ItemStatus.PENDING, error_class=None, updated_at=func.now())
    )
    failed = await session.execute(
        update(RangeChunk)
        .where(RangeChunk.job_id == job_id, RangeChunk.status == ChunkStatus.FAILED)
        .values(status=ChunkStatus.PENDING, attempt=0, not_before=None, error_class=None,
                updated_at=func.now())
    )
    if job.status in (JobStatus.STOPPED, JobStatus.COMPLETED, JobStatus.FAILED):
        apply_action(job, JobAction.RESTART)
        job.run_attempt += 1
        job.finished_at = None
        job.finish_reason = None
        job.stop_requested_at = None
    await events.emit(
        session, "job.retry_errors", job.status,
        f"возвращено элементов: {items.rowcount}, блоков создано: {len(bounds)}", job_id=job.id,
    )
    await settle_job(session, job)
    await session.commit()
    return RetryOutcome(items.rowcount, len(bounds), failed.rowcount)


async def settle_job(session: AsyncSession, job: Job) -> None:
    """Доводит задание до следующего статуса, если текущие операции закончились.

    Вызывающий держит блокировку строки задания: так параллельные закрытия блоков
    видят друг друга, и переход не теряется.
    """
    before = job.status
    counts = dict((await session.execute(
        select(RangeChunk.status, func.count())
        .where(RangeChunk.job_id == job.id).group_by(RangeChunk.status)
    )).all())
    leased = counts.get(ChunkStatus.LEASED, 0)
    pending = counts.get(ChunkStatus.PENDING, 0)
    failed = counts.get(ChunkStatus.FAILED, 0)
    finished = leased == 0 and pending == 0 and is_exhausted(job)

    if job.status in (JobStatus.RUNNING, JobStatus.PAUSING) and finished:
        if failed:
            _finish(job, JobAction.FAIL, "failed_chunks")
        else:
            limited = job.max_ids is not None and effective_end(job) != job.range_to
            _finish(job, JobAction.FINISH, "max_ids" if limited else "range_exhausted")
    elif job.status == JobStatus.PAUSING and leased == 0:
        apply_action(job, JobAction.DRAINED)
    elif job.status == JobStatus.STOPPING and leased == 0:
        apply_action(job, JobAction.DRAINED)
        job.finished_at = func.now()
        job.finish_reason = job.finish_reason or "stopped_by_user"
    if job.status != before:
        failed_now = job.status == JobStatus.FAILED
        await events.emit(
            session, "job.status", job.status, job.finish_reason, job_id=job.id,
            level="error" if failed_now else "info", component="dispatcher",
            error_code=job.finish_reason if failed_now else None,
        )


def _finish(job: Job, action: JobAction, reason: str) -> None:
    apply_action(job, action)
    job.finished_at = func.now()
    job.finish_reason = reason


@dataclass(frozen=True)
class JobView:
    job: Job
    total_ids: int | None
    processed_ids: int
    chunks_total: int | None
    chunks_done: int
    chunks_leased: int
    chunks_failed: int
    items: dict[str, int]
    worker_threads: dict[str, int | None]


async def get_job_view(session: AsyncSession, job_id: int) -> JobView:
    job = await session.get(Job, job_id, populate_existing=True)
    if job is None:
        raise JobNotFound(f"задание {job_id} не найдено")
    return (await _views(session, [job]))[0]


async def list_job_views(session: AsyncSession, limit: int = 100) -> list[JobView]:
    jobs = list(await session.scalars(select(Job).order_by(Job.id.desc()).limit(limit)))
    return await _views(session, jobs)


async def _views(session: AsyncSession, jobs: list[Job]) -> list[JobView]:
    if not jobs:
        return []
    ids = [j.id for j in jobs]
    chunk_rows = await session.execute(
        select(
            RangeChunk.job_id,
            func.count(),
            func.count().filter(RangeChunk.status == ChunkStatus.DONE),
            func.count().filter(RangeChunk.status == ChunkStatus.LEASED),
            func.count().filter(RangeChunk.status == ChunkStatus.FAILED),
            # Блоки повтора лежат внутри уже посчитанного диапазона.
            func.sum(RangeChunk.next_id - RangeChunk.range_start)
            .filter(RangeChunk.retry_round == 0),
        ).where(RangeChunk.job_id.in_(ids)).group_by(RangeChunk.job_id)
    )
    chunks = {row[0]: row[1:] for row in chunk_rows}
    item_rows = await session.execute(
        select(Item.job_id, Item.status, func.count())
        .where(Item.job_id.in_(ids)).group_by(Item.job_id, Item.status)
    )
    items: dict[int, dict[str, int]] = {i: {s.value: 0 for s in ItemStatus} for i in ids}
    for job_id, status, count in item_rows:
        items[job_id][status] = count
    assigned: dict[int, dict[str, int | None]] = {i: {} for i in ids}
    assigned_rows = await session.execute(
        select(JobWorker.job_id, Worker.name, JobWorker.threads)
        .join(Worker, Worker.id == JobWorker.worker_id).where(JobWorker.job_id.in_(ids))
    )
    for job_id, name, threads in assigned_rows:
        assigned[job_id][name] = threads

    views = []
    for job in jobs:
        materialized, done, leased, failed, processed = chunks.get(job.id, (0, 0, 0, 0, 0))
        end = effective_end(job)
        total = None if end is None else end - job.range_from + 1
        views.append(JobView(
            job=job, total_ids=total, processed_ids=int(processed or 0),
            chunks_total=None if total is None else chunks_needed(total, job.chunk_size),
            chunks_done=done, chunks_leased=leased, chunks_failed=failed, items=items[job.id],
            worker_threads=assigned[job.id],
        ))
    return views
