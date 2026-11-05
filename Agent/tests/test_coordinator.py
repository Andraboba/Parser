# -*- coding: utf-8 -*-
"""Клиент координатора: контракт, повторные попытки, outbox."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent.config import CoordinatorConfig, RetryConfig, parse_config
from agent.coordinator import CoordinatorClient
from agent.errors import CoordinatorError
from agent.http_client import HttpClient
from agent.journal import Outbox
from tests.support.mock_services import MockCoordinator
from tests.support.tmpdirs import make_temp_dir

FAST_RETRY = RetryConfig(max_retries=2, base_delay=0.01, max_delay=0.05, jitter=0.0)


class CoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = make_temp_dir()
        self.addCleanup(self.tmp.cleanup)
        self.http = HttpClient(timeout=2)
        self.addCleanup(self.http.close)

    def _client(self, base_url: str, token: str = "secret", outbox: Outbox | None = None) -> CoordinatorClient:
        config = CoordinatorConfig(
            base_url=base_url,
            token=token,
            request_timeout=2,
            heartbeat_interval=0.2,
            lease_ttl=30,
        )
        return CoordinatorClient(config, self.http, FAST_RETRY, outbox)

    def test_full_contract(self) -> None:
        with MockCoordinator(token="secret") as coordinator:
            client = self._client(coordinator.url)
            registered = client.register({"name": "worker-test"})
            self.assertEqual(registered.worker_id, "w-test")

            chunk = client.lease(registered.worker_id, free_slots=4)
            self.assertIsNotNone(chunk)
            assert chunk is not None
            self.assertEqual(chunk.range_start, 100)
            self.assertEqual(chunk.request_timeout_seconds, 20)
            self.assertEqual(chunk.max_retries, 2)
            self.assertFalse(chunk.expired())

            client.progress(
                registered.worker_id, chunk.chunk_id, 101, [{"external_id": 100, "status": "not_found"}], {"checked": 1}
            )
            self.assertTrue(client.complete(registered.worker_id, chunk.chunk_id, 101, [], {"counters": {}}))

            self.assertEqual(len(coordinator.registered), 1)
            self.assertEqual(len(coordinator.progress), 1)
            self.assertEqual(len(coordinator.completed), 1)
            self.assertEqual(coordinator.statuses(), {100: "not_found"})

    def test_retry_on_server_error(self) -> None:
        with MockCoordinator(token=None, fail_first_lease=1) as coordinator:
            client = self._client(coordinator.url, token="")
            registered = client.register({"name": "worker-test"})
            chunk = client.lease(registered.worker_id, free_slots=1)
            self.assertIsNotNone(chunk)
            self.assertEqual(coordinator.lease_calls, 2)

    def test_bad_token_is_not_retried(self) -> None:
        with MockCoordinator(token="secret") as coordinator:
            client = self._client(coordinator.url, token="wrong")
            with self.assertRaises(CoordinatorError) as context:
                client.register({"name": "worker-test"})
            self.assertEqual(context.exception.status, 401)
            self.assertFalse(context.exception.retryable)

    def test_failed_complete_goes_to_outbox(self) -> None:
        outbox = Outbox(Path(self.tmp.name))
        client = self._client("http://127.0.0.1:1", token="", outbox=outbox)
        delivered = client.complete("w-test", 7, 42, [], {"counters": {}})
        self.assertFalse(delivered)
        self.assertEqual(len(outbox), 1)

    def test_flush_outbox(self) -> None:
        outbox = Outbox(Path(self.tmp.name))
        outbox.add(
            "complete",
            {
                "worker_id": "w-test",
                "chunk_id": 3,
                "next_id": 9,
                "results": [{"external_id": 8, "status": "not_found"}],
                "summary": {},
            },
        )
        with MockCoordinator(token=None) as coordinator:
            client = self._client(coordinator.url, token="", outbox=outbox)
            delivered = client.flush_outbox()
        self.assertEqual(delivered, 1)
        self.assertEqual(len(outbox), 0)

    def test_register_without_worker_id_raises(self) -> None:
        client = self._client("http://127.0.0.1:1", token="")
        with self.assertRaises(CoordinatorError):
            client.register({"name": "worker-test"})


class ProtocolConfigTests(unittest.TestCase):
    def test_protocol_version_default(self) -> None:
        config = parse_config({})
        self.assertEqual(config.coordinator.protocol_version, 1)


if __name__ == "__main__":
    unittest.main()
