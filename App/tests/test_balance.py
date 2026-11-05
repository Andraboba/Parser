"""Балансировка по ресурсам узла и централизованный лимит запросов (ТЗ, п. 6.3 и 9)."""
from typing import Any

import pytest
from fastapi.testclient import TestClient

from coordinator.config import Settings
from coordinator.domain.balance import (
    NodeState,
    Thresholds,
    chunk_quota,
    effective_threads,
    refusal_reason,
)
from coordinator.domain.rps import fair_shares, next_limit
from tests.helpers import complete, lease, must_lease, register, run_dispatcher_tick, sql, start_job

LIMITS = Thresholds()
HEALTHY = NodeState(enabled=True, free_slots=4, threads=8, leased_chunks=0)


def state(**changes: Any) -> NodeState:
    return NodeState(**{**HEALTHY.__dict__, **changes})


@pytest.mark.parametrize(("changes", "reason"), [
    ({}, None),
    ({"enabled": False}, "disabled"),
    ({"free_slots": 0}, "no_free_slots"),
    ({"leased_chunks": 1}, "chunk_quota"),
    ({"disk_free_bytes": 10, "min_free_bytes": 100}, "disk_low"),
    ({"disk_free_bytes": 100, "min_free_bytes": 100}, None),
    ({"cpu_limit": 1.0, "cpu_cores_avg": 0.99}, "cpu_overload"),
    ({"cpu_limit": 1.0, "cpu_cores_avg": 0.5}, None),
    ({"cpu_limit": None, "cpu_cores_avg": 50.0}, None),
    ({"memory_limit": 100, "memory_bytes": 99}, "memory_overload"),
    ({"memory_limit": 100, "memory_bytes": 50}, None),
    ({"checked_recently": 100, "failed_recently": 60}, "too_many_errors"),
    ({"checked_recently": 100, "failed_recently": 40}, None),
    ({"checked_recently": 5, "failed_recently": 5}, None),
])
def test_refusal_reasons(changes: dict[str, Any], reason: str | None) -> None:
    assert refusal_reason(state(**changes), LIMITS) == reason


def test_cpu_limit_reduces_effective_threads() -> None:
    assert effective_threads(16, None, 8) == 16
    assert effective_threads(16, 0.5, 8) == 4.0
    assert effective_threads(2, 2.0, 8) == 2
    assert effective_threads(16, 0.01, 8) == 1.0


def test_weak_node_gets_fewer_chunks_than_strong_one() -> None:
    weak = state(threads=32, cpu_limit=0.5)
    strong = state(threads=32, cpu_limit=None)
    assert chunk_quota(weak, LIMITS) == 1
    assert chunk_quota(strong, LIMITS) == 2


@pytest.mark.parametrize(("current", "throttled", "expected"), [
    (20.0, 3, 10.0),
    (10.0, 1, 5.0),
    (1.0, 1, 1.0),
    (10.0, 0, 11.0),
    (19.5, 0, 20.0),
    (20.0, 0, 20.0),
    (40.0, 0, 20.0),
    (40.0, 2, 10.0),
])
def test_limit_falls_on_throttling_and_recovers_slowly(
    current: float, throttled: int, expected: float,
) -> None:
    assert next_limit(current, 20.0, throttled) == expected


def test_limit_converges_after_series_of_429() -> None:
    limit = 20.0
    for _ in range(10):
        limit = next_limit(limit, 20.0, 1)
    assert limit == 1.0, "ниже 5 % от базы лимит не опускается"
    for _ in range(40):
        limit = next_limit(limit, 20.0, 0)
    assert limit == 20.0


def test_shares_follow_weights_and_sum_stays_within_total() -> None:
    shares = fair_shares(26.0, {1: 8, 2: 16, 3: 2}, {})
    assert shares == pytest.approx({1: 8.0, 2: 16.0, 3: 2.0})


def test_capped_worker_gives_leftover_to_others() -> None:
    shares = fair_shares(30.0, {1: 1, 2: 1, 3: 1}, {1: 2.0})
    assert shares[1] == 2.0
    assert shares[2] == pytest.approx(14.0)
    assert sum(shares.values()) == pytest.approx(30.0)


