"""Главная панель, скорость и оставшееся время, предупреждения ТЗ п. 12, метрики."""
import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from coordinator.config import Settings
from coordinator.domain.alerts import (
    AlertLimits,
    JobSnapshot,
    WorkerSnapshot,
    job_alerts,
    version_alerts,
    worker_alerts,
)
from coordinator.domain.progress import (
    Rates,
    Sample,
    average_rate,
    current_rates,
    eta_seconds,
)
from coordinator.storage import db, monitoring
from tests.helpers import complete, must_lease, register, silence_worker, sql, start_job

T0 = datetime(2026, 1, 1, tzinfo=UTC)
LIMITS = AlertLimits()


def sample(seconds: float, ids: int, size: int = 0) -> Sample:
    return Sample(T0 + timedelta(seconds=seconds), ids, size)


def test_current_rate_uses_recent_window_only() -> None:
    samples = [sample(0, 0), sample(100, 1000), sample(110, 1100), sample(130, 1300, 600)]
    assert current_rates(samples, 30) == Rates(10.0, 20.0)


@pytest.mark.parametrize("samples", [[], [sample(0, 5)], [sample(0, 5), sample(0, 5)]])
def test_rate_of_insufficient_samples_is_zero(samples: list[Sample]) -> None:
    assert current_rates(samples, 30) == Rates(0.0, 0.0)


def test_rate_never_goes_negative() -> None:
    assert current_rates([sample(0, 100), sample(10, 50)], 30).ids_per_second == 0.0


def test_average_rate() -> None:
    assert average_rate(100, 50.0) == 2.0
    assert average_rate(100, 0.0) == 0.0


@pytest.mark.parametrize(("total", "done", "speed", "expected"), [
    (100, 50, 10.0, 5.0), (100, 100, 10.0, 0.0), (100, 150, 10.0, 0.0),
    (None, 50, 10.0, None), (100, 50, 0.0, None),
])
def test_eta(total: int | None, done: int, speed: float, expected: float | None) -> None:
    assert eta_seconds(total, done, speed) == expected


def node(**changes: Any) -> WorkerSnapshot:
    base: dict[str, Any] = {
        "id": 1, "name": "worker-01", "enabled": True, "online": True,
        "heartbeat_age_seconds": 1.0, "agent_version": "1.0", "protocol_version": 1,
    }
    return WorkerSnapshot(**{**base, **changes})


def codes(alerts: list[Any]) -> set[str]:
    return {a.code for a in alerts}


@pytest.mark.parametrize(("changes", "code"), [
    ({"online": False}, "worker_unavailable"),
    ({"container_state": "exited"}, "worker_unavailable"),
    ({"heartbeat_age_seconds": 100.0}, "heartbeat_missing"),
    ({"disk_free_bytes": 10, "min_free_bytes": 100}, "disk_low"),
    ({"cpu_cores_avg": 0.95, "cpu_limit": 1.0}, "cpu_high"),
    ({"memory_bytes": 95, "memory_limit": 100}, "memory_high"),
    ({"checked_recently": 100, "failed_recently": 40}, "errors_rising"),
    ({"rate_limited_recently": 10}, "rate_limited"),
    ({"oom_killed": True}, "oom_killed"),
    ({"restart_count": 3}, "container_restarts"),
])
def test_every_worker_alert_fires(changes: dict[str, Any], code: str) -> None:
    assert code in codes(worker_alerts(node(**changes), LIMITS, 11.0))


def test_healthy_worker_has_no_alerts() -> None:
    assert worker_alerts(node(), LIMITS, 11.0) == []


def test_disabled_worker_is_silent() -> None:
    assert worker_alerts(node(enabled=False, online=False), LIMITS, 11.0) == []


def test_few_checks_do_not_trigger_error_alert() -> None:
    assert not worker_alerts(node(checked_recently=5, failed_recently=5), LIMITS, 11.0)


def test_protocol_mismatch_with_coordinator() -> None:
    assert codes(version_alerts([node(protocol_version=2)], 1)) == {"version_mismatch"}


