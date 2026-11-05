"""Сводка для главной панели, предупреждений и экспорта метрик. Только чтение."""
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.config import Settings
from coordinator.domain.alerts import (
    Alert,
    AlertLimits,
    JobSnapshot,
    WorkerSnapshot,
    job_alerts,
    version_alerts,
    worker_alerts,
)
from coordinator.domain.progress import Rates, Sample, average_rate, current_rates, eta_seconds
from coordinator.domain.statuses import ChunkStatus, JobStatus, WorkerStatus
from coordinator.storage.jobs import JobView, get_job_view
from coordinator.storage.models import (
    Download,
    Item,
    Job,
    JobSample,
    RangeChunk,
    Worker,
    WorkerMetric,
)

_ACTIVE = (JobStatus.RUNNING, JobStatus.PAUSING, JobStatus.PAUSED, JobStatus.STOPPING)
_RATE_WINDOW_S = 30.0
_SAMPLE_KEEP_MINUTES = 240


async def bytes_done(session: AsyncSession, job_id: int) -> int:
    total = await session.scalar(
        select(func.coalesce(func.sum(Download.bytes_done), 0))
        .join(Item, Item.id == Download.item_id).where(Item.job_id == job_id)
    )
    return int(total or 0)


async def record_samples(session: AsyncSession) -> None:
    """Снимок прогресса заданий в работе. Вызывает диспетчер: так скорость считается одинаково
    у всех экземпляров API и переживает их перезапуск."""
    rows = await session.execute(
        select(Job.id, func.coalesce(func.sum(RangeChunk.next_id - RangeChunk.range_start), 0))
        .join(RangeChunk, RangeChunk.job_id == Job.id)
        .where(
            Job.status.in_((JobStatus.RUNNING, JobStatus.PAUSING, JobStatus.STOPPING)),
            RangeChunk.retry_round == 0,
        )
        .group_by(Job.id)
    )
    for job_id, processed in rows.all():
        session.add(JobSample(
            job_id=job_id, processed_ids=int(processed),
            bytes_done=await bytes_done(session, job_id),
        ))
    await session.execute(JobSample.__table__.delete().where(
        JobSample.at < func.now() - timedelta(minutes=_SAMPLE_KEEP_MINUTES),
    ))
    await session.commit()


async def job_samples(session: AsyncSession, job_id: int, minutes: int) -> list[Sample]:
    rows = await session.execute(
        select(JobSample.at, JobSample.processed_ids, JobSample.bytes_done)
        .where(
            JobSample.job_id == job_id,
            JobSample.at > func.now() - timedelta(minutes=minutes),
        )
        .order_by(JobSample.at)
    )
    return [Sample(*row) for row in rows]


@dataclass(frozen=True)
class Dashboard:
    view: JobView | None
    bytes_done: int
    rates: Rates
    average_ids_per_second: float
    average_bytes_per_second: float
    eta_seconds: float | None
    active_workers: int
    active_threads: int
    workers_total: int
    samples: list[Sample]


async def _active_worker_counts(session: AsyncSession) -> tuple[int, int, int]:
    workers = list(await session.scalars(select(Worker)))
    online = [w for w in workers if w.status == WorkerStatus.ONLINE and w.enabled]
    threads = sum(w.desired_threads or w.threads for w in online)
    return len(online), threads, len(workers)


async def dashboard(session: AsyncSession, job_id: int | None) -> Dashboard:
    """Состояние выбранного задания. Без выбора — последнее активное, иначе просто последнее."""
    if job_id is None:
        job_id = await session.scalar(
            select(Job.id).where(Job.status.in_(_ACTIVE)).order_by(Job.id.desc()).limit(1)
        ) or await session.scalar(select(Job.id).order_by(Job.id.desc()).limit(1))
    active, threads, total = await _active_worker_counts(session)
    if job_id is None:
        return Dashboard(None, 0, Rates(0.0, 0.0), 0.0, 0.0, None, active, threads, total, [])
    view = await get_job_view(session, job_id)
    samples = await job_samples(session, job_id, 30)
    rates = current_rates(samples, _RATE_WINDOW_S)
    done = await bytes_done(session, job_id)
    job = view.job
    now = datetime.now(UTC)
    elapsed = ((job.finished_at or now) - job.started_at).total_seconds() if job.started_at else 0.0
    return Dashboard(
        view=view, bytes_done=done, rates=rates,
        average_ids_per_second=average_rate(view.processed_ids, elapsed),
        average_bytes_per_second=average_rate(done, elapsed),
        eta_seconds=eta_seconds(view.total_ids, view.processed_ids, rates.ids_per_second),
        active_workers=active, active_threads=threads, workers_total=total, samples=samples,
    )


def _delta(newest: dict | None, oldest: dict | None, key: str) -> int:
    if not newest or not oldest:
        return 0
    return max(0, int(newest.get(key, 0)) - int(oldest.get(key, 0)))


