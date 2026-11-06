# -*- coding: utf-8 -*-
"""Прямой mp4 из download-links: выбор качества и проверка реального размера."""

from __future__ import annotations

import json
import unittest

from agent.adapter import SiteAdapter
from agent.config import DownloadPolicy, SiteConfig


class FakeResponse:
    """Минимальный ответ: только Content-Length и закрытие."""

    status = 200

    def __init__(self, size: int) -> None:
        self._size = size

    def content_length(self) -> int:
        return self._size

    def close(self) -> None:
        pass


class FakeHttp:
    """Подменяет HTTP-клиент: JSON каталога и размеры файлов."""

    def __init__(self, payload: dict, sizes: dict[str, int] | None = None) -> None:
        self.payload = payload
        self.sizes = sizes or {}
        self.calls: list[str] = []

    def get(self, url, headers=None, timeout=None, follow_redirects=False, allowed_hosts=()):
        self.calls.append(url)
        return 200, {}, json.dumps(self.payload).encode("utf-8")

    def request(self, method, url, headers=None, timeout=None):
        self.calls.append(url)
        return FakeResponse(self.sizes.get(url, 0))


def make_adapter(
    payload: dict, sizes: dict[str, int] | None = None, limit: int = 100 * 1024 * 1024
) -> SiteAdapter:
    site = SiteConfig(
        page_url_template="https://kinescope.io/{id}",
        download=DownloadPolicy(max_file_size_bytes=limit, prefer="direct"),
    )
    return SiteAdapter(site, FakeHttp(payload, sizes))


PAGE = (
    '<script>const playerOptions = {"playlist":[{"id":"x","title":"T",'
    '"downloadLink":"/download-links.json"}]};</script>'
)

ASSETS = {
    "assets": [
        {"quality": "1080p", "size": 179_000_000, "url": "https://cdn/1080.mp4",
         "download_filename": "a-1080p.mp4"},
        {"quality": "720p", "size": 117_000_000, "url": "https://cdn/720.mp4",
         "download_filename": "a-720p.mp4"},
        {"quality": "480p", "size": 61_000_000, "url": "https://cdn/480.mp4",
         "download_filename": "a-480p.mp4"},
        {"quality": "360p", "size": 38_000_000, "url": "https://cdn/360.mp4",
         "download_filename": "a-360p.mp4"},
    ]
}

REAL_SIZES = {
    "https://cdn/1080.mp4": 179_000_000,
    "https://cdn/720.mp4": 122_000_000,
    "https://cdn/480.mp4": 122_000_000,
    "https://cdn/360.mp4": 42_742_197,
}


class DirectAssetTests(unittest.TestCase):
    def test_chooses_best_fitting_mp4(self) -> None:
        sizes = {
            "https://cdn/1080.mp4": 179_000_000,
            "https://cdn/720.mp4": 117_000_000,
            "https://cdn/480.mp4": 61_000_000,
            "https://cdn/360.mp4": 38_000_000,
        }
        adapter = make_adapter(ASSETS, sizes)
        direct = adapter._direct_asset(PAGE, "https://kinescope.io/1")
        self.assertEqual(direct, ("https://cdn/480.mp4", "video/mp4"))

    def test_uses_real_content_length(self) -> None:
        """Каталог занижает размер: 480p на деле 122 МБ, поэтому берём 360p."""
        adapter = make_adapter(ASSETS, REAL_SIZES)
        direct = adapter._direct_asset(PAGE, "https://kinescope.io/1")
        assert direct is not None
        self.assertEqual(direct[0], "https://cdn/360.mp4")

    def test_skips_non_mp4(self) -> None:
        adapter = make_adapter(
            {
                "assets": [
                    {"quality": "original", "size": 10, "url": "https://cdn/a.pdf",
                     "download_filename": "a.pdf"},
                    {"quality": "360p", "size": 10, "url": "https://cdn/a.mp4",
                     "download_filename": "a.mp4"},
                ]
            }
        )
        direct = adapter._direct_asset(PAGE, "https://kinescope.io/1")
        self.assertEqual(direct, ("https://cdn/a.mp4", "video/mp4"))

    def test_without_download_link(self) -> None:
        adapter = make_adapter({"assets": []})
        self.assertIsNone(adapter._direct_asset("<html></html>", "https://kinescope.io/1"))

    def test_parse_prefers_direct(self) -> None:
        adapter = make_adapter(ASSETS, REAL_SIZES)
        result = adapter._parse(
            7, "https://kinescope.io/7", 200, {"content-type": "text/html"}, PAGE.encode()
        )
        self.assertEqual(result.status, "found")
        self.assertEqual(result.download_url, "https://cdn/360.mp4")
        self.assertEqual(result.mime, "video/mp4")
        self.assertIsNone(result.size)


if __name__ == "__main__":
    unittest.main()
