"""Создание заданий и чтение их состояния для интерфейса."""
from dataclasses import dataclass

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.domain.chunks import split_range
from coordinator.domain.statuses import ChunkStatus, ItemStatus, JobStatus
from coordinator.storage.models import Item, Job, RangeChunk


class JobNotFound(Exception):
    pass


class JobStateConflict(Exception):
    pass


async def create_job(session: AsyncSession, range_from: int, range_to: int, chunk_size: int) -> int:
    bounds = split_range(range_from, range_to, chunk_size)
    job = Job(range_from=range_from, range_to=range_to, chunk_size=chunk_size,
              status=JobStatus.CREATED)
    session.add(job)
    await session.flush()
    await session.execute(insert(RangeChunk), [
        {"job_id": job.id, "range_start": b.start, "range_end": b.end, "next_id": b.start,
         "status": ChunkStatus.PENDING, "attempt": 0}
        for b in bounds
    ])
    await session.commit()
    return job.id


async def start_job(session: AsyncSession, job_id: int) -> None:
    job = await session.get(Job, job_id, with_for_update=True)
    if job is None:
        raise JobNotFound(job_id)
    if job.status != JobStatus.CREATED:
        raise JobStateConflict(f"задание {job_id} в статусе {job.status}, запуск невозможен")
    await session.execute(
        update(Job).where(Job.id == job_id)
        .values(status=JobStatus.RUNNING, started_at=func.now())
    )
    await session.commit()


@dataclass(frozen=True)
class JobView:
    job: Job
    total_ids: int
    processed_ids: int
    chunks_total: int
    chunks_done: int
    chunks_leased: int
    items: dict[str, int]


async def get_job_view(session: AsyncSession, job_id: int) -> JobView:
    job = await session.get(Job, job_id)
    if job is None:
        raise JobNotFound(job_id)
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
            func.sum(RangeChunk.next_id - RangeChunk.range_start),
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

    views = []
    for job in jobs:
        total, done, leased, processed = chunks.get(job.id, (0, 0, 0, 0))
        views.append(JobView(
            job=job, total_ids=job.range_to - job.range_from + 1, processed_ids=int(processed or 0),
            chunks_total=total, chunks_done=done, chunks_leased=leased, items=items[job.id],
        ))
    return views
