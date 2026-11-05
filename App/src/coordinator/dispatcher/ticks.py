"""Периодические работы диспетчера. Каждая функция — короткая транзакция, состояние только в базе,
поэтому перезапуск процесса ничего не теряет (ТЗ, п. 5.1)."""
import logging
from datetime import timedelta
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import case, delete, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.config import Settings
from coordinator.domain.statuses import ChunkStatus, JobAction, JobStatus, WorkerStatus
from coordinator.storage import events, ratelimit, users
from coordinator.storage.jobs import apply_action, settle_job
from coordinator.storage.models import (
    Event,
    IdempotencyKey,
    Job,
    RangeChunk,
    Worker,
    WorkerMetric,
)

log = logging.getLogger("coordinator.dispatcher")


async def mark_offline_workers(session: AsyncSession, settings: Settings) -> int:
    rows = (await session.execute(
        update(Worker)
        .where(
            Worker.status == WorkerStatus.ONLINE,
            Worker.last_heartbeat_at
            < func.now() - timedelta(seconds=settings.worker_offline_after),
        )
        .values(status=WorkerStatus.OFFLINE)
        .returning(Worker.id, Worker.name)
    )).all()
    for worker_id, name in rows:
        await events.emit(
            session, "worker.offline", "offline", f"{name}: нет heartbeat", level="warning",
            component="dispatcher", worker_id=worker_id, error_code="no_heartbeat",
        )
    await session.commit()
    return len(rows)


async def reclaim_chunks(session: AsyncSession, settings: Settings) -> int:
    """Возвращает в очередь блоки с истёкшей арендой и блоки узлов без heartbeat.

    Контрольная точка next_id остаётся, поэтому следующий узел не повторит сделанное.
    Блок, снова и снова теряющий владельца, помечается failed: он может ронять узлы.
    """
    offline = select(Worker.id).where(Worker.status == WorkerStatus.OFFLINE)
    rows = (await session.execute(
        update(RangeChunk)
        .where(
            RangeChunk.status == ChunkStatus.LEASED,
            or_(RangeChunk.lease_expires_at < func.now(), RangeChunk.leased_by.in_(offline)),
        )
        .values(
            status=case(
                (RangeChunk.attempt >= settings.max_chunk_attempts, ChunkStatus.FAILED),
                else_=ChunkStatus.PENDING,
            ),
            error_class="lease_expired", lease_expires_at=None, updated_at=func.now(),
        )
        .returning(RangeChunk.id, RangeChunk.job_id, RangeChunk.leased_by, RangeChunk.next_id,
                   RangeChunk.status)
    )).all()
    for chunk_id, job_id, worker_id, next_id, status in rows:
        await events.emit(
            session, "chunk.reclaimed", status, f"возврат в очередь с {next_id}", level="warning",
            component="dispatcher", job_id=job_id, worker_id=worker_id, chunk_id=chunk_id,
            error_code="lease_expired",
        )
    await session.commit()
    return len(rows)


async def force_stop_leases(session: AsyncSession, settings: Settings) -> int:
    """Немедленная остановка: агент, не среагировавший за отведённое время, теряет аренду.
    Его поздний complete всё равно будет принят, если блок никому не передан."""
    stopping = select(Job.id).where(
        Job.status == JobStatus.STOPPING,
        Job.stop_requested_at < func.now() - timedelta(seconds=settings.stop_grace_seconds),
    )
    result = await session.execute(
        update(RangeChunk)
        .where(RangeChunk.status == ChunkStatus.LEASED, RangeChunk.job_id.in_(stopping))
        .values(status=ChunkStatus.PENDING, lease_expires_at=None, updated_at=func.now())
    )
    await session.commit()
    return result.rowcount


async def settle_jobs(session: AsyncSession) -> None:
    """Страховка: переводит задания, для которых не пришло закрывающего запроса от агента."""
    active = (JobStatus.RUNNING, JobStatus.PAUSING, JobStatus.STOPPING)
    job_ids = list(await session.scalars(select(Job.id).where(Job.status.in_(active))))
    await session.rollback()
    for job_id in job_ids:
        job = await session.scalar(
            select(Job).where(Job.id == job_id, Job.status.in_(active))
            .with_for_update(skip_locked=True).execution_options(populate_existing=True)
        )
        if job is not None:
            await settle_job(session, job)
        await session.commit()


_CONSECUTIVE_MISSING_SQL = text("""
    SELECT frontier,
           (SELECT count(*) FROM items i
             WHERE i.job_id = :job_id AND i.status <> 'not_found'
               AND i.external_id >= frontier - :limit AND i.external_id < frontier)
      FROM (SELECT coalesce(
                     (SELECT min(c.next_id) FROM range_chunks c
                       WHERE c.job_id = :job_id AND c.status <> 'done' AND c.retry_round = 0),
                     :cursor) AS frontier) AS f
""")


