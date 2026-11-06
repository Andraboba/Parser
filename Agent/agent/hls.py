# -*- coding: utf-8 -*-
"""Разбор HLS-плейлистов (m3u8) без внешних зависимостей.

Поддерживается подмножество, которого достаточно для публичных плееров:
master-плейлист с вариантами качества, media-плейлист с отдельным init-сегментом
(``EXT-X-MAP``), байтовыми диапазонами (``EXT-X-BYTERANGE``) и относительными
адресами. Шифрованные плейлисты (``EXT-X-KEY``) явно отвергаются: ключи и
расшифровка не входят в задачу агента.

Модуль ничего не скачивает: только разбирает текст, чтобы загрузчик мог
принять решение и посчитать ожидаемый размер.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin

__all__ = [
    "HlsError",
    "Variant",
    "Segment",
    "MediaPlaylist",
    "parse_master",
    "parse_media",
    "is_master",
]

_ATTRIBUTE_RE = re.compile(r'([A-Za-z0-9-]+)=("[^"]*"|[^,]*)')
_RESOLUTION_RE = re.compile(r"(\d+)\s*x\s*(\d+)")


class HlsError(ValueError):
    """Плейлист не соответствует поддерживаемому подмножеству HLS."""


@dataclass(frozen=True)
class Variant:
    """Вариант качества из master-плейлиста."""

    uri: str
    bandwidth: int = 0
    width: int = 0
    height: int = 0

    @property
    def quality(self) -> str:
        if self.height:
            return f"{self.height}p"
        return f"{self.bandwidth // 1000}k" if self.bandwidth else ""


@dataclass(frozen=True)
class Segment:
    """Сегмент или init-сегмент: адрес и необязательный диапазон байт."""

    uri: str
    offset: int | None = None
    length: int | None = None
    duration: float = 0.0

    @property
    def byte_range(self) -> str | None:
        if self.offset is None or self.length is None:
            return None
        return f"bytes={self.offset}-{self.offset + self.length - 1}"


@dataclass(frozen=True)
class MediaPlaylist:
    """Media-плейлист: init-сегмент (если есть) и список сегментов."""

    segments: tuple[Segment, ...]
    init: Segment | None = None
    duration: float = 0.0

    @property
    def total_bytes(self) -> int | None:
        """Точный размер, если у всех сегментов задан диапазон байт."""
        lengths = [segment.length for segment in self.segments]
        if self.init is not None:
            lengths.append(self.init.length)
        if any(length is None for length in lengths):
            return None
        return sum(length for length in lengths if length is not None)  # type: ignore[arg-type]

    @property
    def is_fragmented_mp4(self) -> bool:
        return self.init is not None


def _attributes(text: str) -> dict[str, str]:
    """Разобрать список ``KEY=VALUE``, где значение может быть в кавычках."""
    attributes: dict[str, str] = {}
    for match in _ATTRIBUTE_RE.finditer(text):
        value = match.group(2)
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
        attributes[match.group(1).upper()] = value
    return attributes


def _byterange(value: str | None, last_end: int) -> tuple[int | None, int | None]:
    """Разобрать ``<length>[@<offset>]``; без offset продолжить с last_end."""
    if not value:
        return None, None
    text = value.strip().strip('"')
    if not text:
        return None, None
    if "@" in text:
        length_text, offset_text = text.split("@", 1)
    else:
        length_text, offset_text = text, str(last_end)
    try:
        length = int(length_text)
        offset = int(offset_text)
    except ValueError as exc:
        raise HlsError(f"некорректный байтовый диапазон: {value!r}") from exc
    if length <= 0 or offset < 0:
        raise HlsError(f"некорректный байтовый диапазон: {value!r}")
    return length, offset


def is_master(text: str) -> bool:
    """Отличить master-плейлист от media-плейлиста."""
    return "#EXT-X-STREAM-INF" in text


def parse_master(text: str, base_url: str) -> list[Variant]:
    """Варианты качества master-плейлиста. Пустой список — это не master."""
    variants: list[Variant] = []
    pending: tuple[int, int, int] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-STREAM-INF:"):
            attributes = _attributes(line.split(":", 1)[1])
            try:
                bandwidth = int(attributes.get("BANDWIDTH", "0") or 0)
            except ValueError:
                bandwidth = 0
            width = height = 0
            match = _RESOLUTION_RE.search(attributes.get("RESOLUTION", ""))
            if match:
                width, height = int(match.group(1)), int(match.group(2))
            pending = (bandwidth, width, height)
        elif not line.startswith("#") and pending is not None:
            bandwidth, width, height = pending
            variants.append(Variant(urljoin(base_url, line), bandwidth, width, height))
            pending = None
    return variants


def parse_media(text: str, base_url: str) -> MediaPlaylist:
    """Разобрать media-плейлист в список сегментов."""
    segments: list[Segment] = []
    init: Segment | None = None
    duration = 0.0
    last_end = 0
    pending_range: tuple[int | None, int | None] | None = None
    pending_duration = 0.0
    encrypted = False

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-KEY:"):
            method = _attributes(line.split(":", 1)[1]).get("METHOD", "").upper()
            if method not in ("", "NONE"):
                encrypted = True
        elif line.startswith("#EXT-X-MAP:"):
            attributes = _attributes(line.split(":", 1)[1])
            uri = attributes.get("URI", "")
            if not uri:
                raise HlsError("EXT-X-MAP без URI")
            length, offset = _byterange(attributes.get("BYTERANGE"), 0)
            init = Segment(urljoin(base_url, uri), offset, length)
        elif line.startswith("#EXT-X-BYTERANGE:"):
            pending_range = _byterange(line.split(":", 1)[1], last_end)
        elif line.startswith("#EXTINF:"):
            value = line.split(":", 1)[1].split(",", 1)[0].strip()
            try:
                pending_duration = float(value)
            except ValueError:
                pending_duration = 0.0
        elif not line.startswith("#"):
            offset = length = None
            if pending_range is not None:
                length, offset = pending_range
            segments.append(
                Segment(urljoin(base_url, line), offset, length, pending_duration)
            )
            if length is not None and offset is not None:
                last_end = offset + length
            pending_range = None
            pending_duration = 0.0
            duration += segments[-1].duration

    if encrypted:
        raise HlsError("HLS-плейлист зашифрован (EXT-X-KEY): расшифровка не поддерживается")
    if not segments:
        raise HlsError("в плейлисте нет сегментов")
    return MediaPlaylist(tuple(segments), init, duration)
