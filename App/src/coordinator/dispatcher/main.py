"""Процесс диспетчера: таймеры аренды, узлов и заданий.

Запуск: python -m coordinator.dispatcher.main. Работы атомарны, поэтому лишний экземпляр не мешает.
"""
import asyncio
import logging
import signal
import time
from typing import Any

from sqlalchemy.exc import DBAPIError

from coordinator.config import Settings
from coordinator.dispatcher.ticks import (
    grant_amnesty,
    purge_old_rows,
    run_tick,
    sync_containers,
)
from coordinator.errors import CoordinatorError
from coordinator.logging_setup import setup_logging
from coordinator.provisioning import reconcile_servers
from coordinator.runner_client import RunnerClient
from coordinator.storage import cache, db, monitoring

log = logging.getLogger("coordinator.dispatcher")

_PURGE_INTERVAL_S = 3600.0
_MAX_BACKOFF_S = 30.0


async def _server_loop(
    settings: Settings, sessions: Any, runner: RunnerClient, stop: asyncio.Event,
) -> None:
    """Отдельно от таймеров: медленный SSH не должен задерживать возврат блоков."""
    while not stop.is_set():
        try:
            async with sessions() as session:
                await reconcile_servers(session, runner, settings)
        except (DBAPIError, OSError, CoordinatorError) as exc:
            log.warning("сверка серверов не удалась: %s", type(exc).__name__)
        try:
            async with asyncio.timeout(settings.server_sync_interval):
                await stop.wait()
        except TimeoutError:
            pass


async def run(settings: Settings, stop: asyncio.Event) -> None:
    engine = db.make_engine(settings.database_url)
    sessions = db.make_sessionmaker(engine)
    redis = cache.make_redis(settings.redis_url, settings.health_timeout)
    runner = RunnerClient(settings.runner_url, settings.runner_token)
    backoff = 1.0
    last_purge = 0.0
    last_sync = 0.0
    last_sample = 0.0
    needs_amnesty = True
    servers = (
        asyncio.create_task(_server_loop(settings, sessions, runner, stop))
        if runner.configured else None
    )
    try:
        while not stop.is_set():
            try:
                async with sessions() as session:
                    if needs_amnesty:
                        await grant_amnesty(session, settings)
                        needs_amnesty = False
                    await run_tick(session, settings, redis)
                    if time.monotonic() - last_sample > settings.job_sample_interval:
                        last_sample = time.monotonic()
                        await monitoring.record_samples(session)
                    if runner.configured and (
                        time.monotonic() - last_sync > settings.container_sync_interval
                    ):
                        last_sync = time.monotonic()
                        try:
                            containers = await runner.call("GET", "/containers")
                            await sync_containers(session, containers)
                        except CoordinatorError as exc:
                            log.warning("runner не ответил: %s", exc)
                    if time.monotonic() - last_purge > _PURGE_INTERVAL_S:
                        await purge_old_rows(session, settings)
                        last_purge = time.monotonic()
                backoff = 1.0
                delay = settings.dispatcher_interval
            except (DBAPIError, OSError) as exc:
                # База недоступна: ждём и повторяем, состояние в ней не потеряно (ТЗ, п. 13).
                log.warning("база недоступна: %s; повтор через %.0f с", type(exc).__name__, backoff)
                needs_amnesty = True
                delay = backoff
                backoff = min(backoff * 2, _MAX_BACKOFF_S)
            try:
                async with asyncio.timeout(delay):
                    await stop.wait()
            except TimeoutError:
                pass
    finally:
        if servers is not None:
            servers.cancel()
            await asyncio.gather(servers, return_exceptions=True)
        await runner.aclose()
        await redis.aclose()
        await engine.dispose()


def main() -> None:
    setup_logging("dispatcher")

    async def runner() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await run(Settings(), stop)

    asyncio.run(runner())


if __name__ == "__main__":
    main()