async def _consecutive_missing_reached(session: AsyncSession, job: Job) -> bool:
    """Подряд отсутствующие идентификаторы считаем только по сплошной обработанной части
    диапазона: блоки выполняются вразнобой, и дыра впереди ничего не доказывает."""
    limit = job.stop_on_consecutive_missing
    if limit is None:
        return False
    frontier, present = (await session.execute(_CONSECUTIVE_MISSING_SQL, {
        "job_id": job.id, "limit": limit, "cursor": job.next_chunk_start,
    })).one()
    return frontier - limit >= job.range_from and present == 0


async def check_stop_conditions(session: AsyncSession, settings: Settings) -> None:
    """Условия завершения задания из ТЗ, п. 10. Активные блоки закрываются как при остановке."""
    job_ids = list(await session.scalars(select(Job.id).where(Job.status == JobStatus.RUNNING)))
    online = await session.scalar(select(func.count()).select_from(Worker).where(
        Worker.status == WorkerStatus.ONLINE, Worker.enabled.is_(True),
    ))
    await session.rollback()
    for job_id in job_ids:
        job = await session.scalar(
            select(Job).where(Job.id == job_id, Job.status == JobStatus.RUNNING)
            .with_for_update(skip_locked=True).execution_options(populate_existing=True)
        )
        if job is not None:
            reason = await _stop_reason(session, job, settings, online or 0)
            if reason is not None:
                apply_action(job, JobAction.STOP)
                job.stop_requested_at = func.now()
                job.finish_reason = reason
                await events.emit(
                    session, "job.stop_condition", job.status, reason, component="dispatcher",
                    job_id=job.id,
                )
                await settle_job(session, job)
        await session.commit()


async def _stop_reason(
    session: AsyncSession, job: Job, settings: Settings, online: int,
) -> str | None:
    if job.max_duration_seconds is not None:
        limit = func.now() - timedelta(seconds=job.max_duration_seconds)
        if await session.scalar(select(job.started_at < limit)):
            return "max_duration"
    if await _consecutive_missing_reached(session, job):
        return "consecutive_missing"
    if job.range_to is None:
        if online > 0:
            job.no_workers_since = None
        elif job.no_workers_since is None:
            job.no_workers_since = func.now()
        else:
            limit = func.now() - timedelta(seconds=settings.no_workers_grace_seconds)
            if await session.scalar(select(job.no_workers_since < limit)):
                return "no_workers"
    return None


async def sync_containers(session: AsyncSession, containers: list[dict[str, Any]]) -> None:
    """Контейнер пропал или не запущен: узел недоступен сразу, не дожидаясь heartbeat.
    Узлы вне управления runner-а (их никогда не было в списке) не трогаем."""
    by_worker = {c["worker"]: c for c in containers}
    workers = list(await session.scalars(select(Worker)))
    for worker in workers:
        container = by_worker.get(worker.name)
        if container is not None:
            worker.container_state = container
        elif worker.container_state is not None:
            worker.container_state = {**worker.container_state, "state": "missing"}
        state = worker.container_state
        if state is not None and state.get("state") != "running":
            worker.status = WorkerStatus.OFFLINE
    await session.commit()


async def grant_amnesty(session: AsyncSession, settings: Settings) -> None:
    """После простоя базы или диспетчера heartbeat-ы некуда было доставить, и по времени все
    узлы выглядят мёртвыми. Даём им один полный срок: иначе живые агенты разом потеряли бы
    блоки и работу пришлось бы повторять (ТЗ, п. 13)."""
    await session.execute(
        update(Worker).where(Worker.status == WorkerStatus.ONLINE)
        .values(last_heartbeat_at=func.now())
    )
    await session.execute(
        update(RangeChunk).where(RangeChunk.status == ChunkStatus.LEASED)
        .values(lease_expires_at=func.now() + timedelta(seconds=settings.lease_ttl))
    )
    await session.execute(update(Job).values(no_workers_since=None))
    await session.commit()


async def purge_old_rows(session: AsyncSession, settings: Settings) -> None:
    await session.execute(delete(IdempotencyKey).where(
        IdempotencyKey.created_at < func.now() - timedelta(hours=settings.idempotency_ttl_hours),
    ))
    await session.execute(delete(WorkerMetric).where(
        WorkerMetric.collected_at < func.now() - timedelta(hours=settings.metrics_retention_hours),
    ))
    await session.execute(delete(Event).where(
        Event.at < func.now() - timedelta(days=settings.events_retention_days),
    ))
    await session.commit()
    await users.purge_expired_sessions(session)


async def run_tick(session: AsyncSession, settings: Settings, redis: Redis | None = None) -> None:
    await mark_offline_workers(session, settings)
    await reclaim_chunks(session, settings)
    await check_stop_conditions(session, settings)
    await force_stop_leases(session, settings)
    await settle_jobs(session)
    if redis is not None:
        try:
            await ratelimit.update_shares(session, redis, settings)
        except RedisError as exc:
            # Без Redis узлы получают равные доли из базы, задание не останавливается.
            log.warning("redis недоступен: %s", type(exc).__name__)
