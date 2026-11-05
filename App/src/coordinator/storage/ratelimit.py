"""Состояние общего лимита запросов в Redis. База остаётся источником правды: без Redis
координатор работает на равных долях, посчитанных по ней, и не падает (ТЗ, п. 5.3)."""
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.config import Settings
from coordinator.domain.balance import effective_threads
from coordinator.domain.rps import fair_shares, next_limit
from coordinator.domain.statuses import JobStatus, WorkerStatus
from coordinator.storage.models import Job, Worker

_THROTTLED = "rate:throttled"
_EFFECTIVE = "rate:effective"
_SHARES = "rate:shares"
_TTL_S = 60


async def report_throttled(redis: Redis, count: int) -> None:
    """Агент сообщил об ответах 429 и 5xx. Потеря счётчика не страшна, результат важнее."""
    if count > 0:
        try:
            await redis.incrby(_THROTTLED, count)
            await redis.expire(_THROTTLED, _TTL_S)
        except RedisError:
            pass


async def base_limit(session: AsyncSession, settings: Settings) -> float:
    """Общий лимит стенда: задание может только снизить его, но не поднять."""
    job_limit = await session.scalar(
        select(func.min(Job.global_rate_limit_rps)).where(
            Job.status.in_((JobStatus.RUNNING, JobStatus.PAUSING)),
        )
    )
    return settings.global_rps_limit if job_limit is None else min(
        job_limit, settings.global_rps_limit,
    )


async def _active_workers(session: AsyncSession) -> list[Worker]:
    return list(await session.scalars(
        select(Worker).where(Worker.status == WorkerStatus.ONLINE, Worker.enabled.is_(True))
    ))


def _weights(workers: list[Worker], settings: Settings) -> dict[int, float]:
    return {
        w.id: effective_threads(
            w.desired_threads or w.threads, w.cpu_limit, settings.threads_per_cpu,
        )
        for w in workers
    }


async def update_shares(session: AsyncSession, redis: Redis, settings: Settings) -> None:
    base = await base_limit(session, settings)
    workers = await _active_workers(session)
    await session.commit()
    throttled = int(await redis.getdel(_THROTTLED) or 0)
    stored = await redis.get(_EFFECTIVE)
    effective = next_limit(float(stored) if stored else base, base, throttled)
    shares = fair_shares(
        effective, _weights(workers, settings), {w.id: w.rps_limit for w in workers},
    )
    async with redis.pipeline(transaction=True) as pipe:
        pipe.set(_EFFECTIVE, effective, ex=_TTL_S)
        pipe.delete(_SHARES)
        if shares:
            pipe.hset(_SHARES, mapping={str(k): v for k, v in shares.items()})
            pipe.expire(_SHARES, _TTL_S)
        await pipe.execute()


async def share_for(
    session: AsyncSession, redis: Redis, settings: Settings, worker_id: int,
) -> float:
    try:
        share = await redis.hget(_SHARES, str(worker_id))
        if share is not None:
            return float(share)
        # Узел ещё не учтён, точная доля будет на следующем проходе. Малая заглушка опасна:
        # агент применяет долю на весь блок и завис бы на нём. Секунду превышения лимит переживёт.
        known = await redis.hlen(_SHARES)
        effective = await redis.get(_EFFECTIVE)
        if known and effective:
            return float(effective) / (known + 1)
    except RedisError:
        pass
    online = await session.scalar(
        select(func.count()).select_from(Worker).where(Worker.status == WorkerStatus.ONLINE)
    )
    return await base_limit(session, settings) / max(1, online or 1)


async def snapshot(session: AsyncSession, redis: Redis, settings: Settings) -> dict[str, object]:
    base = await base_limit(session, settings)
    effective = None
    shares: dict[int, float] = {}
    available = True
    try:
        stored = await redis.get(_EFFECTIVE)
        effective = float(stored) if stored else None
        shares = {int(k): float(v) for k, v in (await redis.hgetall(_SHARES)).items()}
    except RedisError:
        available = False
    return {
        "base_rps": base, "effective_rps": effective, "shares": shares,
        "redis_available": available,
    }
