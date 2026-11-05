"""Heartbeat, аренда и возврат блока с контрольной точкой (ТЗ, п. 6.2)."""
import pytest
from fastapi.testclient import TestClient

from coordinator.config import Settings
from tests.helpers import (
    complete,
    expire_leases,
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


def beat(client: TestClient, worker_id: int, **fields: object) -> dict[str, object]:
    resp = client.post(f"/api/workers/{worker_id}/heartbeat", json=fields)
    assert resp.status_code == 200, resp.text
    return resp.json()


def worker_card(client: TestClient, worker_id: int) -> dict[str, object]:
    return client.get(f"/api/workers/{worker_id}").json()


class TestHeartbeat:
    def test_stores_metrics_and_state(self, client: TestClient) -> None:
        worker = register(client)
        answer = beat(
            client, worker, status="busy", free_slots=2, counters={"checked": 5},
            metrics={"cpu_cores": 0.5, "disk_free_bytes": 1000, "custom_metric": 7},
        )
        assert answer["ok"] is True
        assert answer["lease_ttl"] == Settings().lease_ttl

        card = worker_card(client, worker)
        assert card["agent_state"] == "busy"
        assert card["free_slots"] == 2
        assert card["metrics"]["custom_metric"] == 7
        assert card["counters"] == {"checked": 5}
        rows = sql("SELECT cpu_cores, disk_free_bytes, extra FROM worker_metrics")
        assert rows == [(0.5, 1000, {"custom_metric": 7})]

    def test_empty_heartbeat_is_accepted(self, client: TestClient) -> None:
        assert beat(client, register(client))["ok"] is True
        assert sql("SELECT count(*) FROM worker_metrics") == [(0,)]

    def test_unknown_worker_gets_404_and_must_register_again(self, client: TestClient) -> None:
        assert client.post("/api/workers/999/heartbeat", json={}).status_code == 404

    def test_extends_lease(self, client: TestClient) -> None:
        start_job(client)
        worker = register(client)
        must_lease(client, worker)
        sql("UPDATE range_chunks SET lease_expires_at = now() + interval '1 second'")
        beat(client, worker)
        remaining = sql(
            "SELECT extract(epoch from lease_expires_at - now()) FROM range_chunks"
        )[0][0]
        assert remaining > 30

    def test_returns_worker_online(self, client: TestClient) -> None:
        worker = register(client)
        silence_worker(worker)
        run_dispatcher_tick()
        assert worker_card(client, worker)["status"] == "offline"
        beat(client, worker)
        assert worker_card(client, worker)["status"] == "online"


class TestHeartbeatReconciliation:
    def test_chunk_unknown_to_agent_returns_to_queue(self, client: TestClient) -> None:
        start_job(client)
        worker = register(client)
        chunk = must_lease(client, worker)
        progress(client, worker, chunk, 4)
        sql("UPDATE range_chunks SET leased_at = now() - interval '1 minute'")

        beat(client, worker, active_chunks=[])

        again = must_lease(client, register(client, "other"))
        assert (again["chunk_id"], again["next_id"]) == (chunk["chunk_id"], 4)

    def test_active_chunk_stays_with_agent(self, client: TestClient) -> None:
        start_job(client)
        worker = register(client)
        chunk = must_lease(client, worker)
        sql("UPDATE range_chunks SET leased_at = now() - interval '1 minute'")
        beat(client, worker, active_chunks=[chunk["chunk_id"]])
        assert lease(client, register(client, "other")) is None

    def test_freshly_leased_chunk_is_not_taken_away(self, client: TestClient) -> None:
        start_job(client)
        worker = register(client)
        must_lease(client, worker)
        beat(client, worker, active_chunks=[])
        assert lease(client, register(client, "other")) is None

    def test_agent_without_list_keeps_all_chunks(self, client: TestClient) -> None:
        start_job(client)
        worker = register(client)
        must_lease(client, worker)
        sql("UPDATE range_chunks SET leased_at = now() - interval '1 minute'")
        beat(client, worker)
        assert lease(client, register(client, "other")) is None

    def test_returned_chunk_is_accepted_if_agent_finishes_it_after_all(
        self, client: TestClient,
    ) -> None:
        start_job(client)
        worker = register(client)
        chunk = must_lease(client, worker)
        sql("UPDATE range_chunks SET leased_at = now() - interval '1 minute'")
        beat(client, worker, active_chunks=[])
        assert complete(client, worker, chunk).status_code == 200


class TestLeaseReturn:
    def test_expired_lease_returns_with_checkpoint_to_other_worker(
        self, client: TestClient,
    ) -> None:
        start_job(client)
        first = register(client, "first")
        chunk = must_lease(client, first)
        assert chunk["attempt"] == 1
        assert progress(client, first, chunk, 5).status_code == 200

        expire_leases()
        run_dispatcher_tick()

        second = register(client, "second")
        again = must_lease(client, second)
        assert again["chunk_id"] == chunk["chunk_id"]
        assert again["next_id"] == 5, "сделанное не повторяется"
        assert again["attempt"] == 2

    def test_silent_worker_loses_chunk_before_lease_ends(self, client: TestClient) -> None:
        start_job(client)
        worker = register(client)
        must_lease(client, worker)
        silence_worker(worker)
        run_dispatcher_tick()
        assert must_lease(client, register(client, "other"))["next_id"] == 1

    def test_active_lease_is_not_touched(self, client: TestClient) -> None:
        start_job(client)
        worker = register(client)
        must_lease(client, worker)
        run_dispatcher_tick()
        assert lease(client, register(client, "other")) is None

    def test_chunk_failing_too_many_times_is_marked_failed(self, client: TestClient) -> None:
        job_id = start_job(client)
        worker = register(client)
        for _ in range(Settings().max_chunk_attempts):
            must_lease(client, worker)
            expire_leases()
            run_dispatcher_tick()
        assert lease(client, worker) is None
        body = job_of(client, job_id)
        assert body["status"] == "failed"
        assert body["chunks_failed"] == 1
        assert body["finish_reason"] == "failed_chunks"

    def test_registration_of_restarted_agent_releases_its_chunks(
        self, client: TestClient,
    ) -> None:
        start_job(client)
        worker = register(client)
        chunk = must_lease(client, worker)
        progress(client, worker, chunk, 4)

        assert register(client) == worker, "перезапущенный контейнер сохраняет id"
        again = must_lease(client, worker)
        assert (again["chunk_id"], again["next_id"]) == (chunk["chunk_id"], 4)


class TestLateAgent:
    def test_late_complete_is_accepted_if_nobody_took_the_chunk(self, client: TestClient) -> None:
        job_id = start_job(client)
        worker = register(client)
        chunk = must_lease(client, worker)
        expire_leases()
        run_dispatcher_tick()

        assert complete(client, worker, chunk).status_code == 200
        assert job_of(client, job_id)["status"] == "completed"

    def test_late_complete_is_rejected_after_transfer(self, client: TestClient) -> None:
        start_job(client)
        first = register(client, "first")
        chunk = must_lease(client, first)
        expire_leases()
        run_dispatcher_tick()
        second = register(client, "second")
        again = must_lease(client, second)

        resp = complete(client, first, chunk)
        assert resp.status_code == 409
        assert resp.json()["code"] == "lease_conflict"
        assert complete(client, second, again).status_code == 200

    def test_late_progress_restores_lease_if_nobody_took_the_chunk(
        self, client: TestClient,
    ) -> None:
        start_job(client)
        worker = register(client)
        chunk = must_lease(client, worker)
        expire_leases()
        run_dispatcher_tick()
        assert progress(client, worker, chunk, 3).status_code == 200
        assert lease(client, register(client, "other")) is None


class TestProgress:
    def test_saves_results_and_moves_checkpoint(self, client: TestClient) -> None:
        job_id = start_job(client)
        worker = register(client)
        chunk = must_lease(client, worker)
        results = [{"external_id": i, "status": "not_found"} for i in range(1, 5)]
        answer = progress(client, worker, chunk, 5, results)
        assert answer.json() == {"ok": True, "cancel": False, "rejected": []}
        body = job_of(client, job_id)
        assert body["processed_ids"] == 4
        assert body["items"]["not_found"] == 4

    def test_checkpoint_never_moves_back(self, client: TestClient) -> None:
        job_id = start_job(client)
        worker = register(client)
        chunk = must_lease(client, worker)
        progress(client, worker, chunk, 7)
        progress(client, worker, chunk, 3)
        assert job_of(client, job_id)["processed_ids"] == 6

    def test_progress_of_foreign_chunk_returns_409(self, client: TestClient) -> None:
        start_job(client)
        owner = register(client, "owner")
        chunk = must_lease(client, owner)
        assert progress(client, register(client, "other"), chunk, 3).status_code == 409


class TestPartialComplete:
    def test_unfinished_chunk_returns_to_queue(self, client: TestClient) -> None:
        job_id = start_job(client)
        worker = register(client)
        chunk = must_lease(client, worker)
        assert complete(client, worker, chunk, next_id=4).status_code == 200

        assert job_of(client, job_id)["status"] == "running"
        again = must_lease(client, register(client, "other"))
        assert again["next_id"] == 4

    def test_stale_agent_cannot_move_checkpoint_back(self, client: TestClient) -> None:
        start_job(client)
        worker = register(client)
        chunk = must_lease(client, worker)
        progress(client, worker, chunk, 8)
        complete(client, worker, chunk, next_id=3)
        assert must_lease(client, worker)["next_id"] == 8


class TestStopSignal:
    def test_heartbeat_and_progress_tell_agent_to_cancel(self, client: TestClient) -> None:
        job_id = start_job(client)
        worker = register(client)
        chunk = must_lease(client, worker)
        client.post(f"/api/jobs/{job_id}/stop")

        assert beat(client, worker)["cancel_chunks"] == [chunk["chunk_id"]]
        assert progress(client, worker, chunk, 5).json()["cancel"] is True

        complete(client, worker, chunk, next_id=5)
        body = job_of(client, job_id)
        assert body["status"] == "stopped"
        assert body["processed_ids"] == 4

    def test_pause_does_not_cancel_active_chunk(self, client: TestClient) -> None:
        job_id = start_job(client)
        worker = register(client)
        chunk = must_lease(client, worker)
        client.post(f"/api/jobs/{job_id}/pause")
        assert beat(client, worker)["cancel_chunks"] == []
        assert progress(client, worker, chunk, 5).json()["cancel"] is False

    def test_silent_agent_is_cut_off_after_grace_period(self, client: TestClient) -> None:
        job_id = start_job(client)
        worker = register(client)
        chunk = must_lease(client, worker)
        client.post(f"/api/jobs/{job_id}/stop")
        run_dispatcher_tick()
        assert job_of(client, job_id)["status"] == "stopping", "льготное время ещё не вышло"

        sql("UPDATE jobs SET stop_requested_at = now() - interval '1 hour'")
        run_dispatcher_tick()
        assert job_of(client, job_id)["status"] == "stopped"

        # Поздний ответ сохраняет работу, но задание остаётся остановленным.
        assert complete(client, worker, chunk, next_id=6).status_code == 200
        body = job_of(client, job_id)
        assert body["status"] == "stopped"
        assert body["processed_ids"] == 5