async def worker_snapshots(session: AsyncSession, settings: Settings) -> list[WorkerSnapshot]:
    workers = list(await session.scalars(select(Worker).order_by(Worker.id)))
    window = func.now() - timedelta(seconds=settings.balance_window_seconds)
    samples: dict[int, list[WorkerMetric]] = {}
    for sample in await session.scalars(
        select(WorkerMetric).where(WorkerMetric.collected_at > window)
        .order_by(WorkerMetric.collected_at.desc())
    ):
        samples.setdefault(sample.worker_id, []).append(sample)
    now = datetime.now(UTC)
    snapshots = []
    for worker in workers:
        recent = samples.get(worker.id, [])
        cpu = [s.cpu_cores for s in recent if s.cpu_cores is not None][:3]
        counters = [s.counters for s in recent if s.counters]
        newest, oldest = (counters[0], counters[-1]) if len(counters) > 1 else (None, None)
        latest = recent[0] if recent else None
        state = worker.container_state or {}
        min_free_gb = (
            settings.default_min_free_space_gb if worker.min_free_space_gb is None
            else worker.min_free_space_gb
        )
        snapshots.append(WorkerSnapshot(
            id=worker.id, name=worker.name, enabled=worker.enabled,
            online=worker.status == WorkerStatus.ONLINE,
            heartbeat_age_seconds=(
                (now - worker.last_heartbeat_at).total_seconds()
                if worker.last_heartbeat_at else None
            ),
            agent_version=worker.agent_version, protocol_version=worker.protocol_version,
            cpu_cores_avg=sum(cpu) / len(cpu) if cpu else None,
            cpu_limit=(latest.cpu_limit if latest else None) or worker.cpu_limit,
            memory_bytes=latest.memory_bytes if latest else None,
            memory_limit=latest.memory_limit if latest else None,
            disk_free_bytes=latest.disk_free_bytes if latest else None,
            min_free_bytes=int(min_free_gb * 1024**3),
            checked_recently=_delta(newest, oldest, "checked"),
            failed_recently=_delta(newest, oldest, "failed"),
            rate_limited_recently=_delta(newest, oldest, "rate_limited"),
            container_state=state.get("state"),
            restart_count=int(state.get("restart_count") or 0),
            oom_killed=bool(state.get("oom_killed")),
        ))
    return snapshots


async def job_snapshots(session: AsyncSession, online_workers: int) -> list[JobSnapshot]:
    jobs = list(await session.scalars(select(Job).where(Job.status == JobStatus.RUNNING)))
    now = datetime.now(UTC)
    snapshots = []
    for job in jobs:
        samples = await job_samples(session, job.id, 15)
        rates = current_rates(samples, _RATE_WINDOW_S)
        since_progress = None
        if samples:
            moved = next(
                (s for s in reversed(samples) if s.processed_ids != samples[-1].processed_ids),
                samples[0],
            )
            since_progress = (samples[-1].at - moved.at).total_seconds()
        running = (now - job.started_at).total_seconds() if job.started_at else 0.0
        processed = samples[-1].processed_ids if samples else 0
        snapshots.append(JobSnapshot(
            id=job.id, running_seconds=running, seconds_since_progress=since_progress,
            current_ids_per_second=rates.ids_per_second,
            average_ids_per_second=average_rate(processed, running),
            has_active_workers=online_workers > 0,
        ))
    return snapshots


async def active_alerts(session: AsyncSession, settings: Settings) -> list[Alert]:
    workers = await worker_snapshots(session, settings)
    limits = AlertLimits()
    found: list[Alert] = []
    for worker in workers:
        found += worker_alerts(worker, limits, settings.worker_offline_after)
    found += version_alerts(workers, settings.protocol_version)
    online = sum(1 for w in workers if w.online and w.enabled)
    for job in await job_snapshots(session, online):
        found += job_alerts(job, limits)
    return found


@dataclass(frozen=True)
class Totals:
    chunks: dict[str, int]
    items: dict[str, int]
    oldest_pending_seconds: float
    jobs: dict[str, int]
    bytes_done: int


async def totals(session: AsyncSession) -> Totals:
    chunks = dict((await session.execute(
        select(RangeChunk.status, func.count()).group_by(RangeChunk.status)
    )).all())
    items = dict((await session.execute(
        select(Item.status, func.count()).group_by(Item.status)
    )).all())
    jobs = dict((await session.execute(
        select(Job.status, func.count()).group_by(Job.status)
    )).all())
    oldest = await session.scalar(
        select(func.min(RangeChunk.created_at)).where(RangeChunk.status == ChunkStatus.PENDING)
    )
    done = await session.scalar(select(func.coalesce(func.sum(Download.bytes_done), 0)))
    return Totals(
        chunks=chunks, items=items, jobs=jobs, bytes_done=int(done or 0),
        oldest_pending_seconds=(datetime.now(UTC) - oldest).total_seconds() if oldest else 0.0,
    )
