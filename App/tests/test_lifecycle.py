"""Таблица переходов задания и жизненный цикл через API (ТЗ, п. 19)."""
from typing import Any

import pytest
from fastapi.testclient import TestClient

from coordinator.domain.statuses import JOB_TRANSITIONS, JobAction, JobStatus, next_status

S = JobStatus
A = JobAction

ALLOWED = {
    (S.CREATED, A.START): S.VALIDATING,
    (S.VALIDATING, A.VALIDATED): S.RUNNING,
    (S.VALIDATING, A.FAIL): S.FAILED,
    (S.RUNNING, A.PAUSE): S.PAUSING,
    (S.RUNNING, A.STOP): S.STOPPING,
    (S.RUNNING, A.FINISH): S.COMPLETED,
    (S.RUNNING, A.FAIL): S.FAILED,
    (S.PAUSING, A.DRAINED): S.PAUSED,
    (S.PAUSING, A.RESUME): S.RUNNING,
    (S.PAUSING, A.STOP): S.STOPPING,
    (S.PAUSING, A.FINISH): S.COMPLETED,
    (S.PAUSING, A.FAIL): S.FAILED,
    (S.PAUSED, A.RESUME): S.RUNNING,
    (S.PAUSED, A.STOP): S.STOPPING,
    (S.STOPPING, A.DRAINED): S.STOPPED,
    (S.STOPPED, A.RESTART): S.RUNNING,
    (S.COMPLETED, A.RESTART): S.RUNNING,
    (S.FAILED, A.RESTART): S.RUNNING,
}


def test_table_matches_specification() -> None:
    assert JOB_TRANSITIONS == ALLOWED


@pytest.mark.parametrize("status", list(S))
@pytest.mark.parametrize("action", list(A))
def test_every_pair_of_status_and_action(status: JobStatus, action: JobAction) -> None:
    assert next_status(status, action) == ALLOWED.get((status, action))


@pytest.mark.parametrize("status", [S.COMPLETED, S.FAILED, S.STOPPED])
@pytest.mark.parametrize("action", [A.START, A.RESUME, A.PAUSE, A.VALIDATED])
def test_finished_job_returns_to_work_only_by_restart(status: JobStatus, action: JobAction) -> None:
    assert next_status(status, action) is None


def make_job(client: TestClient, **fields: Any) -> dict[str, Any]:
    resp = client.post("/api/jobs", json={"range": "[1,4]", "chunk_size": 2, **fields})
    assert resp.status_code == 201, resp.text
    return resp.json()


def register(client: TestClient, name: str = "worker-01") -> int:
    return client.post("/api/workers/register", json={
        "name": name, "agent_version": "t", "protocol_version": 1, "threads": 1,
    }).json()["worker_id"]


def lease(client: TestClient, worker_id: int) -> dict[str, Any] | None:
    resp = client.post(f"/api/workers/{worker_id}/lease", json={})
    return None if resp.status_code == 204 else resp.json()


def close(client: TestClient, worker_id: int, chunk: dict[str, Any]) -> None:
    resp = client.post(f"/api/workers/{worker_id}/complete", json={
        "chunk_id": chunk["chunk_id"], "next_id": chunk["range_end"] + 1, "results": [],
    })
    assert resp.status_code == 200, resp.text


def action(client: TestClient, job_id: int, name: str) -> Any:
    return client.post(f"/api/jobs/{job_id}/{name}")


def status_of(client: TestClient, job_id: int) -> str:
    return client.get(f"/api/jobs/{job_id}").json()["status"]