def test_when_everyone_is_capped_sum_is_below_total() -> None:
    shares = fair_shares(30.0, {1: 1, 2: 1}, {1: 2.0, 2: 3.0})
    assert shares == {1: 2.0, 2: 3.0}


def test_empty_and_zero_weights() -> None:
    assert fair_shares(30.0, {}, {}) == {}
    assert fair_shares(30.0, {1: 0}, {}) == {1: 0.0}


@pytest.mark.parametrize("total", [0.1, 1, 20, 1000])
def test_sum_never_exceeds_total(total: float) -> None:
    shares = fair_shares(total, {i: i + 1 for i in range(50)}, {7: 0.5, 9: 100.0})
    assert sum(shares.values()) <= total * (1 + 1e-9)


def beat(client: TestClient, worker: int, **fields: Any) -> dict[str, Any]:
    resp = client.post(f"/api/workers/{worker}/heartbeat", json=fields)
    assert resp.status_code == 200, resp.text
    return resp.json()


def lease_response(client: TestClient, worker: int, free_slots: int = 4) -> Any:
    return client.post(f"/api/workers/{worker}/lease", json={"free_slots": free_slots})


def card(client: TestClient, worker: int) -> dict[str, Any]:
    return client.get(f"/api/workers/{worker}").json()


@pytest.mark.integration
class TestLeaseRefusal:
    def test_low_disk_stops_new_chunks_and_is_visible(self, client: TestClient) -> None:
        start_job(client)
        worker = register(client)
        beat(client, worker, metrics={"disk_free_bytes": 1000})

        resp = lease_response(client, worker)
        assert resp.status_code == 204
        assert resp.headers["X-Lease-Refused"] == "disk_low"
        assert card(client, worker)["lease_block_reason"] == "disk_low"

    def test_recovery_clears_refusal(self, client: TestClient) -> None:
        start_job(client)
        worker = register(client)
        beat(client, worker, metrics={"disk_free_bytes": 1000})
        assert lease_response(client, worker).status_code == 204
        beat(client, worker, metrics={"disk_free_bytes": 10 * 1024**3})
        assert lease_response(client, worker).status_code == 200
        assert card(client, worker)["lease_block_reason"] is None

    def test_operator_can_lower_reserve_for_node(self, client: TestClient) -> None:
        start_job(client)
        worker = register(client)
        beat(client, worker, metrics={"disk_free_bytes": 1000})
        client.patch(f"/api/workers/{worker}", json={"min_free_space_gb": 0})
        assert lease_response(client, worker).status_code == 200

    def test_zero_free_slots_gets_no_chunk(self, client: TestClient) -> None:
        start_job(client)
        assert lease_response(client, register(client), free_slots=0).status_code == 204

    def test_second_chunk_is_not_given_to_small_node(self, client: TestClient) -> None:
        start_job(client, range="[1,10]", chunk_size=1)
        worker = register(client, threads=1)
        assert lease_response(client, worker).status_code == 200
        resp = lease_response(client, worker)
        assert resp.status_code == 204
        assert resp.headers["X-Lease-Refused"] == "chunk_quota"

    def test_overloaded_cpu_stops_new_chunks(self, client: TestClient) -> None:
        start_job(client)
        worker = register(client)
        for _ in range(3):
            beat(client, worker, metrics={"cpu_cores": 2.0, "cpu_limit": 1.0})
        assert lease_response(client, worker).headers["X-Lease-Refused"] == "cpu_overload"

    def test_memory_overload_stops_new_chunks(self, client: TestClient) -> None:
        start_job(client)
        worker = register(client)
        beat(client, worker, metrics={"memory_bytes": 99, "memory_limit": 100})
        assert lease_response(client, worker).headers["X-Lease-Refused"] == "memory_overload"

    def test_many_recent_errors_stop_new_chunks(self, client: TestClient) -> None:
        start_job(client)
        worker = register(client)
        beat(client, worker, metrics={"load1": 1.0}, counters={"checked": 100, "failed": 10})
        beat(client, worker, metrics={"load1": 1.0}, counters={"checked": 200, "failed": 90})
        assert lease_response(client, worker).headers["X-Lease-Refused"] == "too_many_errors"

    def test_disabled_node_gets_nothing_but_keeps_current_chunk(self, client: TestClient) -> None:
        job_id = start_job(client)
        worker = register(client)
        chunk = must_lease(client, worker)
        client.patch(f"/api/workers/{worker}", json={"enabled": False})

        assert lease_response(client, worker).headers["X-Lease-Refused"] in (
            "disabled", "chunk_quota",
        )
        assert complete(client, worker, chunk).status_code == 200
        assert client.get(f"/api/jobs/{job_id}").json()["status"] == "completed"


