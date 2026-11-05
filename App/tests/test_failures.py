"""Отказ от блока, классификация ошибок и повтор из интерфейса (ТЗ, п. 7, 9, 11.3)."""
from typing import Any

import pytest
from fastapi.testclient import TestClient

from coordinator.config import Settings
from coordinator.domain.retry import error_group, retry_delay
from tests.helpers import complete, job_of, lease, must_lease, register, sql, start_job


@pytest.mark.parametrize(("attempt", "expected"), [
    (0, 1.0), (1, 2.0), (3, 8.0), (6, 60.0), (100, 60.0),
])
def test_retry_delay_is_exponential_with_upper_bound(attempt: int, expected: float) -> None:
    assert retry_delay(attempt, jitter=0.0) == expected


def test_retry_delay_jitter_is_added_and_bounded() -> None:
    delays = {retry_delay(2) for _ in range(50)}
    assert len(delays) > 1, "случайное отклонение разводит повторы узлов"
    assert all(4.0 <= d <= 4.4 for d in delays)


@pytest.mark.parametrize(("error_class", "group"), [
    ("network", "network"), ("server_error", "server"), ("http_503", "server"),
    ("http_418", "client"), ("forbidden", "access"), ("insufficient_space", "disk"),
    ("invalid_path", "security"), ("something_new", "other"), (None, None),
])
def test_error_groups(error_class: str | None, group: str | None) -> None:
    assert error_group(error_class) == group


def fail(
    client: TestClient, worker: int, chunk: dict[str, Any], error_class: str = "network",
    key: str | None = None, next_id: int | None = None,
) -> Any:
    return client.post(
        f"/api/workers/{worker}/fail", headers={"Idempotency-Key": key} if key else {},
        json={"chunk_id": chunk["chunk_id"], "error_class": error_class, "message": "сбой",
              "next_id": next_id},
    )


@pytest.mark.integration
class TestFail:
    def test_chunk_returns_to_queue_after_backoff(self, client: TestClient) -> None:
        start_job(client)
        worker = register(client)
        chunk = must_lease(client, worker)
        assert fail(client, worker, chunk).json() == {"ok": True}
        assert lease(client, worker) is None, "повтор только после задержки"

        sql("UPDATE range_chunks SET not_before = now() - interval '1 second'")
        again = must_lease(client, worker)
        assert again["chunk_id"] == chunk["chunk_id"]
        assert again["attempt"] == 2

    def test_checkpoint_from_fail_is_kept(self, client: TestClient) -> None:
        start_job(client)
        worker = register(client)
        chunk = must_lease(client, worker)
        fail(client, worker, chunk, next_id=6)
        sql("UPDATE range_chunks SET not_before = NULL")
        assert must_lease(client, worker)["next_id"] == 6

    def test_exhausted_attempts_fail_chunk_and_job(self, client: TestClient) -> None:
        job_id = start_job(client)
        worker = register(client)
        for _ in range(Settings().max_chunk_attempts):
            sql("UPDATE range_chunks SET not_before = NULL")
            fail(client, worker, must_lease(client, worker))
        body = job_of(client, job_id)
        assert body["status"] == "failed"
        assert body["chunks_failed"] == 1

    def test_configuration_error_fails_job_immediately(self, client: TestClient) -> None:
        job_id = start_job(client, range="[1,100]")
        worker = register(client)
        fail(client, worker, must_lease(client, worker), "config_error")
        body = job_of(client, job_id)
        assert body["status"] == "failed"
        assert body["finish_reason"] == "config_error"
        assert lease(client, worker) is None

    def test_same_key_has_no_second_effect(self, client: TestClient) -> None:
        start_job(client)
        worker = register(client)
        chunk = must_lease(client, worker)
        fail(client, worker, chunk, key="fail-1")
        sql("UPDATE range_chunks SET not_before = NULL, error_class = 'marker'")
        assert fail(client, worker, chunk, "other", key="fail-1").status_code == 200
        assert sql("SELECT error_class FROM range_chunks") == [("marker",)]

    def test_same_key_on_new_attempt_counts_again(self, client: TestClient) -> None:
        job_id = start_job(client)
        worker = register(client)
        for _ in range(Settings().max_chunk_attempts):
            sql("UPDATE range_chunks SET not_before = NULL")
            fail(client, worker, must_lease(client, worker), key="fail-same")
        assert job_of(client, job_id)["status"] == "failed"

    def test_foreign_worker_cannot_fail_chunk(self, client: TestClient) -> None:
        start_job(client)
        chunk = must_lease(client, register(client, "owner"))
        assert fail(client, register(client, "other"), chunk).status_code == 409


