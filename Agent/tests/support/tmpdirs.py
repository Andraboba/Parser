# -*- coding: utf-8 -*-
"""Временные каталоги для тестов.

Системный каталог временных файлов может быть недоступен на запись,
а ``tempfile.mkdtemp`` на некоторых конфигурациях Windows создаёт каталог
с ограниченными правами. Поэтому каталог создаётся обычным ``mkdir``
внутри проекта и удаляется вместе с содержимым.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

__all__ = ["make_temp_dir"]

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_FALLBACK = _PROJECT_ROOT / ".tmp"


def _usable_base() -> Path:
    base = _FALLBACK
    try:
        base.mkdir(parents=True, exist_ok=True)
        probe = base / f".probe-{uuid.uuid4().hex[:6]}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return base
    except OSError:
        import tempfile

        fallback = Path(tempfile.gettempdir()) / "lab-agent-tests"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


class TempDir:
    """Каталог, который удаляется при выходе из контекста."""

    def __init__(self) -> None:
        base = _usable_base()
        self.path = base / f"t-{uuid.uuid4().hex[:10]}"
        self.path.mkdir(parents=True, exist_ok=False)
        self.name = str(self.path)

    def __enter__(self) -> str:
        return self.name

    def __exit__(self, *exc_info: object) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def make_temp_dir() -> TempDir:
    return TempDir()
