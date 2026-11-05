"""Журнал событий (ТЗ, п. 15): одна запись на значимое действие, поиск по полям и времени."""
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.domain.security import mask_secrets
from coordinator.storage.models import Event

log = logging.getLogger("coordinator.events")

_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING,
           "error": logging.ERROR}


async def emit(
    session: AsyncSession, operation: str, result: str, message: str | None = None, *,
    level: str = "info", component: str = "api", job_id: int | None = None,
    worker_id: int | None = None, chunk_id: int | None = None, external_id: int | None = None,
    error_code: str | None = None,
) -> None:
    """Пишет событие в текущей транзакции и в журнал процесса. Секреты маскируются."""
    text = mask_secrets(message) if message else None
    await session.execute(insert(Event).values(
        level=level, component=component, job_id=job_id, worker_id=worker_id, chunk_id=chunk_id,
        external_id=external_id, operation=operation, result=result, error_code=error_code,
        message=text,
    ))
    log.log(_LEVELS.get(level, logging.INFO), text or operation, extra={
        "job_id": job_id, "worker_id": worker_id, "chunk_id": chunk_id,
        "external_id": external_id, "operation": operation, "result": result,
        "error_code": error_code,
    })


@dataclass(frozen=True)
class EventFilter:
    job_id: int | None = None
    worker_id: int | None = None
    chunk_id: int | None = None
    external_id: int | None = None
    level: str | None = None
    operation: str | None = None
    result: str | None = None
    q: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    before_id: int | None = None


async def search(session: AsyncSession, flt: EventFilter, limit: int = 100) -> list[Event]:
    """Новые сверху, постранично по id. Поиск по заданию, узлу, материалу, статусу и времени."""
    stmt: Any = select(Event)
    for column, value in (
        (Event.job_id, flt.job_id), (Event.worker_id, flt.worker_id),
        (Event.chunk_id, flt.chunk_id), (Event.external_id, flt.external_id),
        (Event.level, flt.level), (Event.operation, flt.operation), (Event.result, flt.result),
    ):
        if value is not None:
            stmt = stmt.where(column == value)
    if flt.q:
        stmt = stmt.where(Event.message.icontains(flt.q, autoescape=True))
    if flt.since is not None:
        stmt = stmt.where(Event.at >= flt.since)
    if flt.until is not None:
        stmt = stmt.where(Event.at <= flt.until)
    if flt.before_id is not None:
        stmt = stmt.where(Event.id < flt.before_id)
    return list(await session.scalars(stmt.order_by(Event.id.desc()).limit(limit)))
