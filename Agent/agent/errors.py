# -*- coding: utf-8 -*-
"""Типы ошибок агента.

Классы ошибок отделены от текстов: по классу принимается решение —
повторять, остановиться или пометить элемент окончательно ошибочным.
"""

from __future__ import annotations


class AgentError(Exception):
    """Базовая ошибка агента."""


class ConfigError(AgentError):
    """Конфигурация отсутствует, не разбирается или противоречива."""


class NetworkError(AgentError):
    """Сетевая ошибка: тайм-аут, обрыв, TLS, DNS."""


class HttpStatusError(AgentError):
    """Неожидаемый HTTP-статус."""

    def __init__(self, status: int, url: str = "", retry_after: float | None = None) -> None:
        self.status = status
        self.url = url
        self.retry_after = retry_after
        super().__init__(f"HTTP {status} for {url}")


class CoordinatorError(AgentError):
    """Ошибка обмена с координатором."""

    def __init__(self, message: str, status: int | None = None, retryable: bool = False) -> None:
        self.status = status
        self.retryable = retryable
        super().__init__(message)


class DownloadError(AgentError):
    """Ошибка загрузки файла."""

    error_class = "download_error"

    def __init__(self, message: str, error_class: str | None = None) -> None:
        if error_class:
            self.error_class = error_class
        super().__init__(message)


class InsufficientSpace(DownloadError):
    error_class = "insufficient_space"


class TooLargeFile(DownloadError):
    error_class = "too_large"


class SizeMismatch(DownloadError):
    error_class = "size_mismatch"


class LeaseExpired(AgentError):
    """Аренда блока истекла: работу нужно прекратить."""


class ShutdownRequested(AgentError):
    """Агент получил сигнал остановки."""
