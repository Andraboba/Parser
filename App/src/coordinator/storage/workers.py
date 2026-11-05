"""Регистрация узлов, heartbeat и чтение их состояния."""
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy import insert as core_insert
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.config import Settings
from coordinator.domain.balance import NodeState, Thresholds
from coordinator.domain.statuses import ChunkStatus, WorkerStatus
from coordinator.errors import WorkerNotFound
from coordinator.storage.models import Job, JobWorker, RangeChunk, Worker, WorkerMetric


@dataclass(frozen=True)
class Registration:
    name: str
    provisioning_mode: str
    container_id: str | None
    image: str | None
    image_digest: str | None
    agent_version: str
    protocol_version: int
    threads: int
    cpu_limit: float | None
    mem_limit_mb: int | None
    storage_path: str | None
    last_ip: str | None = None


async def register_worker(session: AsyncSession, reg: Registration) -> int:
    """Регистрирует узел или обновляет существующий с тем же именем.

    Ключ — имя узла, а не адрес: пересозданный контейнер получает прежний worker_id.
    """
    values = {**reg.__dict__, "status": WorkerStatus.ONLINE, "last_heartbeat_at": func.now()}
    stmt = insert(Worker).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Worker.name],
        set_={k: stmt.excluded[k] for k in values if k != "name"},
    ).returning(Worker.id)
    return (await session.execute(stmt)).scalar_one()


_METRIC_COLUMNS = (
    "cpu_cores", "cpu_limit", "memory_bytes", "memory_limit", "disk_total_bytes",
    "disk_free_bytes", "load1", "net_rx_bps", "net_tx_bps", "uptime_seconds",
)


async def record_heartbeat(
    session: AsyncSession, worker: Worker, *, agent_state: str | None, free_slots: int | None,
    metrics: dict[str, Any], counters: dict[str, Any],
) -> None:
    worker.status = WorkerStatus.ONLINE
    worker.last_heartbeat_at = func.now()
    worker.agent_state = agent_state
    worker.free_slots = free_slots
    if metrics:
        worker.metrics = metrics
    if counters:
        worker.counters = counters
    if metrics:
        known = {k: metrics.get(k) for k in _METRIC_COLUMNS}
        extra = {k: v for k, v in metrics.items() if k not in _METRIC_COLUMNS}
        await session.execute(core_insert(WorkerMetric).values(
            worker_id=worker.id, extra=extra or None, counters=counters or None, **known,
        ))


async def apply_settings(session: AsyncSession, worker_id: int, changes: dict[str, Any]) -> None:
    """Настройки оператора. Поле со значением None снимает настройку."""
    worker = await session.get(Worker, worker_id, with_for_update=True)
    if worker is None:
        raise WorkerNotFound(f"узел {worker_id} не найден")
    for field, value in changes.items():
        setattr(worker, field, value)
    await session.commit()


async def metrics_series(
    session: AsyncSession, worker_id: int, minutes: int, limit: int = 720,
) -> list[WorkerMetric]:
    rows = await session.scalars(
        select(WorkerMetric)
        .where(
            WorkerMetric.worker_id == worker_id,
            WorkerMetric.collected_at > func.now() - timedelta(minutes=minutes),
        )
        .order_by(WorkerMetric.collected_at.desc()).limit(limit)
    )
    return list(rows)[::-1]


async def node_state(
    session: AsyncSession, worker: Worker, free_slots: int, settings: Settings,
) -> NodeState:
    """Состояние узла для решения о выдаче: средняя загрузка и ошибки за последнее окно."""
    samples = list(await session.scalars(
        select(WorkerMetric)
        .where(
            WorkerMetric.worker_id == worker.id,
            WorkerMetric.collected_at
            > func.now() - timedelta(seconds=settings.balance_window_seconds),
        )
        .order_by(WorkerMetric.collected_at.desc()).limit(12)
    ))
    leased = await session.scalar(
        select(func.count()).select_from(RangeChunk).where(
            RangeChunk.leased_by == worker.id, RangeChunk.status == ChunkStatus.LEASED,
        )
    )
    cpu = [s.cpu_cores for s in samples if s.cpu_cores is not None]
    latest = samples[0] if samples else None
    checked = failed = 0
    with_counters = [s.counters for s in samples if s.counters]
    if len(with_counters) >= 2:
        newest, oldest = with_counters[0], with_counters[-1]
        checked = max(0, int(newest.get("checked", 0)) - int(oldest.get("checked", 0)))
        failed = max(0, int(newest.get("failed", 0)) - int(oldest.get("failed", 0)))
    min_free_gb = (
        settings.default_min_free_space_gb if worker.min_free_space_gb is None
        else worker.min_free_space_gb
    )
    return NodeState(
        enabled=worker.enabled, free_slots=free_slots,
        threads=worker.desired_threads or worker.threads, leased_chunks=leased or 0,
        cpu_limit=(latest.cpu_limit if latest else None) or worker.cpu_limit,
        cpu_cores_avg=sum(cpu) / len(cpu) if cpu else None,
        memory_bytes=latest.memory_bytes if latest else None,
        memory_limit=latest.memory_limit if latest else None,
        disk_free_bytes=latest.disk_free_bytes if latest else None,
        min_free_bytes=int(min_free_gb * 1024**3),
        checked_recently=checked, failed_recently=failed,
    )


def thresholds(settings: Settings) -> Thresholds:
    return Thresholds(
        cpu_ratio=settings.cpu_overload_ratio, memory_ratio=settings.memory_overload_ratio,
        error_ratio=settings.error_ratio_limit, threads_per_chunk=settings.threads_per_chunk,
        threads_per_cpu=settings.threads_per_cpu,
    )


async def resolve_threads(session: AsyncSession, worker: Worker, job_id: int | None) -> int:
    """Индивидуальное значение важнее глобального (ТЗ, п. 4): узел в задании, настройка узла
    оператором, общее значение задания и только потом то, что заявил сам агент."""
    if job_id is not None:
        assigned = await session.scalar(select(JobWorker.threads).where(
            JobWorker.job_id == job_id, JobWorker.worker_id == worker.id,
        ))
        if assigned is not None:
            return assigned
    if worker.desired_threads is not None:
        return worker.desired_threads
    if job_id is not None:
        job_threads = await session.scalar(select(Job.threads).where(Job.id == job_id))
        if job_threads is not None:
            return job_threads
    return worker.threads


@dataclass(frozen=True)
class WorkerView:
    worker: Worker
    leased_chunks: int
    current_chunks: list[int]


async def get_worker_view(session: AsyncSession, worker_id: int) -> WorkerView:
    views = await _worker_views(session, Worker.id == worker_id)
    if not views:
        raise WorkerNotFound(f"узел {worker_id} не найден")
    return views[0]


async def list_worker_views(session: AsyncSession) -> list[WorkerView]:
    return await _worker_views(session, True)


async def _worker_views(session: AsyncSession, condition: Any) -> list[WorkerView]:
    workers = list(await session.scalars(
        select(Worker).where(condition).order_by(Worker.name)
        .execution_options(populate_existing=True)
    ))
    rows = await session.execute(
        select(RangeChunk.leased_by, RangeChunk.id)
        .where(RangeChunk.status == ChunkStatus.LEASED)
        .order_by(RangeChunk.id)
    )
    chunks: dict[int, list[int]] = {}
    for worker_id, chunk_id in rows:
        chunks.setdefault(worker_id, []).append(chunk_id)
    return [
        WorkerView(w, len(chunks.get(w.id, [])), chunks.get(w.id, []))
        for w in workers
    ]
