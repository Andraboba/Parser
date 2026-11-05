# -*- coding: utf-8 -*-
"""Указания координатора в ответе heartbeat."""

from __future__ import annotations

import unittest
from pathlib import Path

from agent.config import parse_config
from agent.scheduler import AgentRuntime

from tests.support.tmpdirs import make_temp_dir


class InstructionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = make_temp_dir()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.runtime = AgentRuntime(
            parse_config(
                {
                    "agent": {
                        "name": "worker-test",
                        "threads": 8,
                        "storage_path": str(root / "videos"),
                        "state_dir": str(root / "state"),
                        "min_free_space_gb": 0,
                    },
                    "coordinator": {"base_url": "http://127.0.0.1:9", "token": "t"},
                    "limits": {"rps": 50, "check_concurrency": 8},
                }
            )
        )

    def test_rps_share_changes_rate(self) -> None:
        self.runtime._apply_instructions({"rps_share": 7.5})
        self.assertEqual(self.runtime.limiter.bucket.rate, 7.5)

    def test_small_share_has_floor(self) -> None:
        self.runtime._apply_instructions({"rps_share": 0.01})
        self.assertEqual(self.runtime.limiter.bucket.rate, 0.1)

    def test_absent_share_keeps_rate(self) -> None:
        before = self.runtime.limiter.bucket.rate
        self.runtime._apply_instructions({"ok": True, "rps_share": None, "threads": None})
        self.assertEqual(self.runtime.limiter.bucket.rate, before)

    def test_threads_limited_by_local_setting(self) -> None:
        self.runtime._apply_instructions({"threads": 4})
        self.assertEqual(self.runtime.concurrency, 4)
        self.runtime._apply_instructions({"threads": 100})
        self.assertEqual(self.runtime.concurrency, 8)

    def test_cancelled_chunks_are_remembered(self) -> None:
        self.runtime._apply_instructions({"cancel_chunks": [5, 6]})
        self.assertEqual(self.runtime._cancelled_chunks, {5, 6})


if __name__ == "__main__":
    unittest.main()
