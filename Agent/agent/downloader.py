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
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import DownloadError, InsufficientSpace, NetworkError, SizeMismatch, TooLargeFile
from .hls import HlsError, MediaPlaylist, Segment, Variant, parse_master, parse_media
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


def _range_start(value: str | None) -> int | None:
    """Начало диапазона из `Content-Range: bytes 100-199/200`."""
    if not value:
        return None
    head = value.strip().split(None, 1)[-1]
    start = head.split("-", 1)[0].strip()
    return int(start) if start.isdigit() else None


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
        progress: Callable[[int], None] | None = None,
    ) -> DownloadResult:
        finished = self.storage.existing_video(item_id)
        if finished is not None:
            digest, size = _hash_file(finished)
            if progress is not None:
                progress(size)
            return DownloadResult(
                path=finished,
                size=size,
                sha256=digest,
                resumed=False,
                skipped=True,
                mime=mime,
            )
        path = url.split("?", 1)[0].lower()
        if path.endswith(".m3u8") or (mime or "").lower().startswith(
            "application/vnd.apple.mpegurl"
        ):
            return self._download_hls(url, item_id, progress)
        return self._download_once(
            url, item_id, expected_size, mime, retry_on_range_error=True, progress=progress,
        )

    # ------------------------------------------------------------------
    # HLS (m3u8): master -> вариант -> init-сегмент -> сегменты
    # ------------------------------------------------------------------
    def _download_hls(
        self,
        url: str,
        item_id: int,
        progress: Callable[[int], None] | None = None,
    ) -> DownloadResult:
        """Собрать HLS-поток в один файл (fMP4 или MPEG-TS).

        Аудиодорожка HLS у Kinescope идёт отдельным плейлистом и здесь не
        склеивается: файл получается без звука. Для материалов, где разрешена
        прямая ссылка, адаптер отдаёт mp4, и работает обычный путь.
        """
        if not self.storage.has_space():
            raise InsufficientSpace("на диске недостаточно места для HLS-загрузки")
        try:
            text, base = self._fetch_playlist(url)
            variants = parse_master(text, base)
            if variants:
                playlist, variant = self._choose_variant(variants)
            else:
                playlist = parse_media(text, base)
                variant = None
        except HlsError as exc:
            raise DownloadError(str(exc), error_class="hls_error") from exc

        expected = playlist.total_bytes
        if expected is not None and expected > self.max_bytes:
            raise TooLargeFile(
                f"HLS-поток больше допустимого размера: {expected} > {self.max_bytes}"
            )
        _log.info(
            "HLS: выбран поток",
            extra={
                "item_id": item_id,
                "quality": variant.quality if variant is not None else "",
                "segments": len(playlist.segments),
                "expected_bytes": expected,
                "fragmented_mp4": playlist.is_fragmented_mp4,
            },
        )

        paths = self.storage.ensure(item_id)
        partial = paths.partial
        try:
            partial.unlink()
        except OSError:
            pass

        digest = hashlib.sha256()
        written = 0
        with open(partial, "wb") as handle:
            if playlist.init is not None:
                written, digest = self._write_segment(
                    handle, playlist.init, digest, written, progress
                )
            for segment in playlist.segments:
                written, digest = self._write_segment(
                    handle, segment, digest, written, progress
                )
            handle.flush()
            os.fsync(handle.fileno())

        if written <= 0:
            raise DownloadError("HLS-поток не содержит данных", error_class="hls_error")
        if playlist.is_fragmented_mp4:
            extension, mime = ".mp4", "video/mp4"
        else:
            extension, mime = ".ts", "video/mp2t"
        final_path = paths.video(extension)
        os.replace(partial, final_path)
        self._fsync_directory(final_path.parent)
        return DownloadResult(
            path=final_path,
            size=written,
            sha256=digest.hexdigest(),
            resumed=False,
            skipped=False,
            mime=mime,
        )

    def _fetch_playlist(self, url: str) -> tuple[str, str]:
        """Прочитать m3u8 и вернуть текст и адрес для разрешения относительных ссылок."""
        status, _headers, body = self.http.get(
            url,
            headers={"Accept": "application/vnd.apple.mpegurl, application/x-mpegURL, */*"},
            timeout=self.timeout,
        )
        if status in (401, 403):
            raise DownloadError("доступ к HLS-плейлисту запрещён", error_class="forbidden")
        if status == 404:
            raise DownloadError("HLS-плейлист не найден", error_class="not_found")
        if status == 429:
            raise DownloadError("сервер ограничил загрузку", error_class="rate_limited")
        if status >= 500:
            raise DownloadError(f"ошибка сервера {status}", error_class="server_error")
        if status != 200:
            raise DownloadError(f"неожидаемый статус {status}", error_class="http_error")
        text = body.decode("utf-8", "replace")
        if "#EXTM3U" not in text:
            raise DownloadError("ответ не является HLS-плейлистом", error_class="hls_error")
        return text, url

    def _choose_variant(
        self, variants: list[Variant]
    ) -> tuple[MediaPlaylist, Variant]:
        """Выбрать максимальное качество, которое укладывается в лимит размера."""
        candidates: list[tuple[int, Variant, MediaPlaylist]] = []
        for variant in variants:
            try:
                text, base = self._fetch_playlist(variant.uri)
                media = parse_media(text, base)
            except (DownloadError, HlsError, NetworkError):
                continue
            size = media.total_bytes
            if size is None:
                bitrate = variant.bandwidth or 0
                size = int(media.duration * bitrate / 8) if bitrate else 0
            candidates.append((size, variant, media))
        if not candidates:
            raise DownloadError("не удалось прочитать HLS-плейлист", error_class="hls_error")
        fitting = [candidate for candidate in candidates if 0 < candidate[0] <= self.max_bytes]
        if fitting:
            chosen = max(fitting, key=lambda c: (c[1].height, c[1].bandwidth, c[0]))
        else:
            chosen = min(candidates, key=lambda c: c[0] or self.max_bytes + 1)
        return chosen[2], chosen[1]

    def _write_segment(
        self,
        handle: Any,
        segment: Segment,
        digest: Any,
        written: int,
        progress: Callable[[int], None] | None,
    ) -> tuple[int, Any]:
        """Скачать один сегмент, при сбое откатить запись и повторить.

        Возвращает новое число записанных байт и объект хеша: при повторе
        хеш восстанавливается из копии, поэтому вызывающий код обязан
        пользоваться возвращённым значением.
        """
        start_position = handle.tell()
        start_digest = digest.copy()
        headers = {"Accept": "*/*"}
        if segment.byte_range:
            headers["Range"] = segment.byte_range

        for attempt in range(3):
            try:
                response = self.http.request(
                    "GET", segment.uri, headers=headers, timeout=self.timeout
                )
                try:
                    status = response.status
                    if status in (401, 403):
                        raise DownloadError("доступ к сегменту запрещён", error_class="forbidden")
                    if status == 404:
                        raise DownloadError("сегмент не найден", error_class="not_found")
                    if status == 429:
                        raise DownloadError("сервер ограничил загрузку", error_class="rate_limited")
                    if status >= 500:
                        raise DownloadError(f"ошибка сервера {status}", error_class="server_error")
                    if segment.length is not None:
                        if status != 206:
                            raise DownloadError(
                                "сервер не отдал байтовый диапазон", error_class="range_unsupported"
                            )
                        start = _range_start(response.header("content-range"))
                        if start != segment.offset:
                            raise DownloadError(
                                "сервер вернул неверный диапазон", error_class="range_mismatch"
                            )
                    elif status not in (200, 206):
                        raise DownloadError(f"неожидаемый статус {status}", error_class="http_error")

                    declared = response.content_length()
                    if segment.length is not None and declared not in (None, segment.length):
                        raise DownloadError(
                            "неверная длина сегмента", error_class="size_mismatch"
                        )
                    segment_written = 0
                    for chunk in response.iter_chunks(self.chunk_size):
                        segment_written += len(chunk)
                        written += len(chunk)
                        if written > self.max_bytes:
                            raise TooLargeFile(
                                f"HLS-поток больше допустимого размера: {written} > {self.max_bytes}"
                            )
                        if written % (16 * 1024 * 1024) < self.chunk_size:
                            if not self.storage.has_space():
                                raise InsufficientSpace("на диске заканчивается место")
                        handle.write(chunk)
                        digest.update(chunk)
                        if progress is not None:
                            progress(written)
                    if segment.length is not None and segment_written != segment.length:
                        raise DownloadError(
                            "сегмент скачался не полностью", error_class="size_mismatch"
                        )
                    return written, digest
                finally:
                    response.close()
            except (TooLargeFile, InsufficientSpace):
                raise
            except (DownloadError, NetworkError, OSError) as exc:
                error_class = getattr(exc, "error_class", "download_error")
                retryable = error_class not in (
                    "forbidden", "not_found", "range_unsupported", "range_mismatch", "size_mismatch",
                )
                if not retryable or attempt == 2:
                    raise
                handle.seek(start_position)
                handle.truncate()
                digest = start_digest.copy()
                written = start_position
                time.sleep(min(2 ** attempt, 5))
        raise DownloadError("сегмент не скачался", error_class="hls_error")

    # ------------------------------------------------------------------
    def _download_once(
        self,
        url: str,
        item_id: int,
        expected_size: int | None,
        mime: str | None,
        retry_on_range_error: bool,
        progress: Callable[[int], None] | None = None,
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
                return self._download_once(
                    url, item_id, expected_size, mime, False, progress=progress,
                )

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
            if status == 206 and existing > 0:
                # Продолжаем только с того места, которое просили. Иначе сервер
                # отдаёт другой диапазон, и склейка дала бы битый файл.
                start = _range_start(response.header("content-range"))
                if start != existing:
                    response.close()
                    if retry_on_range_error:
                        try:
                            partial.unlink()
                        except OSError:
                            pass
                        return self._download_once(
                            url, item_id, expected_size, mime, False, progress=progress,
                        )
                    raise DownloadError(
                        "сервер вернул неверный диапазон Range", error_class="range_mismatch",
                    )
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
                    if progress is not None:
                        progress(written)
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
