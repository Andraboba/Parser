# -*- coding: utf-8 -*-
"""Адаптер целевого сайта — единственное место знания о нём.

Адаптер получает идентификатор, формирует адрес страницы, выполняет запрос,
классифицирует ответ и извлекает разрешённые метаданные. Всё, что зависит
от разметки, вынесено в конфигурацию (``site_adapter``): шаблон адреса,
маркеры наличия и отсутствия материала, разрешённые хосты редиректов.

Классификация повторяет таблицу из плана: 200 — found или not_found,
404 — not_found, 401/403 — forbidden без попыток обхода, 429 — rate_limited,
5xx и сетевые ошибки — повторяемая ошибка.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

from .config import SiteConfig
from .errors import NetworkError
from .http_client import HttpClient
from .logging_setup import get_logger

__all__ = [
    "CheckResult",
    "SiteAdapter",
    "STATUS_FOUND",
    "STATUS_NOT_FOUND",
    "STATUS_FORBIDDEN",
    "STATUS_RATE_LIMITED",
    "STATUS_RETRY",
    "STATUS_FAILED",
    "normalize_title",
]

STATUS_FOUND = "found"
STATUS_NOT_FOUND = "not_found"
STATUS_FORBIDDEN = "forbidden"
STATUS_RATE_LIMITED = "rate_limited"
STATUS_RETRY = "retry_wait"
STATUS_FAILED = "failed"

_log = get_logger("adapter")

_VIDEO_URL_RE = re.compile(
    r"""https?://[^\s"'<>\\]+?\.(?:mp4|m3u8|webm|mov|mkv)(?:\?[^\s"'<>\\]*)?""",
    re.IGNORECASE,
)
_JSON_VIDEO_FIELD_RE = re.compile(
    r"""["'](?:contentUrl|content_url|videoUrl|video_url|videoSrc|src|file|hls|mp4)["']\s*:\s*["']([^"']+)["']""",
    re.IGNORECASE,
)
_TITLE_PATTERNS = (
    re.compile(r"""<meta[^>]+property=["']og:title["'][^>]+content=["']([^"']+)["']""", re.IGNORECASE),
    re.compile(r"""<meta[^>]+name=["']twitter:title["'][^>]+content=["']([^"']+)["']""", re.IGNORECASE),
    re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL),
)
_SCRIPT_JSON_RE = re.compile(
    r"""<script[^>]*type=["']application/(?:ld\+)?json["'][^>]*>(.*?)</script>""",
    re.IGNORECASE | re.DOTALL,
)
_SIZE_RE = re.compile(r"""["'](?:size|fileSize|file_size|contentLength)["']\s*:\s*(\d{3,})""")
# Ссылка на каталог прямых файлов в playerOptions (Kinescope).
_DOWNLOAD_LINK_RE = re.compile(r'"downloadLink"\s*:\s*"([^"]+)"')
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WHITESPACE_RE = re.compile(r"\s+")

_VIDEO_MIME_BY_SUFFIX = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".m3u8": "application/vnd.apple.mpegurl",
}


def normalize_title(raw: str | None, limit: int = 300) -> str | None:
    """Привести название к безопасному виду (ТЗ, п. 8).

    Название никогда не используется как путь: только как метаданные.
    """
    if raw is None:
        return None
    text = _CONTROL_CHARS_RE.sub(" ", raw)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    if not text:
        return None
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