def test_minority_agent_version_is_flagged() -> None:
    alerts = version_alerts([
        node(id=1, agent_version="1.0"), node(id=2, agent_version="1.0"),
        node(id=3, agent_version="0.9"),
    ], 1)
    assert [a.worker_id for a in alerts] == [3]


def job_state(**changes: Any) -> JobSnapshot:
    base: dict[str, Any] = {
        "id": 1, "running_seconds": 600.0, "seconds_since_progress": 1.0,
        "current_ids_per_second": 100.0, "average_ids_per_second": 100.0,
        "has_active_workers": True,
    }
    return JobSnapshot(**{**base, **changes})


def test_stalled_job_alert() -> None:
    assert codes(job_alerts(job_state(seconds_since_progress=200.0), LIMITS)) == {"job_stalled"}


def test_stall_without_workers_is_reported_by_worker_alerts_instead() -> None:
    state = job_state(seconds_since_progress=200.0, has_active_workers=False,
                      current_ids_per_second=100.0)
    assert not job_alerts(state, LIMITS)


def test_speed_drop_alert() -> None:
    assert codes(job_alerts(job_state(current_ids_per_second=10.0), LIMITS)) == {"speed_dropped"}


def test_young_job_has_no_speed_alert() -> None:
    state = job_state(current_ids_per_second=1.0, running_seconds=10.0)
    assert not job_alerts(state, LIMITS)


def tick_samples() -> None:
    async def run() -> None:
        engine = db.make_engine(Settings().database_url)
        try:
            async with db.make_sessionmaker(engine)() as session:
                await monitoring.record_samples(session)
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.integration
class TestDashboard:
    def test_without_jobs(self, client: TestClient) -> None:
        body = client.get("/api/dashboard").json()
        assert body["job"] is None
        assert body["eta_seconds"] is None

    def test_shows_speed_eta_and_workers(self, client: TestClient) -> None:
        job_id = start_job(client, range="[1,100]")
        worker = register(client, threads=8)
        chunk = must_lease(client, worker)
        complete(client, worker, chunk, results=[
            {"external_id": 3, "status": "completed",
             "file": {"path": "/data/videos/x", "size": 500}},
        ])
        tick_samples()
        # Прогресс 10 идентификаторов и 500 байт за 10 секунд.
        sql("UPDATE job_samples SET at = now() - interval '10 seconds', processed_ids = 0, "
            "bytes_done = 0")
        tick_samples()

        body = client.get("/api/dashboard").json()
        assert body["job"]["id"] == job_id
        assert body["active_workers"] == 1
        assert body["active_threads"] == 8
        assert body["bytes_done"] == 500
        assert body["ids_per_second"] == pytest.approx(1.0, rel=0.1)
        assert body["bytes_per_second"] == pytest.approx(50.0, rel=0.1)
        assert body["eta_seconds"] == pytest.approx(90.0, rel=0.1)
        assert len(body["samples"]) == 2

    def test_open_range_has_no_eta(self, client: TestClient) -> None:
        start_job(client, range="[1,]")
        worker = register(client)
        complete(client, worker, must_lease(client, worker))
        tick_samples()
        sql("UPDATE job_samples SET at = now() - interval '10 seconds', processed_ids = 0")
        tick_samples()
        body = client.get("/api/dashboard").json()
        assert body["ids_per_second"] > 0
        assert body["eta_seconds"] is None

    def test_active_job_is_chosen_over_newer_finished_one(self, client: TestClient) -> None:
        active = start_job(client, range="[1,100]")
        worker = register(client)
        finished = start_job(client, range="[1,1]")
        sql("UPDATE jobs SET status = 'completed' WHERE id = :id", id=finished)
        assert client.get("/api/dashboard").json()["job"]["id"] == active
        assert worker

    def test_explicit_job_is_respected(self, client: TestClient) -> None:
        first = start_job(client)
        start_job(client)
        assert client.get(f"/api/dashboard?job_id={first}").json()["job"]["id"] == first

    def test_unknown_job_returns_404(self, client: TestClient) -> None:
        assert client.get("/api/dashboard?job_id=999").status_code == 404

    def test_finished_job_is_not_sampled(self, client: TestClient) -> None:
        job_id = start_job(client, range="[1,1]")
        worker = register(client)
        complete(client, worker, must_lease(client, worker))
        tick_samples()
        assert sql("SELECT count(*) FROM job_samples WHERE job_id = :id", id=job_id) == [(0,)]