@pytest.mark.integration
class TestRetryErrors:
    def finished_job(self, client: TestClient) -> tuple[int, int]:
        job_id = start_job(client, range="[1,8]")
        worker = register(client)
        chunk = must_lease(client, worker)
        complete(client, worker, chunk, results=[
            {"external_id": 1, "status": "not_found"},
            {"external_id": 2, "status": "failed", "error_class": "server_error"},
            {"external_id": 3, "status": "rate_limited"},
            {"external_id": 4, "status": "retry_wait"},
            {"external_id": 5, "status": "forbidden", "http_status": 403},
            {"external_id": 6, "status": "completed"},
            {"external_id": 7, "status": "cancelled"},
            {"external_id": 8, "status": "found"},
        ])
        assert job_of(client, job_id)["status"] == "completed"
        return job_id, worker

    def test_only_error_items_are_requeued_as_contiguous_chunks(self, client: TestClient) -> None:
        job_id, worker = self.finished_job(client)
        answer = client.post(f"/api/jobs/{job_id}/retry-errors").json()
        assert answer["items_requeued"] == 4
        assert answer["chunks_created"] == 2
        assert answer["job"]["status"] == "running"
        assert answer["job"]["run_attempt"] == 2

        first = must_lease(client, worker)
        second = must_lease(client, worker)
        assert (first["range_start"], first["range_end"]) == (2, 4)
        assert (second["range_start"], second["range_end"]) == (7, 7)

    def test_forbidden_and_completed_are_never_retried(self, client: TestClient) -> None:
        job_id, _ = self.finished_job(client)
        client.post(f"/api/jobs/{job_id}/retry-errors")
        items = job_of(client, job_id)["items"]
        assert items["forbidden"] == 1
        assert items["completed"] == 1
        assert items["pending"] == 4

    def test_progress_is_not_counted_twice(self, client: TestClient) -> None:
        job_id, worker = self.finished_job(client)
        client.post(f"/api/jobs/{job_id}/retry-errors")
        for _ in range(2):
            complete(client, worker, must_lease(client, worker))
        body = job_of(client, job_id)
        assert body["status"] == "completed"
        assert body["processed_ids"] == 8

    def test_failed_chunks_are_requeued(self, client: TestClient) -> None:
        job_id = start_job(client)
        worker = register(client)
        for _ in range(Settings().max_chunk_attempts):
            sql("UPDATE range_chunks SET not_before = NULL")
            fail(client, worker, must_lease(client, worker))
        answer = client.post(f"/api/jobs/{job_id}/retry-errors").json()
        assert answer["chunks_requeued"] == 1
        assert answer["job"]["status"] == "running"
        assert must_lease(client, worker)["attempt"] == 1

    def test_second_retry_uses_new_round(self, client: TestClient) -> None:
        job_id, worker = self.finished_job(client)
        client.post(f"/api/jobs/{job_id}/retry-errors")
        for _ in range(2):
            chunk = must_lease(client, worker)
            complete(client, worker, chunk, results=[
                {"external_id": i, "status": "failed"}
                for i in range(chunk["range_start"], chunk["range_end"] + 1)
            ])
        answer = client.post(f"/api/jobs/{job_id}/retry-errors").json()
        assert answer["chunks_created"] == 2
        assert sql("SELECT DISTINCT retry_round FROM range_chunks ORDER BY 1") == [
            (0,), (1,), (2,),
        ]

    def test_nothing_to_retry_keeps_job_completed(self, client: TestClient) -> None:
        job_id = start_job(client, range="[1,1]")
        worker = register(client)
        complete(client, worker, must_lease(client, worker), results=[
            {"external_id": 1, "status": "not_found"},
        ])
        answer = client.post(f"/api/jobs/{job_id}/retry-errors").json()
        assert answer["items_requeued"] == 0
        assert answer["job"]["status"] == "completed"

    def test_job_that_never_started_cannot_be_retried(self, client: TestClient) -> None:
        job_id = client.post("/api/jobs", json={"range": "1"}).json()["id"]
        assert client.post(f"/api/jobs/{job_id}/retry-errors").status_code == 409
