# -*- coding: utf-8 -*-
"""Локальное состояние агента: журнал текущего блока и очередь отправки.

Контейнер может быть пересоздан в любой момент, поэтому всё, что нельзя
потерять, лежит на диске: текущий блок (для диагностики и быстрого
продолжения) и подтверждения, которые не удалось доставить координатору.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

__all__ = ["StateStore", "Outbox"]


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


class StateStore:
    """Небольшой JSON-файл с состоянием текущего блока."""

    def __init__(self, directory: Path) -> None:
        self.path = Path(directory) / "state.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def save(self, state: dict[str, Any]) -> None:
        _atomic_write(self.path, state)

    def clear(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass


class Outbox:
    """Очередь недоставленных подтверждений (complete/fail).

    Каждое сообщение — отдельный файл. Файл удаляется только после того,
    как координатор подтвердил приём, поэтому падение процесса не теряет
    результат уже проделанной работы.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory) / "outbox"
        self.directory.mkdir(parents=True, exist_ok=True)

    def add(self, kind: str, payload: dict[str, Any]) -> str:
        entry_id = f"{time.time_ns():020d}-{uuid.uuid4().hex[:8]}"
        _atomic_write(self.directory / f"{entry_id}.json", {"kind": kind, "payload": payload})
        return entry_id

    def pending(self) -> list[tuple[str, dict[str, Any]]]:
        entries: list[tuple[str, dict[str, Any]]] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            kind = data.get("kind")
            payload = data.get("payload")
            if isinstance(kind, str) and isinstance(payload, dict):
                entries.append((path.stem, {"kind": kind, "payload": payload}))
        return entries

    def remove(self, entry_id: str) -> None:
        try:
            (self.directory / f"{entry_id}.json").unlink()
        except OSError:
            pass

    def __len__(self) -> int:
        return len(list(self.directory.glob("*.json")))
