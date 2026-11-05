"""Регистрация узлов и чтение их состояния."""
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.domain.statuses import ChunkStatus, WorkerStatus
from coordinator.storage.models import RangeChunk, Worker


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
    worker_id = (await session.execute(stmt)).scalar_one()
    await session.commit()
    return worker_id


async def worker_exists(session: AsyncSession, worker_id: int) -> bool:
    return await session.get(Worker, worker_id) is not None


@dataclass(frozen=True)
class WorkerView:
    worker: Worker
    leased_chunks: int


async def list_worker_views(session: AsyncSession) -> list[WorkerView]:
    leased = (
        select(RangeChunk.leased_by, func.count().label("n"))
        .where(RangeChunk.status == ChunkStatus.LEASED)
        .group_by(RangeChunk.leased_by).subquery()
    )
    rows = await session.execute(
        select(Worker, func.coalesce(leased.c.n, 0))
        .outerjoin(leased, leased.c.leased_by == Worker.id)
        .order_by(Worker.name)
    )
    return [WorkerView(w, n) for w, n in rows]