@pytest.mark.integration
class TestThreads:
    def test_individual_value_beats_global_one(self, client: TestClient) -> None:
        register(client, "a", threads=2)
        register(client, "b", threads=2)
        worker_a = register(client, "a", threads=2)
        worker_b = register(client, "b", threads=2)
        start_job(client, threads=4, range="[1,100]", chunk_size=10,
                  workers=[{"name": "a", "threads": 8}, {"name": "b"}])
        assert must_lease(client, worker_a)["threads"] == 8
        assert must_lease(client, worker_b)["threads"] == 4

    def test_agent_value_is_used_without_settings(self, client: TestClient) -> None:
        start_job(client)
        assert must_lease(client, register(client, threads=3))["threads"] == 3

    def test_threads_change_reaches_agent_through_heartbeat(self, client: TestClient) -> None:
        worker = register(client, threads=4)
        assert beat(client, worker)["threads"] is None
        client.patch(f"/api/workers/{worker}", json={"desired_threads": 16})
        assert beat(client, worker)["threads"] == 16

    def test_job_threads_can_change_on_the_fly(self, client: TestClient) -> None:
        job_id = start_job(client)
        worker = register(client, threads=4)
        must_lease(client, worker)
        client.patch(f"/api/jobs/{job_id}", json={"threads": 12})
        assert beat(client, worker)["threads"] == 12


@pytest.mark.integration
class TestWorkerApi:
    def test_patch_unknown_worker_returns_404(self, client: TestClient) -> None:
        assert client.patch("/api/workers/999", json={"enabled": False}).status_code == 404

    def test_patch_keeps_other_fields(self, client: TestClient) -> None:
        worker = register(client)
        client.patch(f"/api/workers/{worker}", json={"desired_threads": 3, "rps_limit": 2.5})
        client.patch(f"/api/workers/{worker}", json={"enabled": False})
        body = card(client, worker)
        assert (body["desired_threads"], body["rps_limit"], body["enabled"]) == (3, 2.5, False)

    def test_patch_can_clear_setting(self, client: TestClient) -> None:
        worker = register(client)
        client.patch(f"/api/workers/{worker}", json={"desired_threads": 3})
        client.patch(f"/api/workers/{worker}", json={"desired_threads": None})
        assert card(client, worker)["desired_threads"] is None

    def test_ssh_fields_store_reference_not_key(self, client: TestClient) -> None:
        worker = register(client)
        client.patch(f"/api/workers/{worker}", json={
            "ssh_host": "192.168.10.11", "ssh_port": 22, "ssh_user": "downloader",
            "ssh_key_ref": "worker-01-key",
        })
        body = card(client, worker)
        assert body["ssh_host"] == "192.168.10.11"
        assert body["ssh_key_ref"] == "worker-01-key", "имя секрета, а не сам ключ"

    def test_metrics_series_is_chronological(self, client: TestClient) -> None:
        worker = register(client)
        for value in (1.0, 2.0, 3.0):
            beat(client, worker, metrics={"load1": value}, counters={"checked": 1})
        rows = client.get(f"/api/workers/{worker}/metrics").json()
        assert [r["load1"] for r in rows] == [1.0, 2.0, 3.0]
        assert rows[0]["counters"] == {"checked": 1}

    def test_metrics_of_unknown_worker_return_404(self, client: TestClient) -> None:
        assert client.get("/api/workers/999/metrics").status_code == 404


