# -*- coding: utf-8 -*-
"""Настройка журналирования.

Формат — JSON в stdout: так журнал читается и человеком, и сборщиком логов
в контейнере (ТЗ, п. 15). Все поля, требуемые ТЗ, передаются через extra.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, MutableMapping

__all__ = ["setup_logging", "get_logger", "log_extra", "JsonFormatter"]

# Поля, которые нельзя записывать в журнал (ТЗ, п. 15).
_FORBIDDEN_KEYS = {
    "token",
    "password",
    "secret",
    "authorization",
    "ssh_key",
    "cookie",
    "private_key",
}

_STANDARD_ATTRS = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "taskName",
    "message",
    "asctime",
}


def _scrub(key: str, value: Any) -> Any:
    lowered = key.lower()
    if any(marker in lowered for marker in _FORBIDDEN_KEYS):
        return "***"
    if isinstance(value, str) and len(value) > 500:
        return value[:500] + "…"
    return value


class JsonFormatter(logging.Formatter):
    """Одна строка JSON на запись журнала."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "component": getattr(record, "component", record.name),
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS or key in payload or key.startswith("_"):
                continue
            payload[key] = _scrub(key, value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    """Читаемый формат для локальной разработки."""

    def format(self, record: logging.LogRecord) -> str:
        stamp = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%H:%M:%S")
        base = f"{stamp} {record.levelname:<7} {getattr(record, 'component', record.name)}: {record.getMessage()}"
        extras = []
        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS or key.startswith("_"):
                continue
            extras.append(f"{key}={value}")
        if extras:
            base += " | " + " ".join(extras)
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def setup_logging(level: str = "INFO", as_json: bool = True) -> None:
    """Настроить корневой журнал один раз для всего процесса."""
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter() if as_json else TextFormatter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Сторонние библиотеки не должны засорять журнал своими строками.
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def get_logger(component: str) -> logging.LoggerAdapter:
    """Журнал с постоянным полем component."""
    return logging.LoggerAdapter(logging.getLogger(component), {"component": component})


def log_extra(**fields: Any) -> MutableMapping[str, Any]:
    """Собрать extra для записи журнала (имена не должны конфликтовать со стандартными)."""
    return {key: value for key, value in fields.items() if key not in _STANDARD_ATTRS}


def container_id() -> str | None:
    """Идентификатор контейнера для регистрации у координатора."""
    explicit = os.environ.get("AGENT_CONTAINER_ID")
    if explicit:
        return explicit
    try:
        with open("/proc/self/cgroup", encoding="utf-8") as handle:
            for line in handle:
                for part in line.strip().replace("\\", "/").split("/"):
                    if len(part) == 64 and all(char in "0123456789abcdef" for char in part):
                        return part
    except OSError:
        pass
    # В docker имя хоста по умолчанию равно короткому id контейнера.
    hostname = os.environ.get("HOSTNAME") or ""
    if len(hostname) == 12 and all(char in "0123456789abcdef" for char in hostname):
        return hostname
    return None
