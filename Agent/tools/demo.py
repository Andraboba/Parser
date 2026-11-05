# -*- coding: utf-8 -*-
"""Демонстрация агента без реального сайта.

Поднимает локальный «сайт» и заглушку координатора, прогоняет один блок
идентификаторов и печатает результат: статусы, файлы, контрольные суммы.

    python tools/demo.py
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.config import AgentConfig, parse_config  # noqa: E402
from agent.scheduler import AgentRuntime  # noqa: E402
from tests.support.mock_services import (  # noqa: E402
    VIDEO_PAYLOAD,
    MockCoordinator,
    MockSite,
)

DEMO_DIR = ROOT / "data" / "demo"


def build_config(site_url: str, coordinator_url: str) -> AgentConfig:
    return parse_config(
        {
            "agent": {
                "name": "worker-demo",
                "threads": 4,
                "storage_path": str(DEMO_DIR / "videos"),
                "state_dir": str(DEMO_DIR / "state"),
                "min_free_space_gb": 0,
                "log_level": "WARNING",
                "log_json": False,
            },
            "coordinator": {
                "base_url": coordinator_url,
                "token": "demo-token",
                "heartbeat_interval": 1,
                "lease_ttl": 60,
                "poll_interval": 0.5,
            },
            "limits": {"rps": 100, "check_concurrency": 4, "download_concurrency": 2},
            "retry": {"max_retries": 2, "base_delay": 0.05, "max_delay": 0.2, "jitter": 0.0},
            "site_adapter": {
                "page_url_template": f"{site_url}/page/{{id}}",
                "missing_markers": ["страница не найдена"],
                "user_agent": "demo-agent/1.0",
                "request_timeout_seconds": 5,
                "download": {"policy": "public_only", "max_file_size_bytes": 1024 * 1024},
            },
        }
    )


def main() -> int:
    if DEMO_DIR.exists():
        shutil.rmtree(DEMO_DIR, ignore_errors=True)
    DEMO_DIR.mkdir(parents=True, exist_ok=True)

    site = MockSite()
    site.start()
    coordinator = MockCoordinator(
        chunk={
            "chunk_id": 1,
            "job_id": 1,
            "range_start": 100,
            "range_end": 115,
            "next_id": 100,
            "lease_ttl": 60,
            "rps_share": 100,
        }
    )
    coordinator.start()

    print(f"сайт:         {site.url}")
    print(f"координатор:  {coordinator.url}")
    print(f"каталог:      {DEMO_DIR}")
    print()

    runtime = AgentRuntime(build_config(site.url, coordinator.url))
    started = time.time()
    try:
        exit_code = runtime.run(once=True)
    finally:
        site.stop()
        coordinator.stop()
    elapsed = time.time() - started

    if exit_code != 0:
        print(f"агент завершился с кодом {exit_code}")
        return exit_code

    statuses = coordinator.statuses()
    completed = coordinator.completed[0] if coordinator.completed else {}
    print(f"обработано за {elapsed:.1f} с, next_id = {completed.get('next_id')}")
    print()
    print(f"{'id':>5}  {'статус':<14} название")
    print("-" * 60)
    for item_id in sorted(statuses):
        file_info = next(
            (
                result.get("file")
                for result in coordinator.results
                if str(result.get("external_id")) == str(item_id)
            ),
            None,
        )
        title = ""
        for result in coordinator.results:
            if str(result.get("external_id")) == str(item_id) and result.get("title"):
                title = str(result["title"])
                break
        marker = ""
        if file_info:
            marker = f"  [{file_info['size']} байт, {Path(file_info['path']).name}]"
        print(f"{item_id:>5}  {statuses[item_id]:<14} {title}{marker}")

    print()
    videos = sorted((DEMO_DIR / "videos").rglob("video.*"))
    print(f"файлов на диске: {len(videos)}")
    for path in videos:
        print(f"  {path.relative_to(DEMO_DIR)}  {path.stat().st_size} байт")
    if videos:
        expected = len(VIDEO_PAYLOAD)
        actual = videos[0].stat().st_size
        print(f"размер совпадает с источником: {actual == expected}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
