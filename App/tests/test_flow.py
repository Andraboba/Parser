"""Полный цикл задания через HTTP API и его граничные случаи."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


def make_job(client: TestClient, range_from: int, range_to: int, chunk_size: int) -> int:
    resp = client.post("/api/jobs", json={
        "range_from": range_from, "range_to": range_to, "chunk_size": chunk_size,
    })
    assert resp.status_code == 201
    return resp.json()["id"]


def start(client: TestClient, job_id: int) -> None:
    assert client.post(f"/api/jobs/{job_id}/start").status_code == 200


def register(client: TestClient, name: str = "worker-01", threads: int = 8) -> int:
    resp = client.post("/api/workers/register", json={
        "name": name, "agent_version": "test", "protocol_version": 1, "threads": threads,
    })
    assert resp.status_code == 201
    return resp.json()["worker_id"]


def lease(client: TestClient, worker_id: int) -> dict[str, Any] | None:
    resp = client.post(f"/api/workers/{worker_id}/lease", json={"free_slots": 1})
    if resp.status_code == 204:
        return None
    assert resp.status_code == 200
    return resp.json()


def complete_body(chunk: dict[str, Any]) -> dict[str, Any]:
    return {
        "chunk_id": chunk["chunk_id"],
        "next_id": chunk["range_end"] + 1,
        "results": [
            {"external_id": i, "status": "found" if i % 2 == 0 else "not_found"}
            for i in range(chunk["range_start"], chunk["range_end"] + 1)
        ],
    }


def test_full_cycle(client: TestClient) -> None:
    job_id = make_job(client, 1, 25, 10)
    created = client.get(f"/api/jobs/{job_id}").json()
    assert created["status"] == "created"
    assert created["created_at"] is not None
    assert (created["total_ids"], created["processed_ids"]) == (25, 0)
    assert created["chunks_total"] == 3

    worker_id = register(client)
    assert lease(client, worker_id) is None, "незапущенное задание не выдаёт блоков"

    started = client.post(f"/api/jobs/{job_id}/start").json()
    assert started["status"] == "running"
    assert started["started_at"] is not None

    chunks = []
    while (chunk := lease(client, worker_id)) is not None:
        chunks.append(chunk)
    assert [(c["range_start"], c["range_end"]) for c in chunks] == [(1, 10), (11, 20), (21, 25)]
    assert all(c["job_id"] == job_id and c["next_id"] == c["range_start"] for c in chunks)

    view = client.get(f"/api/jobs/{job_id}").json()
    assert view["chunks_leased"] == 3
    assert client.get("/api/workers").json()[0]["leased_chunks"] == 3

    for chunk in chunks:
        resp = client.post(f"/api/workers/{worker_id}/complete", json=complete_body(chunk))
        assert resp.status_code == 200

    done = client.get(f"/api/jobs/{job_id}").json()
    assert done["status"] == "completed"
    assert done["finished_at"] is not None
    assert (done["chunks_done"], done["chunks_leased"]) == (3, 0)
    assert done["processed_ids"] == 25
    assert (done["items"]["found"], done["items"]["not_found"]) == (12, 13)
    assert client.get("/api/workers").json()[0]["leased_chunks"] == 0


def test_repeated_complete_does_not_create_duplicates(client: TestClient) -> None:
    job_id = make_job(client, 1, 10, 10)
    start(client, job_id)
    worker_id = register(client)
    chunk = lease(client, worker_id)
    assert chunk is not None
    body = complete_body(chunk)

    assert client.post(f"/api/workers/{worker_id}/complete", json=body).status_code == 200
    assert client.post(f"/api/workers/{worker_id}/complete", json=body).status_code == 200

    items = client.get(f"/api/jobs/{job_id}").json()["items"]
    assert sum(items.values()) == 10


def test_duplicate_id_inside_one_complete_keeps_last_result(client: TestClient) -> None:
    job_id = make_job(client, 1, 2, 10)
    start(client, job_id)
    worker_id = register(client)
    chunk = lease(client, worker_id)
    assert chunk is not None

    resp = client.post(f"/api/workers/{worker_id}/complete", json={
        "chunk_id": chunk["chunk_id"], "next_id": 3, "results": [
            {"external_id": 1, "status": "failed", "error_class": "timeout"},
            {"external_id": 1, "status": "found"},
            {"external_id": 2, "status": "not_found"},
        ],
    })

    assert resp.status_code == 200
    items = client.get(f"/api/jobs/{job_id}").json()["items"]
    assert (items["found"], items["not_found"], items["failed"]) == (1, 1, 0)


def test_register_is_upsert_by_name(client: TestClient) -> None:
    first = register(client, threads=8)
    second = register(client, threads=16)

    workers = client.get("/api/workers").json()
    assert first == second
    assert [(w["id"], w["threads"]) for w in workers] == [(first, 16)]


def test_register_accepts_null_limits_and_optional_fields(client: TestClient) -> None:
    resp = client.post("/api/workers/register", json={
        "name": "worker-01", "agent_version": "1.0.0", "protocol_version": 1, "threads": 4,
        "container_id": None, "image": None, "storage_path": "/data",
        "limits": {"cpus": None, "memory_mb": None},
    })
    assert resp.status_code == 201
    body = resp.json()
    assert (body["heartbeat_interval"], body["lease_ttl"]) == (5, 60)


def test_parallel_leases_never_share_a_chunk(client: TestClient) -> None:
    job_id = make_job(client, 1, 100, 1)
    start(client, job_id)
    workers = [register(client, f"worker-{n}", threads=1000) for n in range(4)]

    def drain(worker_id: int) -> list[int]:
        got = []
        while (chunk := lease(client, worker_id)) is not None:
            got.append(chunk["chunk_id"])
        return got

    with ThreadPoolExecutor(len(workers)) as pool:
        leased = [c for part in pool.map(drain, workers) for c in part]

    assert len(leased) == 100
    assert len(set(leased)) == 100


def test_parallel_completes_finish_the_job(client: TestClient) -> None:
    """Гонка возможна только на последних блоках: каждое закрытие не видит незафиксированное другое.

    Поэтому в каждом раунде одновременно закрываются последние два блока задания.
    """
    worker_id = register(client)
    url = f"/api/workers/{worker_id}/complete"
    barrier = Barrier(2)

    def close(chunk: dict[str, Any]) -> int:
        barrier.wait()
        return client.post(url, json=complete_body(chunk)).status_code

    for _ in range(25):
        job_id = make_job(client, 1, 2, 1)
        start(client, job_id)
        chunks = [lease(client, worker_id), lease(client, worker_id)]

        with ThreadPoolExecutor(2) as pool:
            codes = list(pool.map(close, chunks))

        assert set(codes) == {200}
        body = client.get(f"/api/jobs/{job_id}").json()
        assert body["status"] == "completed"
        assert body["processed_ids"] == 2


def test_unknown_job_returns_404(client: TestClient) -> None:
    assert client.get("/api/jobs/999").status_code == 404
    assert client.post("/api/jobs/999/start").status_code == 404


def test_start_twice_returns_409(client: TestClient) -> None:
    job_id = make_job(client, 1, 5, 5)
    start(client, job_id)
    assert client.post(f"/api/jobs/{job_id}/start").status_code == 409


@pytest.mark.parametrize("body", [
    {"range_from": 10, "range_to": 5, "chunk_size": 1},
    {"range_from": -1, "range_to": 5, "chunk_size": 1},
    {"range_from": 0, "range_to": 5, "chunk_size": 0},
    {"range_from": 0, "range_to": 5, "chunk_size": 10_001},
    {"range_from": 0, "range_to": 2**63, "chunk_size": 1},
])
def test_invalid_job_returns_422(client: TestClient, body: dict[str, int]) -> None:
    assert client.post("/api/jobs", json=body).status_code == 422


def test_unknown_worker_returns_404(client: TestClient) -> None:
    assert client.post("/api/workers/999/lease", json={}).status_code == 404
    resp = client.post("/api/workers/999/complete", json={
        "chunk_id": 1, "next_id": 2, "results": [],
    })
    assert resp.status_code == 404


def test_complete_by_other_worker_returns_409(client: TestClient) -> None:
    start(client, make_job(client, 1, 5, 5))
    owner = register(client, "owner")
    other = register(client, "other")
    chunk = lease(client, owner)
    assert chunk is not None

    resp = client.post(f"/api/workers/{other}/complete", json=complete_body(chunk))

    assert resp.status_code == 409


def test_complete_unknown_chunk_returns_409(client: TestClient) -> None:
    worker_id = register(client)
    resp = client.post(f"/api/workers/{worker_id}/complete", json={
        "chunk_id": 999, "next_id": 2, "results": [],
    })
    assert resp.status_code == 409


def test_complete_with_next_id_outside_chunk_returns_422(client: TestClient) -> None:
    start(client, make_job(client, 1, 5, 5))
    worker_id = register(client)
    chunk = lease(client, worker_id)
    assert chunk is not None

    resp = client.post(f"/api/workers/{worker_id}/complete", json={
        "chunk_id": chunk["chunk_id"], "next_id": 7, "results": [],
    })

    assert resp.status_code == 422


def test_complete_with_id_outside_chunk_returns_422_and_keeps_lease(client: TestClient) -> None:
    job_id = make_job(client, 1, 5, 5)
    start(client, job_id)
    worker_id = register(client)
    chunk = lease(client, worker_id)
    assert chunk is not None

    resp = client.post(f"/api/workers/{worker_id}/complete", json={
        "chunk_id": chunk["chunk_id"], "next_id": 6,
        "results": [{"external_id": 6, "status": "found"}],
    })

    assert resp.status_code == 422
    view = client.get(f"/api/jobs/{job_id}").json()
    assert view["chunks_leased"] == 1
    assert sum(view["items"].values()) == 0


def test_complete_with_unknown_status_returns_422(client: TestClient) -> None:
    start(client, make_job(client, 1, 5, 5))
    worker_id = register(client)
    chunk = lease(client, worker_id)
    assert chunk is not None

    resp = client.post(f"/api/workers/{worker_id}/complete", json={
        "chunk_id": chunk["chunk_id"], "next_id": 6,
        "results": [{"external_id": 1, "status": "whatever"}],
    })

    assert resp.status_code == 422
