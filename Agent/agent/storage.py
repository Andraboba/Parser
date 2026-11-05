# -*- coding: utf-8 -*-
"""Раскладка файлов на диске.

Схема из ТЗ, п. 8::

    {storage_path}/{id-prefix}/{id}/
        metadata.json
        video.{extension}

Временный файл — ``video.part``; после проверки размера и контрольной суммы
он атомарно переименовывается. Имя каталога — только идентификатор:
название материала может содержать что угодно и как путь не используется.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["Storage", "ItemPaths"]

_PREFIX_DIVISOR = 1_000_000


@dataclass(frozen=True)
class ItemPaths:
    """Пути одного материала."""

    directory: Path

    @property
    def metadata(self) -> Path:
        return self.directory / "metadata.json"

    @property
    def partial(self) -> Path:
        return self.directory / "video.part"

    def video(self, extension: str) -> Path:
        return self.directory / f"video{extension}"

    def any_video(self) -> Path | None:
        for candidate in sorted(self.directory.glob("video.*")):
            if candidate.name == "video.part":
                continue
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
        return None


class Storage:
    """Каталог хранения материалов и служебные операции над ним."""

    def __init__(self, root: Path, min_free_bytes: int = 0) -> None:
        self.root = Path(root)
        self.min_free_bytes = max(0, int(min_free_bytes))

    # -- пути -----------------------------------------------------------
    @staticmethod
    def prefix_for(item_id: int) -> str:
        """Префикс каталога: 200673499 -> 200 (как в примере ТЗ)."""
        return str(int(item_id) // _PREFIX_DIVISOR)

    def paths(self, item_id: int) -> ItemPaths:
        return ItemPaths(self.root / self.prefix_for(item_id) / str(int(item_id)))

    def ensure(self, item_id: int) -> ItemPaths:
        paths = self.paths(item_id)
        paths.directory.mkdir(parents=True, exist_ok=True)
        return paths

    # -- место ----------------------------------------------------------
    def free_bytes(self) -> int:
        usage = shutil.disk_usage(self.root)
        return int(usage.free)

    def has_space(self, needed: int = 0) -> bool:
        return self.free_bytes() >= self.min_free_bytes + max(0, needed)

    # -- метаданные -----------------------------------------------------
    def save_metadata(self, item_id: int, metadata: dict[str, Any]) -> Path:
        paths = self.ensure(item_id)
        temporary = paths.metadata.with_suffix(".json.tmp")
        payload = json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True)
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, paths.metadata)
        return paths.metadata

    def read_metadata(self, item_id: int) -> dict[str, Any] | None:
        path = self.paths(item_id).metadata
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    # -- видео ----------------------------------------------------------
    def existing_video(self, item_id: int) -> Path | None:
        return self.paths(item_id).any_video()

    def total_size(self) -> int:
        total = 0
        for path in self.root.rglob("video.*"):
            try:
                total += path.stat().st_size
            except OSError:
                continue
        return total

    # -- уборка ---------------------------------------------------------
    def cleanup_partials(self, max_age_seconds: float = 24 * 3600) -> int:
        """Удалить старые недокачанные файлы. Возвращает число удалённых."""
        removed = 0
        deadline = time.time() - max_age_seconds
        for path in self.root.rglob("video.part"):
            try:
                if path.stat().st_mtime < deadline:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        return removed