def rate_tick() -> None:
    run_dispatcher_tick()


@pytest.mark.integration
class TestRateLimit:
    def test_shares_are_given_by_threads_and_sum_to_limit(self, client: TestClient) -> None:
        strong = register(client, "strong", threads=16)
        weak = register(client, "weak", threads=4)
        rate_tick()
        strong_share = beat(client, strong)["rps_share"]
        weak_share = beat(client, weak)["rps_share"]
        assert strong_share == pytest.approx(16.0)
        assert weak_share == pytest.approx(4.0)
        assert strong_share + weak_share == pytest.approx(Settings().global_rps_limit)

    def test_job_limit_lowers_shared_pool(self, client: TestClient) -> None:
        start_job(client, global_rate_limit_rps=5)
        worker = register(client)
        rate_tick()
        body = client.get("/api/rate-limit").json()
        assert body["base_rps"] == 5
        assert body["shares"][str(worker)] == pytest.approx(5.0)
        assert must_lease(client, worker)["rps_share"] == pytest.approx(5.0)

    def test_429_lowers_total_and_absence_restores_it(self, client: TestClient) -> None:
        start_job(client, range="[1,10]")
        worker = register(client)
        rate_tick()
        assert client.get("/api/rate-limit").json()["effective_rps"] == 20.0

        chunk = must_lease(client, worker)
        client.post(f"/api/workers/{worker}/progress", json={
            "chunk_id": chunk["chunk_id"], "next_id": 2,
            "results": [{"external_id": 1, "status": "rate_limited", "http_status": 429}],
        })
        rate_tick()
        assert client.get("/api/rate-limit").json()["effective_rps"] == 10.0
        assert beat(client, worker)["rps_share"] == pytest.approx(10.0)

        rate_tick()
        assert client.get("/api/rate-limit").json()["effective_rps"] == 11.0

    def test_server_errors_also_lower_total(self, client: TestClient) -> None:
        start_job(client)
        worker = register(client)
        rate_tick()
        complete(client, worker, must_lease(client, worker), results=[
            {"external_id": 1, "status": "failed", "http_status": 503},
        ])
        rate_tick()
        assert client.get("/api/rate-limit").json()["effective_rps"] == 10.0

    def test_offline_worker_share_goes_to_others(self, client: TestClient) -> None:
        first = register(client, "first", threads=4)
        second = register(client, "second", threads=4)
        rate_tick()
        assert beat(client, first)["rps_share"] == pytest.approx(10.0)
        sql("UPDATE workers SET last_heartbeat_at = now() - interval '1 hour' WHERE id = :id",
            id=second)
        rate_tick()
        assert beat(client, first)["rps_share"] == pytest.approx(20.0)

    def test_new_worker_gets_fair_estimate_until_next_tick(self, client: TestClient) -> None:
        register(client, "first")
        rate_tick()
        late = register(client, "late")
        assert beat(client, late)["rps_share"] == pytest.approx(10.0), (
            "малая доля остановила бы агента: он применяет её на весь блок"
        )

    def test_without_redis_shares_are_equal_from_database(self, clean_db: None) -> None:
        from coordinator.api.app import create_app
        from tests.conftest import FLEET_TOKEN, login

        settings = Settings(redis_url="redis://127.0.0.1:1/0", health_timeout=0.2)
        with TestClient(create_app(settings)) as client:
            client.headers["X-Agent-Token"] = FLEET_TOKEN
            login(client)
            first = register(client, "first")
            register(client, "second")
            assert beat(client, first)["rps_share"] == pytest.approx(10.0)
            body = client.get("/api/rate-limit").json()
            assert body["redis_available"] is False

    def test_dispatcher_tick_survives_without_redis(self, client: TestClient) -> None:
        start_job(client)
        run_dispatcher_tick(redis_url="redis://127.0.0.1:1/0")
        assert lease(client, register(client)) is not None