@pytest.mark.integration
class TestAlertsApi:
    def alerts(self, client: TestClient) -> set[str]:
        return {a["code"] for a in client.get("/api/alerts").json()}

    def test_silent_worker_raises_alerts(self, client: TestClient) -> None:
        worker = register(client)
        assert self.alerts(client) == set()
        silence_worker(worker)
        assert "heartbeat_missing" in self.alerts(client)

    def test_low_disk_is_reported_with_worker(self, client: TestClient) -> None:
        worker = register(client)
        client.post(f"/api/workers/{worker}/heartbeat", json={
            "metrics": {"disk_free_bytes": 1000},
        })
        alert = client.get("/api/alerts").json()[0]
        assert alert["code"] == "disk_low"
        assert alert["worker_id"] == worker

    def test_errors_and_429_are_detected_from_counters(self, client: TestClient) -> None:
        worker = register(client)
        for checked, failed, limited in ((0, 0, 0), (100, 50, 20)):
            client.post(f"/api/workers/{worker}/heartbeat", json={
                "metrics": {"load1": 0.1},
                "counters": {"checked": checked, "failed": failed, "rate_limited": limited},
            })
        assert {"errors_rising", "rate_limited"} <= self.alerts(client)

    def test_stalled_job_is_detected(self, client: TestClient) -> None:
        start_job(client)
        register(client)
        tick_samples()
        sql("UPDATE job_samples SET at = now() - interval '5 minutes'")
        tick_samples()
        assert "job_stalled" in self.alerts(client)

    def test_viewer_rights_are_required(self, clean_db: None) -> None:
        from coordinator.api.app import create_app

        with TestClient(create_app(Settings())) as anonymous:
            assert anonymous.get("/api/alerts").status_code == 401
            assert anonymous.get("/api/dashboard").status_code == 401


@pytest.mark.integration
class TestPrometheus:
    def test_exposition_has_required_metrics(self, client: TestClient) -> None:
        start_job(client)
        worker = register(client)
        client.post(f"/api/workers/{worker}/heartbeat", json={
            "metrics": {"cpu_cores": 0.5, "cpu_limit": 1.0, "memory_bytes": 10,
                        "memory_limit": 100, "disk_free_bytes": 5 * 1024**3},
        })
        must_lease(client, worker)
        tick_samples()
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/plain")
        body = resp.text
        for line in (
            "coord_workers_online 1", 'coord_worker_up{worker="worker-01"} 1.0',
            'coord_worker_cpu_cores{worker="worker-01"} 0.5', 'coord_chunks{status="leased"} 1',
            "# TYPE coord_oldest_pending_seconds gauge", "coord_rate_limit_base_rps",
        ):
            assert line in body, line

    def test_alert_appears_as_metric(self, client: TestClient) -> None:
        worker = register(client)
        client.post(f"/api/workers/{worker}/heartbeat", json={
            "metrics": {"disk_free_bytes": 1},
        })
        assert 'coord_alerts{code="disk_low"' in client.get("/metrics").text

    def test_label_values_are_escaped(self, client: TestClient) -> None:
        client.post("/api/workers/register", json={
            "name": 'we"ird', "agent_version": "1", "protocol_version": 1, "threads": 1,
        })
        assert r'worker="we\"ird"' in client.get("/metrics").text

    def test_metrics_are_not_in_public_schema(self, client: TestClient) -> None:
        assert "/metrics" not in client.get("/openapi.json").json()["paths"]
