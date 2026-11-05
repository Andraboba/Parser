"""Общие действия тестов над API и прямые правки базы для имитации времени."""
import asyncio
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from coordinator.config import Settings
from coordinator.dispatcher.ticks import run_tick
from coordinator.storage import db
from coordinator.storage.cache import make_redis


def make_job(client: TestClient, **fields: Any) -> int:
    resp = client.post("/api/jobs", json={"range": "[1,10]", "chunk_size": 10, **fields})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def start_job(client: TestClient, **fields: Any) -> int:
    job_id = make_job(client, **fields)
    resp = client.post(f"/api/jobs/{job_id}/start")
    assert resp.status_code == 200, resp.text
    return job_id


def register(
    client: TestClient, name: str = "worker-01", *, threads: int = 4,
    storage_path: str | None = "/data/videos",
) -> int:
    resp = client.post("/api/workers/register", json={
        "name": name, "agent_version": "t", "protocol_version": 1, "threads": threads,
        "storage_path": storage_path,
    })
    assert resp.status_code == 201, resp.text
    return resp.json()["worker_id"]


def lease(client: TestClient, worker_id: int) -> dict[str, Any] | None:
    resp = client.post(f"/api/workers/{worker_id}/lease", json={"free_slots": 1})
    if resp.status_code == 204:
        return None
    assert resp.status_code == 200, resp.text
    return resp.json()


def must_lease(client: TestClient, worker_id: int) -> dict[str, Any]:
    chunk = lease(client, worker_id)
    assert chunk is not None
    return chunk


def complete(
    client: TestClient, worker_id: int, chunk: dict[str, Any], next_id: int | None = None,
    results: list[dict[str, Any]] | None = None, key: str | None = None,
) -> Any:
    return client.post(
        f"/api/workers/{worker_id}/complete",
        headers={"Idempotency-Key": key} if key else {},
        json={
            "chunk_id": chunk["chunk_id"],
            "next_id": chunk["range_end"] + 1 if next_id is None else next_id,
            "results": results or [],
        },
    )


def progress(
    client: TestClient, worker_id: int, chunk: dict[str, Any], next_id: int,
    results: list[dict[str, Any]] | None = None,
) -> Any:
    return client.post(f"/api/workers/{worker_id}/progress", json={
        "chunk_id": chunk["chunk_id"], "next_id": next_id, "results": results or [],
    })


def job_of(client: TestClient, job_id: int) -> dict[str, Any]:
    return client.get(f"/api/jobs/{job_id}").json()


def sql(statement: str, **params: Any) -> list[Any]:
    engine = create_engine(Settings().database_url)
    try:
        with engine.begin() as conn:
            result = conn.execute(text(statement), params)
            return list(result) if result.returns_rows else []
    finally:
        engine.dispose()


def run_dispatcher_tick(redis_url: str | None = None) -> None:
    """Один проход диспетчера со своим подключением: у теста другой цикл событий."""
    async def tick() -> None:
        active = Settings()
        engine = db.make_engine(active.database_url)
        redis = make_redis(redis_url or active.redis_url, 0.5)
        try:
            async with db.make_sessionmaker(engine)() as session:
                await run_tick(session, active, redis)
        finally:
            await redis.aclose()
            await engine.dispose()

    asyncio.run(tick())


def expire_leases() -> None:
    sql("UPDATE range_chunks SET lease_expires_at = now() - interval '1 second' "
        "WHERE status = 'leased'")


def silence_worker(worker_id: int) -> None:
    sql("UPDATE workers SET last_heartbeat_at = now() - interval '1 hour' WHERE id = :id",
        id=worker_id)
