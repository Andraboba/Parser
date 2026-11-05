# -*- coding: utf-8 -*-
"""Загрузка файла: потоковая запись, продолжение по Range, контрольная сумма.

Файл пишется во временный ``video.part``. После успешной проверки размера
и sha256 он атомарно переименовывается в ``video.{ext}`` (ТЗ, п. 8).
Обрыв соединения не уничтожает прогресс: при следующем запуске загрузка
продолжается с места обрыва, если сервер поддерживает Range.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import DownloadError, InsufficientSpace, SizeMismatch, TooLargeFile
from .http_client import HttpClient
from .logging_setup import get_logger
from .storage import Storage

__all__ = ["Downloader", "DownloadResult"]

_MIME_EXTENSIONS = {
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/x-matroska": ".mkv",
    "video/quicktime": ".mov",
    "video/x-msvideo": ".avi",
    "application/octet-stream": ".bin",
}

_log = get_logger("downloader")


@dataclass(frozen=True)
class DownloadResult:
    """Итог загрузки."""

    path: Path
    size: int
    sha256: str
    resumed: bool
    skipped: bool
    mime: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "size": self.size,
            "sha256": self.sha256,
            "resumed": self.resumed,
            "skipped": self.skipped,
            "mime": self.mime,
        }


def guess_extension(mime: str | None, url: str) -> str:
    """Расширение по MIME-типу, иначе по адресу."""
    if mime:
        base = mime.split(";")[0].strip().lower()
        if base in _MIME_EXTENSIONS:
            return _MIME_EXTENSIONS[base]
    path = url.split("?", 1)[0]
    suffix = Path(path).suffix.lower()
    if 1 < len(suffix) <= 6 and suffix[1:].isalnum():
        return suffix
    return ".bin"


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


class Downloader:
    """Скачивание одного файла в каталог материала."""

    def __init__(
        self,
        storage: Storage,
        http: HttpClient,
        max_bytes: int,
        chunk_size: int = 256 * 1024,
        timeout: float = 60.0,
    ) -> None:
        self.storage = storage
        self.http = http
        self.max_bytes = int(max_bytes)
        self.chunk_size = max(16 * 1024, int(chunk_size))
        self.timeout = timeout

    def download(
        self,
        url: str,
        item_id: int,
        expected_size: int | None = None,
        mime: str | None = None,
    ) -> DownloadResult:
        finished = self.storage.existing_video(item_id)
        if finished is not None:
            digest, size = _hash_file(finished)
            return DownloadResult(
                path=finished,
                size=size,
                sha256=digest,
                resumed=False,
                skipped=True,
                mime=mime,
            )
        return self._download_once(url, item_id, expected_size, mime, retry_on_range_error=True)

    # ------------------------------------------------------------------
    def _download_once(
        self,
        url: str,
        item_id: int,
        expected_size: int | None,
        mime: str | None,
        retry_on_range_error: bool,
    ) -> DownloadResult:
        paths = self.storage.ensure(item_id)
        partial = paths.partial
        existing = partial.stat().st_size if partial.exists() else 0

        if not self.storage.has_space(expected_size or 0):
            raise InsufficientSpace(
                f"недостаточно места: свободно {self.storage.free_bytes()} байт, "
                f"резерв {self.storage.min_free_bytes}"
            )

        headers = {"Accept": "*/*"}
        if existing > 0:
            headers["Range"] = f"bytes={existing}-"

        response = self.http.request("GET", url, headers=headers, timeout=self.timeout)
        try:
            status = response.status
            if status == 416 and existing > 0 and retry_on_range_error:
                response.close()
                _log.info(
                    "частичный файл отвергнут сервером, начинаем заново",
                    extra={"item_id": item_id, "partial_size": existing},
                )
                try:
                    partial.unlink()
                except OSError:
                    pass
                return self._download_once(url, item_id, expected_size, mime, False)

            if status in (401, 403):
                raise DownloadError("доступ к файлу запрещён", error_class="forbidden")
            if status == 404:
                raise DownloadError("файл не найден", error_class="not_found")
            if status == 429:
                raise DownloadError("сервер ограничил загрузку", error_class="rate_limited")
            if status >= 500:
                raise DownloadError(f"ошибка сервера {status}", error_class="server_error")
            if status not in (200, 206):
                raise DownloadError(f"неожидаемый статус {status}", error_class="http_error")

            mime = mime or response.header("content-type")
            resumed = status == 206 and existing > 0
            if status == 200:
                existing = 0  # сервер отдаёт файл целиком, начинаем с нуля

            digest = hashlib.sha256()
            if resumed and existing > 0:
                self._feed_existing_hash(partial, digest)
            else:
                try:
                    partial.unlink()
                except OSError:
                    pass

            expected_total = expected_size
            if expected_total is None and status == 206:
                content_range = response.header("content-range") or ""
                total = content_range.rsplit("/", 1)[-1].strip()
                if total.isdigit():
                    expected_total = int(total)
            if expected_total is not None and expected_total > self.max_bytes:
                raise TooLargeFile(
                    f"файл больше допустимого размера: {expected_total} > {self.max_bytes}"
                )

            written = existing
            mode = "ab" if resumed else "wb"
            with open(partial, mode) as handle:
                for chunk in response.iter_chunks(self.chunk_size):
                    written += len(chunk)
                    if written > self.max_bytes:
                        raise TooLargeFile(
                            f"файл больше допустимого размера: {written} > {self.max_bytes}"
                        )
                    if written % (16 * 1024 * 1024) < self.chunk_size:
                        if not self.storage.has_space():
                            raise InsufficientSpace("на диске заканчивается место")
                    handle.write(chunk)
                    digest.update(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            response.close()

        if expected_total is not None and written != expected_total:
            try:
                partial.unlink()
            except OSError:
                pass
            raise SizeMismatch(f"размер не совпал: получено {written}, ожидалось {expected_total}")

        extension = guess_extension(mime, url)
        final_path = paths.video(extension)
        os.replace(partial, final_path)
        self._fsync_directory(final_path.parent)

        return DownloadResult(
            path=final_path,
            size=written,
            sha256=digest.hexdigest(),
            resumed=resumed,
            skipped=False,
            mime=mime,
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _feed_existing_hash(path: Path, digest: "hashlib._Hash") -> None:
        with open(path, "rb") as handle:
            while True:
                block = handle.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except (OSError, AttributeError):
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)
