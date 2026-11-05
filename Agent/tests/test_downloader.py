# -*- coding: utf-8 -*-
"""Загрузчик: полная загрузка, продолжение по Range, проверки размера и места."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from agent.downloader import Downloader, guess_extension
from agent.errors import InsufficientSpace, SizeMismatch
from agent.http_client import HttpClient
from agent.storage import Storage
from tests.support.mock_services import VIDEO_PAYLOAD, MockSite
from tests.support.tmpdirs import make_temp_dir


class DownloaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.site = MockSite()
        cls.site.start()
        cls.site_no_range = MockSite(support_range=False)
        cls.site_no_range.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.site.stop()
        cls.site_no_range.stop()

    def setUp(self) -> None:
        self.tmp = make_temp_dir()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.http = HttpClient(timeout=5)
        self.addCleanup(self.http.close)
        self.storage = Storage(self.root / "videos")
        self.root.joinpath("videos").mkdir(parents=True, exist_ok=True)
        self.downloader = Downloader(self.storage, self.http, max_bytes=1024 * 1024)

    def test_download_writes_file_and_hash(self) -> None:
        result = self.downloader.download(f"{self.site.url}/video/105.mp4", 105)
        self.assertFalse(result.skipped)
        self.assertTrue(result.path.exists())
        self.assertEqual(result.size, len(VIDEO_PAYLOAD))
        self.assertEqual(result.sha256, hashlib.sha256(VIDEO_PAYLOAD).hexdigest())
        self.assertEqual(result.path.suffix, ".mp4")
        self.assertEqual(result.path, self.storage.paths(105).video(".mp4"))

    def test_second_call_is_idempotent(self) -> None:
        url = f"{self.site.url}/video/106.mp4"
        first = self.downloader.download(url, 106)
        second = self.downloader.download(url, 106)
        self.assertTrue(second.skipped)
        self.assertEqual(first.sha256, second.sha256)

    def test_resume_by_range(self) -> None:
        paths = self.storage.ensure(107)
        paths.partial.write_bytes(VIDEO_PAYLOAD[:1000])
        result = self.downloader.download(f"{self.site.url}/video/107.mp4", 107)
        self.assertTrue(result.resumed)
        self.assertEqual(result.path.read_bytes(), VIDEO_PAYLOAD)

    def test_restart_when_server_ignores_range(self) -> None:
        paths = self.storage.ensure(108)
        paths.partial.write_bytes(VIDEO_PAYLOAD[:1000])
        result = self.downloader.download(f"{self.site_no_range.url}/video/108.mp4", 108)
        self.assertFalse(result.resumed)
        self.assertEqual(result.path.read_bytes(), VIDEO_PAYLOAD)

    def test_size_mismatch_removes_partial(self) -> None:
        paths = self.storage.ensure(109)
        with self.assertRaises(SizeMismatch):
            self.downloader.download(f"{self.site.url}/video/109.mp4", 109, expected_size=10)
        self.assertFalse(paths.partial.exists())

    def test_insufficient_space(self) -> None:
        storage = Storage(self.root / "small", min_free_bytes=10 ** 15)
        self.root.joinpath("small").mkdir(parents=True, exist_ok=True)
        downloader = Downloader(storage, self.http, max_bytes=1024 * 1024)
        with self.assertRaises(InsufficientSpace):
            downloader.download(f"{self.site.url}/video/110.mp4", 110)

    def test_extension_guess(self) -> None:
        self.assertEqual(guess_extension("video/webm", "http://x/y"), ".webm")
        self.assertEqual(guess_extension(None, "http://x/y/video.mp4?x=1"), ".mp4")
        self.assertEqual(guess_extension("application/octet-stream", "http://x/y"), ".bin")


if __name__ == "__main__":
    unittest.main()
