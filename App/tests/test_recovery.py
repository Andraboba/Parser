"""Открытый диапазон и восстановление после сбоев (ТЗ, п. 10 и 13)."""
import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from coordinator.api.app import create_app
from coordinator.config import Settings
from coordinator.dispatcher.ticks import grant_amnesty
from coordinator.storage import db
from tests.conftest import FLEET_TOKEN, login
from tests.helpers import (
    complete,
    job_of,
    lease,
    must_lease,
    progress,
    register,
    run_dispatcher_tick,
    silence_worker,
    sql,
    start_job,
)

pytestmark = pytest.mark.integration


def amnesty() -> None:
    async def run() -> None:
        settings = Settings()
        engine = db.make_engine(settings.database_url)
        try:
            async with db.make_sessionmaker(engine)() as session:
                await grant_amnesty(session, settings)
        finally:
            await engine.dispose()

    asyncio.run(run())


def results_for(chunk: dict[str, Any], status: str = "not_found") -> list[dict[str, Any]]:
    return [
        {"external_id": i, "status": status}
        for i in range(chunk["range_start"], chunk["range_end"] + 1)
    ]


class TestOpenRange:
    def test_new_chunks_appear_as_old_ones_are_taken(self, client: TestClient) -> None:
        start_job(client, range="[5,]", chunk_size=2)
        worker = register(client, threads=500)
        chunks = [must_lease(client, worker) for _ in range(120)]
        starts = [c["range_start"] for c in chunks]
        assert starts == list(range(5, 5 + 2 * 120, 2)), "без пропусков и повторов"
        assert sql("SELECT count(*) FROM range_chunks")[0][0] >= 120

    def test_keeps_running_without_conditions(self, client: TestClient) -> None:
        job_id = start_job(client, range="", chunk_size=5)
        worker = register(client)
        for _ in range(5):
            complete(client, worker, must_lease(client, worker))
            run_dispatcher_tick()
        body = job_of(client, job_id)
        assert body["status"] == "running"
        assert body["total_ids"] is None
        assert body["processed_ids"] == 25

    def test_stops_by_user(self, client: TestClient) -> None:
        job_id = start_job(client, range="[1,]", chunk_size=5)
        client.post(f"/api/jobs/{job_id}/stop")
        assert job_of(client, job_id)["status"] == "stopped"

    def test_max_ids_ends_exactly_at_limit(self, client: TestClient) -> None:
        job_id = start_job(client, range="[10,]", chunk_size=4, max_ids=10)
        worker = register(client)
        ranges = []
        while (chunk := lease(client, worker)) is not None:
            ranges.append((chunk["range_start"], chunk["range_end"]))
            complete(client, worker, chunk)
        assert ranges == [(10, 13), (14, 17), (18, 19)]
        body = job_of(client, job_id)
        assert body["status"] == "completed"
        assert body["finish_reason"] == "max_ids"
        assert body["total_ids"] == 10

    def test_max_ids_also_limits_finite_range(self, client: TestClient) -> None:
        job_id = start_job(client, range="[1,100]", chunk_size=4, max_ids=4)
        worker = register(client)
        complete(client, worker, must_lease(client, worker))
        assert job_of(client, job_id)["finish_reason"] == "max_ids"

    def test_time_limit_stops_job_and_cancels_active_chunk(self, client: TestClient) -> None:
        job_id = start_job(client, range="[1,]", chunk_size=5, max_duration_seconds=60)
        worker = register(client)
        chunk = must_lease(client, worker)
        run_dispatcher_tick()
        assert job_of(client, job_id)["status"] == "running"

        sql("UPDATE jobs SET started_at = now() - interval '2 minutes'")
        run_dispatcher_tick()
        assert job_of(client, job_id)["status"] == "stopping"
        assert progress(client, worker, chunk, 2).json()["cancel"] is True
        complete(client, worker, chunk, next_id=2)
        body = job_of(client, job_id)
        assert (body["status"], body["finish_reason"]) == ("stopped", "max_duration")

    def test_consecutive_missing_stops_job(self, client: TestClient) -> None:
        job_id = start_job(
            client, range="[1,]", chunk_size=5, stop_on_consecutive_missing=7,
        )
        worker = register(client)
        first = must_lease(client, worker)
        complete(client, worker, first, results=results_for(first))
        run_dispatcher_tick()
        assert job_of(client, job_id)["status"] == "running", "пока только 5 подряд"

        second = must_lease(client, worker)
        complete(client, worker, second, results=results_for(second))
        run_dispatcher_tick()
        body = job_of(client, job_id)
        assert (body["status"], body["finish_reason"]) == ("stopped", "consecutive_missing")

    def test_found_item_resets_consecutive_missing(self, client: TestClient) -> None:
        job_id = start_job(
            client, range="[1,]", chunk_size=5, stop_on_consecutive_missing=7,
        )
        worker = register(client)
        first = must_lease(client, worker)
        complete(client, worker, first, results=results_for(first))
        second = must_lease(client, worker)
        results = results_for(second)
        results[-1]["status"] = "found"
        complete(client, worker, second, results=results)
        run_dispatcher_tick()
        assert job_of(client, job_id)["status"] == "running"

    def test_hole_ahead_does_not_count_as_missing(self, client: TestClient) -> None:
        job_id = start_job(
            client, range="[1,]", chunk_size=5, stop_on_consecutive_missing=7,
        )
        worker = register(client, threads=8)
        first = must_lease(client, worker)
        second = must_lease(client, worker)
        complete(client, worker, second, results=results_for(second))
        third = must_lease(client, worker)
        complete(client, worker, third, results=results_for(third))
        run_dispatcher_tick()
        assert job_of(client, job_id)["status"] == "running", "первый блок ещё не закрыт"
        assert first["range_start"] == 1

    def test_no_workers_for_long_time_stops_open_range(self, client: TestClient) -> None:
        job_id = start_job(client, range="[1,]", chunk_size=5)
        worker = register(client)
        silence_worker(worker)
        run_dispatcher_tick()
        assert job_of(client, job_id)["status"] == "running", "начинается отсчёт ожидания"

        sql("UPDATE jobs SET no_workers_since = now() - interval '1 hour'")
        run_dispatcher_tick()
        body = job_of(client, job_id)
        assert (body["status"], body["finish_reason"]) == ("stopped", "no_workers")

    def test_returning_worker_resets_wait(self, client: TestClient) -> None:
        job_id = start_job(client, range="[1,]", chunk_size=5)
        worker = register(client)
        silence_worker(worker)
        run_dispatcher_tick()
        client.post(f"/api/workers/{worker}/heartbeat", json={})
        run_dispatcher_tick()
        assert sql("SELECT no_workers_since FROM jobs WHERE id = :id", id=job_id) == [(None,)]

    def test_finite_range_waits_for_workers_indefinitely(self, client: TestClient) -> None:
        job_id = start_job(client, range="[1,10]")
        worker = register(client)
        silence_worker(worker)
        run_dispatcher_tick()
        sql("UPDATE jobs SET no_workers_since = now() - interval '1 hour'")
        run_dispatcher_tick()
        assert job_of(client, job_id)["status"] == "running"


