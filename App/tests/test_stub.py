from pathlib import Path

import httpx
import pytest

from agent_stub import StubAgent, cgroup_limits, results_for
from tests.conftest import FLEET_TOKEN, login


def test_results_mark_every_seventh_id_as_found() -> None:
    results = results_for(5, 8)
    assert results == [
        {"external_id": 5, "status": "not_found"},
        {"external_id": 6, "status": "not_found"},
        {"external_id": 7, "status": "found"},
        {"external_id": 8, "status": "not_found"},
    ]


def test_cgroup_limits_read_quota_and_memory(tmp_path: Path) -> None:
    (tmp_path / "cpu.max").write_text("50000 100000\n")
    (tmp_path / "memory.max").write_text(f"{256 * 1024**2}\n")
    assert cgroup_limits(tmp_path) == {"cpus": 0.5, "memory_mb": 256}


def test_cgroup_limits_without_limit_are_none(tmp_path: Path) -> None:
    (tmp_path / "cpu.max").write_text("max 100000\n")
    (tmp_path / "memory.max").write_text("max\n")
    assert cgroup_limits(tmp_path) == {"cpus": None, "memory_mb": None}


def test_cgroup_limits_outside_container_are_none(tmp_path: Path) -> None:
    assert cgroup_limits(tmp_path) == {"cpus": None, "memory_mb": None}


@pytest.mark.integration
def test_stub_finishes_job_against_real_api(live_url: str) -> None:
    with httpx.Client(base_url=live_url) as http:
        login(http)
        job = http.post("/api/jobs", json={"range_from": 1, "range_to": 25, "chunk_size": 10})
        job_id = job.json()["id"]
        http.post(f"/api/jobs/{job_id}/start")

        agent = StubAgent(live_url, "stub-1", threads=2, token=FLEET_TOKEN)
        agent.register()
        steps = 0
        while agent.step():
            steps += 1

        body = http.get(f"/api/jobs/{job_id}").json()
    assert steps == 3
    assert body["status"] == "completed"
    assert body["processed_ids"] == 25
    assert (body["items"]["found"], body["items"]["not_found"]) == (3, 22)
