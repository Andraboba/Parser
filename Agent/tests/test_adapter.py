# -*- coding: utf-8 -*-
"""Адаптер сайта: классификация ответов и извлечение метаданных."""

from __future__ import annotations

import tempfile
import unittest

from agent.adapter import (
    STATUS_FORBIDDEN,
    STATUS_FOUND,
    STATUS_NOT_FOUND,
    STATUS_RATE_LIMITED,
    STATUS_RETRY,
    SiteAdapter,
    normalize_title,
)
from agent.config import parse_config
from agent.http_client import HttpClient
from tests.support.mock_services import MockSite
from tests.support.tmpdirs import make_temp_dir

MISSING_MARKERS = ["страница не найдена"]


class AdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.site = MockSite()
        cls.site.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.site.stop()

    def setUp(self) -> None:
        self.tmp = make_temp_dir()
        self.addCleanup(self.tmp.cleanup)
        config = parse_config(
            {
                "agent": {"storage_path": self.tmp.name},
                "coordinator": {"heartbeat_interval": 1, "lease_ttl": 10},
                "site_adapter": {
                    "page_url_template": f"{self.site.url}/page/{{id}}",
                    "missing_markers": MISSING_MARKERS,
                    "user_agent": "test-agent/1.0",
                    "request_timeout_seconds": 5,
                },
            }
        )
        self.config = config
        self.http = HttpClient(timeout=5)
        self.addCleanup(self.http.close)
        self.adapter = SiteAdapter(config.site, self.http)

    # -- классификация ---------------------------------------------------
    def test_found_with_metadata(self) -> None:
        result = self.adapter.check(105)
        self.assertEqual(result.status, STATUS_FOUND)
        self.assertEqual(result.http_status, 200)
        self.assertEqual(result.title, "Материал 105")
        self.assertTrue(result.download_url.endswith("/video/105.mp4"), result.download_url)
        self.assertEqual(result.size, len(self.site.video))
        self.assertEqual(result.mime, "video/mp4")

    def test_not_found_404(self) -> None:
        result = self.adapter.check(100)
        self.assertEqual(result.status, STATUS_NOT_FOUND)
        self.assertEqual(result.http_status, 404)

    def test_forbidden(self) -> None:
        result = self.adapter.check(101)
        self.assertEqual(result.status, STATUS_FORBIDDEN)
        self.assertIsNone(result.download_url)

    def test_rate_limited(self) -> None:
        result = self.adapter.check(102)
        self.assertEqual(result.status, STATUS_RATE_LIMITED)
        self.assertEqual(result.retry_after, 1.0)

    def test_server_error_is_retryable(self) -> None:
        result = self.adapter.check(103)
        self.assertEqual(result.status, STATUS_RETRY)
        self.assertEqual(result.error_class, "server_error")

    def test_page_without_material(self) -> None:
        result = self.adapter.check(104)
        self.assertEqual(result.status, STATUS_NOT_FOUND)

    def test_metadata_shape(self) -> None:
        metadata = self.adapter.check(105).as_metadata()
        for key in ("external_id", "title", "page_url", "download_url", "discovered_at"):
            self.assertIn(key, metadata)
        self.assertEqual(metadata["external_id"], 105)


class TitleTests(unittest.TestCase):
    def test_control_characters_removed(self) -> None:
        self.assertEqual(normalize_title("Видео\x00\x07  тест "), "Видео тест")

    def test_long_title_truncated(self) -> None:
        title = normalize_title("а" * 500)
        self.assertIsNotNone(title)
        assert title is not None
        self.assertLessEqual(len(title), 300)

    def test_empty_title(self) -> None:
        self.assertIsNone(normalize_title("   \x00 "))


if __name__ == "__main__":
    unittest.main()
