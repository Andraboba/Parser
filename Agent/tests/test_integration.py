# -*- coding: utf-8 -*-
"""Сквозная проверка: агент + заглушка координатора + мок-сайт."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent.config import parse_config
from agent.scheduler import AgentRuntime
from tests.support.mock_services import VIDEO_PAYLOAD, MockCoordinator, MockSite
from tests.support.tmpdirs import make_temp_dir


class IntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = make_temp_dir()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

        self.site = MockSite()
        self.site.start()
        self.addCleanup(self.site.stop)

        self.coordinator = MockCoordinator(
            chunk={
                "chunk_id": 1,
                "job_id": 1,
                "range_start": 100,
                "range_end": 105,
                "next_id": 100,
                "lease_ttl": 30,
                "rps_share": 50,
            }
        )
        self.coordinator.start()
        self.addCleanup(self.coordinator.stop)

        self.config = parse_config(
            {
                "agent": {
                    "name": "worker-test",
                    "threads": 4,
                    "storage_path": str(self.root / "videos"),
                    "state_dir": str(self.root / "state"),
                    "min_free_space_gb": 0,
                    "log_json": False,
                },
                "coordinator": {
                    "base_url": self.coordinator.url,
                    "token": "test-token",
                    "heartbeat_interval": 0.2,
                    "lease_ttl": 30,
                    "poll_interval": 0.1,
                    "request_timeout": 3,
                    "protocol_version": 1,
                },
                "limits": {"rps": 100, "check_concurrency": 4, "download_concurrency": 2},
                "retry": {"max_retries": 2, "base_delay": 0.01, "max_delay": 0.05, "jitter": 0.0},
                "site_adapter": {
                    "page_url_template": f"{self.site.url}/page/{{id}}",
                    "missing_markers": ["страница не найдена"],
                    "user_agent": "test-agent/1.0",
                    "request_timeout_seconds": 5,
                    "download": {"policy": "public_only", "max_file_size_bytes": 1024 * 1024},
                },
            }
        )

    def test_single_chunk_end_to_end(self) -> None:
        runtime = AgentRuntime(self.config)
        code = runtime.run(once=True)
        self.assertEqual(code, 0)

        self.assertTrue(self.coordinator.registered, "агент не зарегистрировался")
        self.assertEqual(len(self.coordinator.completed), 1)

        statuses = self.coordinator.statuses()
        self.assertEqual(statuses.get(100), "not_found")
        self.assertEqual(statuses.get(101), "forbidden")
        self.assertEqual(statuses.get(102), "rate_limited")
        self.assertEqual(statuses.get(103), "retry_wait")
        self.assertEqual(statuses.get(104), "not_found")
        self.assertEqual(statuses.get(105), "completed")

        completed = self.coordinator.completed[0]
        self.assertEqual(completed["next_id"], 106)
        self.assertEqual(completed["summary"]["counters"]["checked"], 6)

        video = runtime.storage.paths(105).any_video()
        self.assertIsNotNone(video, "файл не сохранён")
        assert video is not None
        self.assertEqual(video.read_bytes(), VIDEO_PAYLOAD)

        metadata_path = runtime.storage.paths(105).metadata
        self.assertTrue(metadata_path.exists())
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(metadata["title"], "Материал 105")
        self.assertEqual(metadata["status"], "completed")
        self.assertIn("sha256", metadata["file"])

    def test_no_work_left(self) -> None:
        self.coordinator.chunk = dict(self.coordinator.chunk)  # копия без изменений
        self.coordinator._lease_served = True  # сразу «работы нет»
        runtime = AgentRuntime(self.config)
        code = runtime.run(once=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.coordinator.completed, [])


if __name__ == "__main__":
    unittest.main()
