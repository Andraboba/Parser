"""Задержки повторов и классификация ошибок (ТЗ, п. 7 и 9)."""
import random

BASE_DELAY_S = 1.0
MAX_DELAY_S = 60.0

# Ошибка конфигурации не лечится повтором: задание завершается (ТЗ, п. 10).
CRITICAL_ERROR_CLASSES = frozenset({"config_error", "invalid_config"})
# Отказ в доступе не повторяем: это запрет, а не сбой (ТЗ, п. 7).
NOT_RETRYABLE_ITEM_STATUSES = frozenset({"forbidden", "not_found", "skipped", "completed"})
# Эти элементы возвращает в работу повтор ошибок из интерфейса.
RETRYABLE_ITEM_STATUSES = ("failed", "rate_limited", "retry_wait", "cancelled")

# Группы ошибок для отчётов и фильтров. Класс без группы считается прочим.
ERROR_GROUPS: dict[str, str] = {
    "network": "network",
    "timeout": "network",
    "connection_reset": "network",
    "server_error": "server",
    "http_error": "server",
    "rate_limited": "rate_limit",
    "forbidden": "access",
    "unauthorized": "access",
    "not_found": "missing",
    "no_material": "missing",
    "download_error": "download",
    "size_mismatch": "download",
    "checksum_mismatch": "download",
    "range_mismatch": "download",
    "too_large": "policy",
    "insufficient_space": "disk",
    "disk_full": "disk",
    "invalid_path": "security",
    "config_error": "config",
    "invalid_config": "config",
    "lease_expired": "lease",
    "cancelled": "cancelled",
    "shutdown": "cancelled",
}


def error_group(error_class: str | None) -> str | None:
    if error_class is None:
        return None
    if error_class.startswith("http_5"):
        return "server"
    if error_class.startswith("http_4"):
        return "client"
    return ERROR_GROUPS.get(error_class, "other")


def retry_delay(
    attempt: int, base: float = BASE_DELAY_S, maximum: float = MAX_DELAY_S,
    jitter: float | None = None,
) -> float:
    """retry_delay = min(max_delay, base_delay * 2^attempt) + jitter (ТЗ, п. 9)."""
    delay = min(maximum, base * 2 ** max(0, attempt))
    return delay + (random.uniform(0, delay * 0.1) if jitter is None else jitter)