class TestDatabaseOutage:
    @pytest.fixture
    def broken(self) -> Any:
        settings = Settings(database_url="postgresql+psycopg://x:x@127.0.0.1:1/x")
        with TestClient(create_app(settings)) as test_client:
            test_client.headers["X-Agent-Token"] = FLEET_TOKEN
            yield test_client

    @pytest.mark.parametrize(("method", "path"), [
        ("post", "/api/workers/register"), ("post", "/api/workers/1/heartbeat"),
        ("post", "/api/workers/1/lease"), ("post", "/api/workers/1/complete"),
    ])
    def test_agent_gets_503_with_retry_after(
        self, broken: TestClient, method: str, path: str,
    ) -> None:
        payload = {
            "/api/workers/register": {
                "name": "w", "agent_version": "1", "protocol_version": 1, "threads": 1,
            },
            "/api/workers/1/complete": {"chunk_id": 1, "next_id": 2, "results": []},
        }.get(path, {})
        resp = getattr(broken, method)(path, json=payload)
        assert resp.status_code == 503
        assert resp.headers["Retry-After"] == "2"
        assert resp.json()["code"] == "db_unavailable"

    def test_interface_gets_503(self, broken: TestClient) -> None:
        broken.cookies.set("coord_session", "any")
        assert broken.get("/api/jobs").status_code == 503


class TestRestart:
    def test_new_coordinator_instance_continues_from_checkpoint(self, clean_db: None) -> None:
        with TestClient(create_app(Settings())) as first:
            first.headers["X-Agent-Token"] = FLEET_TOKEN
            login(first)
            job_id = start_job(first, range="[1,10]")
            worker = register(first)
            chunk = must_lease(first, worker)
            progress(first, worker, chunk, 5, results_for({**chunk, "range_end": 4}))

        with TestClient(create_app(Settings())) as second:
            second.headers["X-Agent-Token"] = FLEET_TOKEN
            login(second)
            body = job_of(second, job_id)
            assert body["status"] == "running"
            assert body["processed_ids"] == 4
            assert body["items"]["not_found"] == 4
            assert complete(second, worker, chunk).status_code == 200
            assert job_of(second, job_id)["status"] == "completed"

    def test_amnesty_keeps_silent_agents_after_outage(self, client: TestClient) -> None:
        start_job(client)
        worker = register(client)
        chunk = must_lease(client, worker)
        silence_worker(worker)
        sql("UPDATE range_chunks SET lease_expires_at = now() - interval '1 minute'")

        amnesty()
        run_dispatcher_tick()

        assert client.get(f"/api/workers/{worker}").json()["status"] == "online"
        assert lease(client, register(client, "other")) is None, "блок остался у владельца"
        assert complete(client, worker, chunk).status_code == 200

    def test_without_amnesty_silent_agent_loses_chunk(self, client: TestClient) -> None:
        start_job(client)
        worker = register(client)
        must_lease(client, worker)
        silence_worker(worker)
        run_dispatcher_tick()
        assert lease(client, register(client, "other")) is not None

    def test_amnesty_does_not_revive_workers_known_as_offline(self, client: TestClient) -> None:
        worker = register(client)
        silence_worker(worker)
        run_dispatcher_tick()
        amnesty()
        assert client.get(f"/api/workers/{worker}").json()["status"] == "offline"

    def test_recreated_container_keeps_identity_with_new_address(
        self, client: TestClient,
    ) -> None:
        worker = register(client)
        sql("UPDATE workers SET last_ip = '10.0.0.1', container_id = 'old'")
        assert register(client) == worker
        card = client.get(f"/api/workers/{worker}").json()
        assert card["last_ip"] != "10.0.0.1"