@pytest.mark.integration
class TestLifecycle:
    def test_pause_without_active_chunks_is_immediate(self, client: TestClient) -> None:
        job = make_job(client)
        action(client, job["id"], "start")
        assert action(client, job["id"], "pause").json()["status"] == "paused"

    def test_soft_pause_lets_active_chunk_finish_but_gives_no_new_ones(
        self, client: TestClient,
    ) -> None:
        job = make_job(client)
        action(client, job["id"], "start")
        worker = register(client)
        chunk = lease(client, worker)
        assert chunk is not None

        assert action(client, job["id"], "pause").json()["status"] == "pausing"
        assert lease(client, worker) is None, "во время паузы новые блоки не выдаются"

        close(client, worker, chunk)
        assert status_of(client, job["id"]) == "paused"
        assert lease(client, worker) is None

    def test_resume_gives_blocks_again(self, client: TestClient) -> None:
        job = make_job(client)
        action(client, job["id"], "start")
        action(client, job["id"], "pause")
        assert action(client, job["id"], "resume").json()["status"] == "running"
        assert lease(client, register(client)) is not None

    def test_resume_during_pausing_keeps_lease(self, client: TestClient) -> None:
        job = make_job(client)
        action(client, job["id"], "start")
        worker = register(client)
        chunk = lease(client, worker)
        assert chunk is not None
        action(client, job["id"], "pause")
        assert action(client, job["id"], "resume").json()["status"] == "running"
        close(client, worker, chunk)

    def test_last_chunk_during_pausing_completes_job(self, client: TestClient) -> None:
        job = make_job(client, range="[1,2]")
        action(client, job["id"], "start")
        worker = register(client)
        chunk = lease(client, worker)
        assert chunk is not None
        action(client, job["id"], "pause")
        close(client, worker, chunk)
        assert status_of(client, job["id"]) == "completed"

    def test_stop_without_active_chunks_is_immediate(self, client: TestClient) -> None:
        job = make_job(client)
        action(client, job["id"], "start")
        stopped = action(client, job["id"], "stop").json()
        assert stopped["status"] == "stopped"
        assert stopped["finish_reason"] == "stopped_by_user"
        assert stopped["finished_at"] is not None

    def test_stop_waits_for_active_chunk(self, client: TestClient) -> None:
        job = make_job(client)
        action(client, job["id"], "start")
        worker = register(client)
        chunk = lease(client, worker)
        assert chunk is not None
        assert action(client, job["id"], "stop").json()["status"] == "stopping"
        assert lease(client, worker) is None
        close(client, worker, chunk)
        assert status_of(client, job["id"]) == "stopped"

    def test_restart_after_stop_continues_from_checkpoint(self, client: TestClient) -> None:
        job = make_job(client)
        action(client, job["id"], "start")
        worker = register(client)
        first = lease(client, worker)
        assert first is not None
        close(client, worker, first)
        action(client, job["id"], "stop")

        restarted = action(client, job["id"], "restart").json()
        assert restarted["status"] == "running"
        assert restarted["run_attempt"] == 2
        assert restarted["finished_at"] is None

        second = lease(client, worker)
        assert second is not None
        assert second["range_start"] == 3, "обработанный блок не выдаётся повторно"
        close(client, worker, second)
        assert status_of(client, job["id"]) == "completed"

    def test_restart_of_completed_job_without_work_completes_again(
        self, client: TestClient,
    ) -> None:
        job = make_job(client, range="[1,2]")
        action(client, job["id"], "start")
        worker = register(client)
        chunk = lease(client, worker)
        assert chunk is not None
        close(client, worker, chunk)
        assert action(client, job["id"], "restart").json()["status"] == "completed"

    @pytest.mark.parametrize("name", ["pause", "resume", "stop", "restart"])
    def test_forbidden_action_on_created_job_returns_409(
        self, client: TestClient, name: str,
    ) -> None:
        job = make_job(client)
        resp = action(client, job["id"], name)
        assert resp.status_code == 409
        assert resp.json()["code"] == "job_state_conflict"

    def test_start_of_completed_job_is_rejected(self, client: TestClient) -> None:
        job = make_job(client, range="[1,2]")
        action(client, job["id"], "start")
        worker = register(client)
        chunk = lease(client, worker)
        assert chunk is not None
        close(client, worker, chunk)
        assert action(client, job["id"], "start").status_code == 409
        assert action(client, job["id"], "resume").status_code == 409


@pytest.mark.integration
class TestJobParameters:
    def test_all_range_formats_are_accepted(self, client: TestClient) -> None:
        cases = [("[5,10]", 5, 10), ("100", 0, 100), ("[7,]", 7, None), ("", 0, None)]
        for text, start, end in cases:
            body = make_job(client, range=text)
            assert (body["range_from"], body["range_to"]) == (start, end)
        open_range = client.get("/api/jobs").json()[0]
        assert open_range["total_ids"] is None
        assert open_range["chunks_total"] is None

    def test_bounds_are_equivalent_to_range_string(self, client: TestClient) -> None:
        resp = client.post("/api/jobs", json={"range_from": 5, "range_to": 10})
        assert (resp.json()["range_from"], resp.json()["range_to"]) == (5, 10)
        resp = client.post("/api/jobs", json={"range_from": 5})
        assert resp.json()["range_to"] is None

    @pytest.mark.parametrize("text", ["[10,5]", "abc", "-1"])
    def test_invalid_range_returns_422(self, client: TestClient, text: str) -> None:
        resp = client.post("/api/jobs", json={"range": text})
        assert resp.status_code == 422
        assert resp.json()["code"] == "validation_error"

    def test_huge_range_is_created_instantly_and_lazily(self, client: TestClient) -> None:
        body = make_job(client, range=f"[0,{10**15}]", chunk_size=1)
        assert body["chunks_total"] == 10**15 + 1
        started = action(client, body["id"], "start").json()
        assert started["status"] == "running"

    def test_parameters_are_stored(self, client: TestClient) -> None:
        register(client, "worker-01")
        body = make_job(
            client, threads=4, request_timeout_seconds=5.5, max_retries=2,
            global_rate_limit_rps=20, max_ids=100, max_duration_seconds=60,
            stop_on_consecutive_missing=7, workers=[{"name": "worker-01", "threads": 8}],
        )
        assert body["threads"] == 4
        assert body["workers"] == {"worker-01": 8}
        assert body["request_timeout_seconds"] == 5.5
        assert body["stop_on_consecutive_missing"] == 7

    def test_consecutive_missing_condition_is_off_by_default(self, client: TestClient) -> None:
        assert make_job(client)["stop_on_consecutive_missing"] is None

    def test_unknown_worker_in_job_returns_422(self, client: TestClient) -> None:
        resp = client.post("/api/jobs", json={"range": "1", "workers": [{"name": "ghost"}]})
        assert resp.status_code == 422
        assert resp.json()["code"] == "invalid_job"

    def test_job_with_workers_is_leased_only_to_them(self, client: TestClient) -> None:
        register(client, "assigned")
        other = register(client, "other")
        assigned = register(client, "assigned")
        job = make_job(client, workers=[{"name": "assigned"}])
        action(client, job["id"], "start")
        assert lease(client, other) is None
        assert lease(client, assigned) is not None
