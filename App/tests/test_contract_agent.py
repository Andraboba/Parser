"""Настоящий клиент и настоящий цикл агента из каталога Agent/ против настоящего API."""
import importlib.util
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.conftest import FLEET_TOKEN, login

agent_coordinator = pytest.importorskip("agent.coordinator", reason="нет каталога Agent/")
agent_config = pytest.importorskip("agent.config")
agent_http = pytest.importorskip("agent.http_client")
agent_scheduler = pytest.importorskip("agent.scheduler")

pytestmark = pytest.mark.integration

REGISTER = {
    "name": "real-agent", "agent_version": "1.0.0", "protocol_version": 1, "threads": 4,
    "storage_path": "/data/videos", "container_id": None, "image": None,
    "limits": {"cpus": None, "memory_mb": None},
}


def make_client(url: str, token: str, http: Any) -> Any:
    config = agent_config.CoordinatorConfig(base_url=url, token=token)
    return agent_coordinator.CoordinatorClient(
        config, http, agent_config.RetryConfig(max_retries=0),
    )


@pytest.fixture
def agent_http_client() -> Iterator[Any]:
    http = agent_http.HttpClient(timeout=5.0)
    yield http
    http.close()


@pytest.fixture
def api(live_url: str) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=live_url) as http:
        login(http)
        yield http


def running_job(api: httpx.Client, text: str) -> int:
    job_id = api.post("/api/jobs", json={"range": text, "chunk_size": 100}).json()["id"]
    api.post(f"/api/jobs/{job_id}/start")
    return job_id


def test_register_lease_progress_complete(
    live_url: str, agent_http_client: Any, api: httpx.Client,
) -> None:
    real = make_client(live_url, FLEET_TOKEN, agent_http_client)
    job_id = running_job(api, "[1,3]")
    registered = real.register(REGISTER)
    chunk = real.lease(registered.worker_id, free_slots=4)
    assert chunk is not None
    assert (chunk.range_start, chunk.range_end, chunk.next_id) == (1, 3, 1)
    assert chunk.rps_share is not None

    answer = real.heartbeat(registered.worker_id, {
        "status": "busy", "free_slots": 0, "active_chunks": [chunk.chunk_id],
        "metrics": {"cpu_cores": 0.1, "cpu_limit": None, "memory_bytes": 1, "memory_limit": None,
                    "disk_total_bytes": 10, "disk_free_bytes": 5, "disk_used_bytes": 5,
                    "load1": 0.5, "net_rx_bps": 1, "net_tx_bps": 2, "uptime_seconds": 3.5},
        "counters": {"checked": 0, "bytes_downloaded": 0},
    })
    assert answer["ok"] is True
    assert answer["cancel_chunks"] == []

    real.progress(
        registered.worker_id, chunk.chunk_id, 2,
        [{"external_id": 1, "status": "not_found", "http_status": 404, "error_class": None,
          "title": None, "page_url": "http://site/1", "download_url": None, "file": None}],
        {"checked": 1},
    )
    delivered = real.complete(
        registered.worker_id, chunk.chunk_id, 4,
        [
            {"external_id": 2, "status": "failed", "http_status": 500,
             "error_class": "server_error"},
            {"external_id": 3, "status": "rate_limited", "http_status": 429},
        ],
        {"counters": {"checked": 3}, "expired": False},
    )
    assert delivered is True
    assert real.lease(registered.worker_id, free_slots=4) is None

    body = api.get(f"/api/jobs/{job_id}").json()
    assert body["status"] == "completed"
    assert body["items"]["failed"] == 1
    assert body["items"]["rate_limited"] == 1
    assert body["items"]["not_found"] == 1


def test_fail_returns_chunk(live_url: str, agent_http_client: Any, api: httpx.Client) -> None:
    real = make_client(live_url, FLEET_TOKEN, agent_http_client)
    running_job(api, "[1,3]")
    registered = real.register(REGISTER)
    chunk = real.lease(registered.worker_id, free_slots=4)
    assert real.fail(registered.worker_id, chunk.chunk_id, "network", "обрыв") is True


def test_real_client_receives_token_error(live_url: str, agent_http_client: Any) -> None:
    bad = make_client(live_url, "wrong", agent_http_client)
    with pytest.raises(agent_coordinator.CoordinatorError) as caught:
        bad.register({"name": "x", "agent_version": "1", "protocol_version": 1, "threads": 1})
    assert caught.value.status == 401


def _load_mock_site() -> Any:
    root = Path(agent_coordinator.__file__).parent.parent
    spec = importlib.util.spec_from_file_location(
        "agent_mock_services", root / "tests" / "support" / "mock_services.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.MockSite


def test_real_agent_processes_chunk_and_saves_files(
    live_url: str, api: httpx.Client, tmp_path: Path,
) -> None:
    """Раздел 16 плана: настоящий агент подключается к платформе без её изменений."""
    job_id = running_job(api, "[100,115]")
    site = _load_mock_site()()
    site.start()
    try:
        config = agent_config.parse_config({
            "agent": {
                "name": "real-agent", "threads": 4, "storage_path": str(tmp_path / "videos"),
                "state_dir": str(tmp_path / "state"), "min_free_space_gb": 0,
                "log_level": "WARNING", "log_json": False,
            },
            "coordinator": {
                "base_url": live_url, "token": FLEET_TOKEN, "heartbeat_interval": 1,
                "lease_ttl": 60, "poll_interval": 0.2,
            },
            "limits": {"rps": 200, "check_concurrency": 4, "download_concurrency": 2},
            "retry": {"max_retries": 1, "base_delay": 0.05, "max_delay": 0.2, "jitter": 0.0},
            "site_adapter": {
                "page_url_template": f"{site.url}/page/{{id}}",
                "missing_markers": ["страница не найдена"],
                "request_timeout_seconds": 5,
                "download": {"policy": "public_only", "max_file_size_bytes": 1024 * 1024},
            },
        })
        assert agent_scheduler.AgentRuntime(config).run(once=True) == 0
    finally:
        site.stop()

    body = api.get(f"/api/jobs/{job_id}").json()
    assert body["status"] == "completed"
    assert body["processed_ids"] == 16
    assert sum(body["items"].values()) == 16

    items = api.get(f"/api/jobs/{job_id}/items").json()["items"]
    with_files = [i for i in items if i["file_path"]]
    assert with_files, "настоящий агент сообщил о загруженных файлах"
    for item in with_files:
        assert item["status"] == "completed"
        assert item["checksum"]
        assert Path(item["file_path"]).is_file()
        assert Path(item["file_path"]).is_relative_to(tmp_path / "videos")