@dataclass(frozen=True)
class CheckResult:
    """Нормализованный результат проверки одного идентификатора."""

    item_id: int
    status: str
    page_url: str
    http_status: int | None = None
    error_class: str | None = None
    retry_after: float | None = None
    title: str | None = None
    download_url: str | None = None
    size: int | None = None
    mime: str | None = None
    adapter_version: str = "1"

    @property
    def found(self) -> bool:
        return self.status == STATUS_FOUND

    def as_metadata(self) -> dict[str, Any]:
        return {
            "external_id": self.item_id,
            "title": self.title,
            "page_url": self.page_url,
            "download_url": self.download_url,
            "size": self.size,
            "mime_type": self.mime,
            "status": self.status,
            "http_status": self.http_status,
            "adapter_version": self.adapter_version,
            "discovered_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        }


class SiteAdapter:
    """Проверка идентификатора и извлечение метаданных."""

    version = "1"

    def __init__(self, config: SiteConfig, http: HttpClient) -> None:
        self.config = config
        self.http = http

    # ------------------------------------------------------------------
    def check(self, item_id: int, timeout: float | None = None) -> CheckResult:
        url = self.config.url_for(item_id)
        headers = {
            "User-Agent": self.config.user_agent,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "ru,en;q=0.8",
        }
        headers.update(self.config.headers)

        try:
            status, response_headers, body = self.http.get(
                url,
                headers=headers,
                timeout=timeout if timeout is not None else self.config.request_timeout_seconds,
                follow_redirects=self.config.follow_redirects,
                allowed_hosts=self.config.allowed_redirect_hosts,
            )
        except NetworkError as exc:
            _log.warning(
                "сетевая ошибка при проверке",
                extra={"item_id": item_id, "url": url, "error": str(exc)},
            )
            return CheckResult(
                item_id=item_id,
                status=STATUS_RETRY,
                page_url=url,
                error_class="network",
                adapter_version=self.version,
            )

        if status == 200:
            return self._parse(item_id, url, status, response_headers, body)
        if status in (404, 410):
            return CheckResult(item_id, STATUS_NOT_FOUND, url, status, adapter_version=self.version)
        if status in (401, 403):
            return CheckResult(item_id, STATUS_FORBIDDEN, url, status, adapter_version=self.version)
        if status == 429:
            return CheckResult(
                item_id,
                STATUS_RATE_LIMITED,
                url,
                status,
                retry_after=_parse_retry_after(response_headers.get("retry-after")),
                adapter_version=self.version,
            )
        if status in (408, 425) or status >= 500:
            return CheckResult(
                item_id,
                STATUS_RETRY,
                url,
                status,
                error_class="server_error",
                retry_after=_parse_retry_after(response_headers.get("retry-after")),
                adapter_version=self.version,
            )
        return CheckResult(
            item_id, STATUS_FAILED, url, status, error_class=f"http_{status}",
            adapter_version=self.version,
        )

    # ------------------------------------------------------------------
    def _parse(
        self,
        item_id: int,
        url: str,
        status: int,
        headers: dict[str, str],
        body: bytes,
    ) -> CheckResult:
        try:
            html = body.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - декодирование не должно ронять проверку
            html = body.decode("latin-1", errors="replace")

        lowered = html.lower()
        title = self._extract_title(html)
        candidate_urls = self._extract_video_urls(html, url)
        size = self._extract_size(html)

        marked_found = any(marker.lower() in lowered for marker in self.config.found_markers if marker)
        marked_missing = any(marker.lower() in lowered for marker in self.config.missing_markers if marker)

        prefer = self.config.download.prefer
        direct: tuple[str, str] | None = None
        direct_checked = False
        if prefer == "direct":
            direct_checked = True
            direct = self._direct_asset(html, url)
            if direct is not None:
                return self._direct_result(item_id, url, status, title, direct)

        if candidate_urls:
            download_url = candidate_urls[0]
            mime = headers.get("content-type", "").split(";")[0].strip() or None
            if mime and not mime.startswith("video/"):
                mime = _suffix_mime(download_url) or mime
            return CheckResult(
                item_id=item_id,
                status=STATUS_FOUND,
                page_url=url,
                http_status=status,
                title=title,
                download_url=download_url,
                size=size,
                mime=mime or _suffix_mime(download_url),
                adapter_version=self.version,
            )

        if direct is None and not direct_checked:
            # HLS-ссылки на странице нет: пробуем прямой каталог файлов.
            direct = self._direct_asset(html, url)
            if direct is not None:
                return self._direct_result(item_id, url, status, title, direct)

        if marked_found and not marked_missing:
            # материал есть, но ссылка на файл не найдена — скачивать нечего
            return CheckResult(
                item_id=item_id,
                status=STATUS_FOUND,
                page_url=url,
                http_status=status,
                title=title,
                adapter_version=self.version,
            )

        return CheckResult(
            item_id=item_id,
            status=STATUS_NOT_FOUND,
            page_url=url,
            http_status=status,
            title=title,
            error_class="no_material" if not marked_missing else None,
            adapter_version=self.version,
        )

    # ------------------------------------------------------------------
    def _direct_result(
        self,
        item_id: int,
        url: str,
        status: int,
        title: str | None,
        direct: tuple[str, str],
    ) -> CheckResult:
        """Результат по прямой ссылке: размер узнает загрузчик из Content-Length.

        В каталоге Kinescope поле size описывает только видео-дорожку, а файл
        отдаётся вместе со звуком и весит больше, поэтому размер не фиксируем.
        """
        download_url, mime = direct
        return CheckResult(
            item_id=item_id,
            status=STATUS_FOUND,
            page_url=url,
            http_status=status,
            title=title,
            download_url=download_url,
            size=None,
            mime=mime,
            adapter_version=self.version,
        )

    def _direct_asset(self, html: str, page_url: str) -> tuple[str, str] | None:
        """Взять лучший mp4 из download-links.json (там есть звук)."""
        match = _DOWNLOAD_LINK_RE.search(html)
        if match is None:
            return None
        link = urljoin(page_url, _unescape(match.group(1)))
        try:
            status, _headers, body = self.http.get(
                link,
                headers={"Accept": "application/json"},
                timeout=self.config.request_timeout_seconds,
                follow_redirects=self.config.follow_redirects,
                allowed_hosts=self.config.allowed_redirect_hosts,
            )
        except NetworkError:
            return None
        if status != 200:
            return None
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        assets = data.get("assets") if isinstance(data, dict) else None
        if not isinstance(assets, list):
            return None

        limit = self.config.download.max_file_size_bytes
        candidates: list[tuple[int, int, str]] = []
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            asset_url = asset.get("url")
            if not isinstance(asset_url, str) or not asset_url.startswith(("http://", "https://")):
                continue
            name = str(asset.get("download_filename") or "").lower()
            if not name.endswith(".mp4"):
                continue
            try:
                asset_size = int(asset.get("size") or 0)
            except (TypeError, ValueError):
                asset_size = 0
            candidates.append(
                (_quality_rank(str(asset.get("quality") or "")), asset_size, asset_url)
            )
        candidates.sort(key=lambda item: item[0], reverse=True)

        # Поле size в каталоге Kinescope описывает только видео-дорожку,
        # поэтому фактический размер проверяем по Content-Length.
        for _rank, asset_size, asset_url in candidates:
            actual = self._asset_size(asset_url)
            if actual is not None:
                if actual > limit:
                    continue
                return asset_url, "video/mp4"
            if asset_size and asset_size > limit:
                continue
            return asset_url, "video/mp4"
        return None

    def _asset_size(self, url: str) -> int | None:
        """Фактический размер файла из Content-Length без скачивания тела."""
        try:
            response = self.http.request(
                "GET", url, headers={"Range": "bytes=0-0"},
                timeout=self.config.request_timeout_seconds,
            )
        except NetworkError:
            return None
        try:
            if response.status not in (200, 206):
                return None
            if response.status == 206:
                # Сервер честно ответил на Range: полный размер в Content-Range.
                total = (response.header("content-range") or "").rsplit("/", 1)[-1].strip()
                if total.isdigit():
                    return int(total)
            length = response.content_length()
            return length if length and length > 0 else None
        finally:
            response.close()

    # ------------------------------------------------------------------
    def _extract_title(self, html: str) -> str | None:
        for pattern in _TITLE_PATTERNS:
            match = pattern.search(html)
            if match:
                title = normalize_title(_unescape(match.group(1)))
                if title:
                    return title
        for blob in _SCRIPT_JSON_RE.findall(html):
            data = _loads(blob)
            if isinstance(data, dict):
                for key in ("name", "title", "headline"):
                    value = data.get(key)
                    if isinstance(value, str):
                        title = normalize_title(value)
                        if title:
                            return title
        return None

    def _extract_video_urls(self, html: str, page_url: str) -> list[str]:
        candidates: list[str] = []
        for match in _VIDEO_URL_RE.findall(html):
            candidates.append(match)
        for match in _JSON_VIDEO_FIELD_RE.findall(html):
            if match.lower().startswith(("http://", "https://", "/")):
                candidates.append(match)
        if self.config.player_config_path:
            candidates.extend(self._extract_by_path(html))

        resolved: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            absolute = urljoin(page_url, _unescape(candidate))
            if urlsplit(absolute).scheme not in ("http", "https"):
                continue
            if absolute in seen:
                continue
            seen.add(absolute)
            resolved.append(absolute)

        # mp4 предпочтительнее плейлиста: его можно скачать целиком
        resolved.sort(key=lambda value: (0 if value.lower().endswith(".mp4") else 1, len(value)))
        return resolved

    def _extract_by_path(self, html: str) -> list[str]:
        path = self.config.player_config_path or ""
        for blob in _SCRIPT_JSON_RE.findall(html):
            data = _loads(blob)
            node: Any = data
            for part in path.split("."):
                if isinstance(node, dict):
                    node = node.get(part)
                else:
                    node = None
                    break
            if isinstance(node, str) and node.startswith(("http://", "https://", "/")):
                return [node]
        return []

    def _extract_size(self, html: str) -> int | None:
        match = _SIZE_RE.search(html)
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                return None
        return None


# --------------------------------------------------------------------------
def _quality_rank(value: str) -> int:
    """«1080p» -> 1080, «original» -> максимум; незнакомое — 0."""
    text = value.strip().lower()
    if text in ("original", "source"):
        return 100000
    match = re.search(r"(\d{3,4})", text)
    return int(match.group(1)) if match else 0


def _suffix_mime(url: str) -> str | None:
    path = urlsplit(url).path.lower()
    for suffix, mime in _VIDEO_MIME_BY_SUFFIX.items():
        if path.endswith(suffix):
            return mime
    return None


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        moment = parsedate_to_datetime(value)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return max(0.0, (moment - datetime.now(tz=timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


def _loads(blob: str) -> Any:
    try:
        return json.loads(blob)
    except (json.JSONDecodeError, TypeError):
        return None


def _unescape(value: str) -> str:
    return (
        value.replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .strip()
    )
