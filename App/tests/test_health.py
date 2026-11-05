import pytest
from fastapi.testclient import TestClient

from coordinator.api.app import create_app
from coordinator.config import Settings


@pytest.mark.integration
def test_health_ok_when_dependencies_up() -> None:
    with TestClient(create_app(Settings())) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "checks": {"db": "ok", "redis": "ok"}}


def test_health_degraded_when_dependencies_down() -> None:
    settings = Settings(
        database_url="postgresql+psycopg://x:x@127.0.0.1:1/x",
        redis_url="redis://127.0.0.1:1/0",
        health_timeout=1.0,
    )
    with TestClient(create_app(settings)) as client:
        resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["db"].startswith("error")
    assert body["checks"]["redis"].startswith("error")
