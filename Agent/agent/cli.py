# -*- coding: utf-8 -*-
"""Командная строка агента.

    agentctl run --config config/agent.yaml
    agentctl run --once --config ...        # один блок и выход
    agentctl check-config --config ...
    agentctl doctor --config ...
    agentctl version
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path
from typing import Sequence

from . import PROTOCOL_VERSION, __version__
from .config import AgentConfig, describe, load_config
from .errors import ConfigError
from .logging_setup import get_logger, setup_logging
from .scheduler import AgentRuntime

__all__ = ["main", "build_parser"]

_log = get_logger("cli")

DEFAULT_CONFIG = "config/agent.yaml"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentctl",
        description="Агент рабочего узла: проверка идентификаторов и загрузка материалов",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=("run", "check-config", "doctor", "version"),
        help="что сделать (по умолчанию run)",
    )
    parser.add_argument(
        "--config",
        "-c",
        default=DEFAULT_CONFIG,
        help=f"файл конфигурации (по умолчанию {DEFAULT_CONFIG})",
    )
    parser.add_argument("--once", action="store_true", help="обработать один блок и выйти")
    parser.add_argument("--log-level", default=None, help="переопределить уровень журнала")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "version":
        print(f"agent {__version__} (protocol {PROTOCOL_VERSION})")
        return 0

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"ошибка конфигурации: {exc}", file=sys.stderr)
        return 1

    level = args.log_level or config.log_level
    setup_logging(level, config.log_json)

    if args.command == "check-config":
        print(json.dumps(describe(config), ensure_ascii=False, indent=2))
        return 0

    if args.command == "doctor":
        return _doctor(config)

    return _run(config, once=args.once)


# --------------------------------------------------------------------------
def _run(config: AgentConfig, once: bool) -> int:
    runtime = AgentRuntime(config)

    def _handle_signal(signum: int, _frame: object) -> None:
        _log.info("получен сигнал остановки", extra={"signal": signum})
        runtime.stop()

    for attribute in ("SIGTERM", "SIGINT"):
        user_signal = getattr(signal, attribute, None)
        if user_signal is not None:
            try:
                signal.signal(user_signal, _handle_signal)
            except (ValueError, OSError):  # не главный поток или Windows-особенность
                pass

    try:
        return runtime.run(once=once)
    except KeyboardInterrupt:
        runtime.stop()
        return 130


def _doctor(config: AgentConfig) -> int:
    """Проверка окружения: каталоги, место, координатор."""
    problems = 0
    storage = config.storage_path
    state = config.state_dir

    for label, path in (("каталог хранения", storage), ("каталог состояния", state)):
        if path.exists() and path.is_dir():
            print(f"[ok] {label}: {path}")
        else:
            print(f"[!!] {label} отсутствует: {path}")
            problems += 1

    try:
        probe = state / ".doctor"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        print("[ok] запись в каталог состояния")
    except OSError as exc:
        print(f"[!!] нет записи в каталог состояния: {exc}")
        problems += 1

    try:
        runtime = AgentRuntime(config)
        free = runtime.storage.free_bytes()
        required = int(config.min_free_space_gb * 1024 ** 3)
        print(f"[ok] свободно {free / 1024 ** 3:.1f} ГБ, требуется {config.min_free_space_gb:.1f} ГБ")
        if free < required:
            print("[!!] свободного места меньше минимального резерва")
            problems += 1
        if runtime.client.health():
            print(f"[ok] координатор отвечает: {config.coordinator.base_url}")
        else:
            print(f"[!!] координатор недоступен: {config.coordinator.base_url}")
            problems += 1
        runtime.http.close()
    except Exception as exc:  # noqa: BLE001 - диагностика не должна падать
        print(f"[!!] проверка не удалась: {exc}")
        problems += 1

    print(f"шаблон цели: {config.site.page_url_template}")
    print(f"протокол: {config.coordinator.protocol_version}")
    if problems:
        print(f"проблем: {problems}")
        return 2
    print("проблем не найдено")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
