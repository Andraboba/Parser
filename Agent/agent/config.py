# -*- coding: utf-8 -*-
"""Конфигурация агента: загрузка, проверка, значения по умолчанию.

Источники, по возрастанию приоритета:

1. значения по умолчанию в этом модуле;
2. файл конфигурации (YAML поддерживаемого подмножества или JSON);
3. переменные окружения ``AGENT_*``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

from .errors import ConfigError
from .yamlish import YamlError, load_file

__all__ = [
    "AgentConfig",
    "CoordinatorConfig",
    "DownloadPolicy",
    "LimitsConfig",
    "RetryConfig",
    "SiteConfig",
    "load_config",
    "parse_config",
    "describe",
]

_VALID_POLICIES = ("public_only", "never")


@dataclass(frozen=True)
class RetryConfig:
    """Повторные попытки: экспоненциальная задержка с джиттером (ТЗ, п. 9)."""

    max_retries: int = 4
    base_delay: float = 1.0
    max_delay: float = 30.0
    jitter: float = 0.3

    def delay(self, attempt: int, random_value: float = 0.5) -> float:
        raw = min(self.max_delay, self.base_delay * (2 ** max(attempt, 0)))
        return max(0.0, raw * (1.0 + self.jitter * (2.0 * random_value - 1.0)))


@dataclass(frozen=True)
class CoordinatorConfig:
    """Параметры обмена с координатором."""

    base_url: str = "http://127.0.0.1:8000"
    token: str = ""
    protocol_version: int = 1
    request_timeout: float = 10.0
    heartbeat_interval: float = 5.0
    lease_ttl: float = 60.0
    poll_interval: float = 2.0


@dataclass(frozen=True)
class LimitsConfig:
    """Ограничения нагрузки (ТЗ, п. 9)."""

    rps: float = 20.0
    max_connections: int = 16
    # По умолчанию не ниже agent.threads (8), иначе заявленные потоки не работали бы.
    check_concurrency: int = 8
    download_concurrency: int = 2


@dataclass(frozen=True)
class DownloadPolicy:
    """Что можно скачивать, до какого размера и каким путём."""

    policy: str = "public_only"
    max_file_size_bytes: int = 2 * 1024 ** 3
    # direct — прямой mp4 из download-links (со звуком), hls — сборка сегментов
    # (у Kinescope видео без звука: аудио идёт отдельной дорожкой).
    prefer: str = "hls"


@dataclass(frozen=True)
class SiteConfig:
    """Всё, что зависит от целевого сайта. Единственное место таких знаний."""

    page_url_template: str = "http://kinescope.io/{id}"
    name: str = "target-video-http"
    request_timeout_seconds: float = 20.0
    user_agent: str = "lab-agent/1.0 (educational project)"
    follow_redirects: bool = False
    allowed_redirect_hosts: tuple[str, ...] = ()
    found_markers: tuple[str, ...] = ()
    missing_markers: tuple[str, ...] = (
        "страница не найдена",
        "page not found",
        "not found",
        "видео не найдено",
    )
    player_config_path: str | None = None
    download: DownloadPolicy = field(default_factory=DownloadPolicy)
    headers: Mapping[str, str] = field(default_factory=dict)

    def url_for(self, item_id: int) -> str:
        try:
            return self.page_url_template.format(id=item_id)
        except (KeyError, IndexError) as exc:  # pragma: no cover - защита от кривого шаблона
            raise ConfigError(f"шаблон адреса не содержит {{id}}: {self.page_url_template}") from exc


@dataclass(frozen=True)
class AgentConfig:
    """Полная конфигурация агента."""

    name: str = "worker-01"
    threads: int = 8
    storage_path: Path = Path("/data/videos")
    state_dir: Path = Path("/data/videos/.agent")
    min_free_space_gb: float = 2.0
    log_level: str = "INFO"
    log_json: bool = True
    metrics_interval: float = 5.0
    coordinator: CoordinatorConfig = field(default_factory=CoordinatorConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    site: SiteConfig = field(default_factory=SiteConfig)


# --------------------------------------------------------------------------
# разбор структуры
# --------------------------------------------------------------------------
def _section(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"раздел «{key}» должен быть отображением, получено {type(value).__name__}")
    return value


def _get_str(section: Mapping[str, Any], key: str, default: str, where: str) -> str:
    value = section.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ConfigError(f"{where}.{key}: ожидалась строка, получено {type(value).__name__}")
    return value


def _get_float(section: Mapping[str, Any], key: str, default: float, where: str) -> float:
    value = section.get(key, default)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}.{key}: ожидалось число, получено {type(value).__name__}")
    return float(value)


def _get_int(section: Mapping[str, Any], key: str, default: int, where: str) -> int:
    value = section.get(key, default)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, float) and value.is_integer():
            return int(value)
        raise ConfigError(f"{where}.{key}: ожидалось целое число, получено {type(value).__name__}")
    return value


def _get_bool(section: Mapping[str, Any], key: str, default: bool, where: str) -> bool:
    value = section.get(key, default)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ConfigError(f"{where}.{key}: ожидалось true/false, получено {type(value).__name__}")
    return value


def _get_str_list(section: Mapping[str, Any], key: str, where: str) -> tuple[str, ...]:
    value = section.get(key)
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, list):
        raise ConfigError(f"{where}.{key}: ожидался список строк")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ConfigError(f"{where}.{key}: элементы должны быть строками")
        result.append(item)
    return tuple(result)


def _resolve_secret(value: str) -> str:
    """Подставить значение из окружения, если задана ссылка вида ${NAME}."""
    if value.startswith("${") and value.endswith("}") and len(value) > 3:
        return os.environ.get(value[2:-1], "")
    return value


def parse_config(data: Mapping[str, Any]) -> AgentConfig:
    """Собрать и проверить конфигурацию из уже разобранного словаря."""
    if not isinstance(data, Mapping):
        raise ConfigError("корень конфигурации должен быть отображением")

    agent = _section(data, "agent")
    coordinator = _section(data, "coordinator")
    limits = _section(data, "limits")
    retry = _section(data, "retry")
    site = _section(data, "site_adapter")

    name = _get_str(agent, "name", "worker-01", "agent").strip()
    if not name:
        raise ConfigError("agent.name не может быть пустым")

    threads = _get_int(agent, "threads", 8, "agent")
    if not 1 <= threads <= 128:
        raise ConfigError("agent.threads должен быть в диапазоне 1..128")

    storage_raw = _get_str(agent, "storage_path", "/data/videos", "agent")
    storage_path = Path(os.path.expandvars(storage_raw)).expanduser()
    state_raw = _get_str(agent, "state_dir", "", "agent")
    state_dir = (
        Path(os.path.expandvars(state_raw)).expanduser()
        if state_raw
        else storage_path / ".agent"
    )

    min_free = _get_float(agent, "min_free_space_gb", 2.0, "agent")
    if min_free < 0:
        raise ConfigError("agent.min_free_space_gb не может быть отрицательным")

    base_url = _get_str(coordinator, "base_url", "http://127.0.0.1:8000", "coordinator").rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise ConfigError("coordinator.base_url должен начинаться с http:// или https://")

    token = _resolve_secret(_get_str(coordinator, "token", "", "coordinator"))

    heartbeat = _get_float(coordinator, "heartbeat_interval", 5.0, "coordinator")
    lease_ttl = _get_float(coordinator, "lease_ttl", 60.0, "coordinator")
    if heartbeat <= 0 or lease_ttl <= 0:
        raise ConfigError("интервалы heartbeat и аренды должны быть положительными")
    if heartbeat * 2 > lease_ttl:
        raise ConfigError(
            "coordinator.lease_ttl должен быть не меньше двух интервалов heartbeat, "
            "иначе блок будет возвращаться в очередь раньше продления"
        )

    rps = _get_float(limits, "rps", 20.0, "limits")
    if rps <= 0:
        raise ConfigError("limits.rps должен быть положительным")

    template = _get_str(site, "page_url_template", "http://kinescope.io/{id}", "site_adapter")
    if "{id}" not in template:
        raise ConfigError("site_adapter.page_url_template должен содержать {id}")

    download_section = _section(site, "download")
    policy = _get_str(download_section, "policy", "public_only", "site_adapter.download")
    if policy not in _VALID_POLICIES:
        raise ConfigError(
            f"site_adapter.download.policy: допустимо {', '.join(_VALID_POLICIES)}, получено «{policy}»"
        )
    prefer = _get_str(download_section, "prefer", "hls", "site_adapter.download")
    if prefer not in ("hls", "direct"):
        raise ConfigError(
            f"site_adapter.download.prefer: допустимо hls, direct, получено «{prefer}»"
        )
    max_size = _get_int(
        download_section, "max_file_size_bytes", 2 * 1024 ** 3, "site_adapter.download"
    )
    if max_size <= 0:
        raise ConfigError("site_adapter.download.max_file_size_bytes должен быть положительным")

    headers_raw = site.get("headers") or {}
    if not isinstance(headers_raw, Mapping):
        raise ConfigError("site_adapter.headers должен быть отображением")
    headers = {str(key): str(value) for key, value in headers_raw.items()}

    cfg = AgentConfig(
        name=name,
        threads=threads,
        storage_path=storage_path,
        state_dir=state_dir,
        min_free_space_gb=min_free,
        log_level=_get_str(agent, "log_level", "INFO", "agent").upper(),
        log_json=_get_bool(agent, "log_json", True, "agent"),
        metrics_interval=_get_float(agent, "metrics_interval", 5.0, "agent"),
        coordinator=CoordinatorConfig(
            base_url=base_url,
            token=token,
            protocol_version=_get_int(coordinator, "protocol_version", 1, "coordinator"),
            request_timeout=_get_float(coordinator, "request_timeout", 10.0, "coordinator"),
            heartbeat_interval=heartbeat,
            lease_ttl=lease_ttl,
            poll_interval=_get_float(coordinator, "poll_interval", 2.0, "coordinator"),
        ),
        limits=LimitsConfig(
            rps=rps,
            max_connections=_get_int(limits, "max_connections", 16, "limits"),
            check_concurrency=_get_int(limits, "check_concurrency", 8, "limits"),
            download_concurrency=_get_int(limits, "download_concurrency", 2, "limits"),
        ),
        retry=RetryConfig(
            max_retries=_get_int(retry, "max_retries", 4, "retry"),
            base_delay=_get_float(retry, "base_delay", 1.0, "retry"),
            max_delay=_get_float(retry, "max_delay", 30.0, "retry"),
            jitter=_get_float(retry, "jitter", 0.3, "retry"),
        ),
        site=SiteConfig(
            page_url_template=template,
            name=_get_str(site, "name", "target-video-http", "site_adapter"),
            request_timeout_seconds=_get_float(
                site, "request_timeout_seconds", 20.0, "site_adapter"
            ),
            user_agent=_get_str(site, "user_agent", "lab-agent/1.0", "site_adapter"),
            follow_redirects=_get_bool(site, "follow_redirects", False, "site_adapter"),
            allowed_redirect_hosts=_get_str_list(site, "allowed_redirect_hosts", "site_adapter"),
            found_markers=_get_str_list(site, "found_markers", "site_adapter"),
            missing_markers=_get_str_list(site, "missing_markers", "site_adapter")
            or SiteConfig.missing_markers,
            player_config_path=(
                _get_str(site, "player_config_path", "", "site_adapter") or None
            ),
            download=DownloadPolicy(policy=policy, max_file_size_bytes=max_size, prefer=prefer),
            headers=headers,
        ),
    )
    return _apply_env(cfg, state_explicit=bool(state_raw))


def _apply_env(cfg: AgentConfig, state_explicit: bool = True) -> AgentConfig:
    """Переопределения из окружения — удобно для docker compose."""
    changes: dict[str, Any] = {}
    if os.environ.get("AGENT_NAME"):
        changes["name"] = os.environ["AGENT_NAME"]
    if os.environ.get("AGENT_THREADS"):
        try:
            changes["threads"] = int(os.environ["AGENT_THREADS"])
        except ValueError as exc:
            raise ConfigError("AGENT_THREADS должен быть числом") from exc
    if os.environ.get("AGENT_STORAGE_PATH"):
        storage = Path(os.environ["AGENT_STORAGE_PATH"]).expanduser()
        changes["storage_path"] = storage
        if not state_explicit:
            # каталог состояния по умолчанию живёт рядом с данными
            changes["state_dir"] = storage / ".agent"
    if os.environ.get("AGENT_LOG_LEVEL"):
        changes["log_level"] = os.environ["AGENT_LOG_LEVEL"].upper()

    coordinator_changes: dict[str, Any] = {}
    if os.environ.get("AGENT_COORDINATOR_URL"):
        coordinator_changes["base_url"] = os.environ["AGENT_COORDINATOR_URL"].rstrip("/")
    if os.environ.get("AGENT_TOKEN"):
        coordinator_changes["token"] = os.environ["AGENT_TOKEN"]
    if coordinator_changes:
        changes["coordinator"] = replace(cfg.coordinator, **coordinator_changes)

    limits_changes: dict[str, Any] = {}
    if os.environ.get("AGENT_RPS"):
        try:
            limits_changes["rps"] = float(os.environ["AGENT_RPS"])
        except ValueError as exc:
            raise ConfigError("AGENT_RPS должен быть числом") from exc
    if limits_changes:
        changes["limits"] = replace(cfg.limits, **limits_changes)

    site_changes: dict[str, Any] = {}
    if os.environ.get("AGENT_SITE_TEMPLATE"):
        site_changes["page_url_template"] = os.environ["AGENT_SITE_TEMPLATE"]
    if site_changes:
        changes["site"] = replace(cfg.site, **site_changes)

    return replace(cfg, **changes) if changes else cfg


def load_config(path: str | Path) -> AgentConfig:
    """Прочитать конфигурацию из файла и проверить её."""
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"файл конфигурации не найден: {path}")
    try:
        data = load_file(path)
    except YamlError as exc:
        raise ConfigError(str(exc)) from exc
    cfg = parse_config(data)
    _ensure_dirs(cfg)
    return cfg


def _ensure_dirs(cfg: AgentConfig) -> None:
    """Создать каталоги хранения и состояния, если их нет."""
    for directory in (cfg.storage_path, cfg.state_dir):
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConfigError(f"не удалось создать каталог {directory}: {exc}") from exc
    if not os.access(cfg.storage_path, os.W_OK):
        raise ConfigError(f"каталог хранения недоступен для записи: {cfg.storage_path}")


def describe(cfg: AgentConfig) -> dict[str, Any]:
    """Представление конфигурации без секретов — для --check-config."""
    return {
        "agent": {
            "name": cfg.name,
            "threads": cfg.threads,
            "storage_path": str(cfg.storage_path),
            "state_dir": str(cfg.state_dir),
            "min_free_space_gb": cfg.min_free_space_gb,
            "log_level": cfg.log_level,
            "log_json": cfg.log_json,
        },
        "coordinator": {
            "base_url": cfg.coordinator.base_url,
            "token": "***" if cfg.coordinator.token else "(пусто)",
            "protocol_version": cfg.coordinator.protocol_version,
            "heartbeat_interval": cfg.coordinator.heartbeat_interval,
            "lease_ttl": cfg.coordinator.lease_ttl,
        },
        "limits": {
            "rps": cfg.limits.rps,
            "max_connections": cfg.limits.max_connections,
            "check_concurrency": cfg.limits.check_concurrency,
            "download_concurrency": cfg.limits.download_concurrency,
        },
        "site_adapter": {
            "name": cfg.site.name,
            "page_url_template": cfg.site.page_url_template,
            "download_policy": cfg.site.download.policy,
            "download_prefer": cfg.site.download.prefer,
            "max_file_size_bytes": cfg.site.download.max_file_size_bytes,
            "follow_redirects": cfg.site.follow_redirects,
        },
    }
