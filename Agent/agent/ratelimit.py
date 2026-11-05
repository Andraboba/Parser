# -*- coding: utf-8 -*-
"""Ограничитель частоты запросов: токен-ведро с возможностью менять скорость."""

from __future__ import annotations

import threading
import time

__all__ = ["TokenBucket", "AdaptiveLimiter"]


class TokenBucket:
    """Потокобезопасное токен-ведро.

    Скорость можно менять на лету: этим пользуется адаптивное снижение
    нагрузки при ответах 429 и 5xx (ТЗ, п. 9).
    """

    def __init__(self, rate: float, burst: float | None = None) -> None:
        if rate <= 0:
            raise ValueError("rate должен быть положительным")
        self._rate = float(rate)
        self._burst = float(burst) if burst is not None else max(1.0, float(rate))
        self._tokens = self._burst
        self._updated = time.monotonic()
        self._condition = threading.Condition()

    @property
    def rate(self) -> float:
        with self._condition:
            return self._rate

    def set_rate(self, rate: float) -> None:
        if rate <= 0:
            raise ValueError("rate должен быть положительным")
        with self._condition:
            self._refill()
            self._rate = float(rate)
            self._burst = max(1.0, float(rate))
            self._tokens = min(self._tokens, self._burst)
            self._condition.notify_all()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._updated
        if elapsed > 0:
            self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
            self._updated = now

    def acquire(self, tokens: float = 1.0, timeout: float | None = None) -> bool:
        """Взять разрешение на запрос. False — если не дождались за timeout."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while True:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return True
                missing = tokens - self._tokens
                wait = missing / self._rate
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    wait = min(wait, remaining)
                self._condition.wait(timeout=max(wait, 0.001))


class AdaptiveLimiter:
    """Локальный лимит с реакцией на 429/5xx.

    При ограничении скорость снижается вдвое (но не ниже минимума),
    после серии успешных ответов постепенно возвращается к базовой.
    """

    def __init__(
        self,
        rate: float,
        min_rate: float = 0.5,
        recover_after: int = 20,
        recover_factor: float = 1.15,
    ) -> None:
        self._base = float(rate)
        self._min = float(min_rate)
        self._recover_after = max(1, int(recover_after))
        self._recover_factor = float(recover_factor)
        self._bucket = TokenBucket(rate)
        self._lock = threading.Lock()
        self._successes = 0
        self._throttled = 0

    @property
    def bucket(self) -> TokenBucket:
        return self._bucket

    @property
    def rate(self) -> float:
        return self._bucket.rate

    def acquire(self, tokens: float = 1.0, timeout: float | None = None) -> bool:
        return self._bucket.acquire(tokens, timeout)

    def on_success(self) -> None:
        with self._lock:
            self._successes += 1
            if self._successes < self._recover_after:
                return
            self._successes = 0
            current = self._bucket.rate
            if current < self._base:
                self._bucket.set_rate(min(self._base, current * self._recover_factor))

    def on_throttled(self) -> None:
        with self._lock:
            self._successes = 0
            self._throttled += 1
            current = self._bucket.rate
            self._bucket.set_rate(max(self._min, current / 2.0))

    @property
    def throttled_events(self) -> int:
        return self._throttled
