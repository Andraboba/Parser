# -*- coding: utf-8 -*-
"""Клиент координатора: регистрация, heartbeat, аренда, прогресс, закрытие блока.

Повторные попытки выполняются здесь же: сетевые ошибки, 429 и 5xx
повторяются с экспоненциальной задержкой, 4xx — нет. Подтверждения
блока (complete/fail), которые не удалось доставить, попадают в outbox
и досылаются позже — иначе пересоздание контейнера потеряло бы результат.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Mapping

from .config import CoordinatorConfig, RetryConfig
from .errors import CoordinatorError, NetworkError
from .http_client import HttpClient
from .journal import Outbox
from .logging_setup import get_logger

__all__ = ["Chunk", "CoordinatorClient", "RegisterResult"]

_log = get_logger("coordinator")

_RETRYABLE_STATUSES = (429, 500, 502, 503, 504)


@dataclass(frozen=True)
class RegisterResult:
    worker_id: str
    heartbeat_interval: float
    lease_ttl: float


@dataclass(frozen=True)
class Chunk:
    """Арендованный блок идентификаторов."""

    chunk_id: int
    job_id: int
    range_start: int
    range_end: int
    next_id: int
    deadline: float
    rps_share: float | None = None

    @property
    def size(self) -> int:
        return max(0, self.range_end - self.next_id + 1)

    def expired(self, now: float | None = None) -> bool:
        return (now if now is not None else time.monotonic()) >= self.deadline

    def extend(self, seconds: float) -> "Chunk":
        now = time.monotonic()
        base = max(now, self.deadline - seconds)
        return Chunk(
            chunk_id=self.chunk_id,
            job_id=self.job_id,
            range_start=self.range_start,
            range_end=self.range_end,
            next_id=self.next_id,
            deadline=base + seconds,
            rps_share=self.rps_share,
        )


class CoordinatorClient:
    """Тонкая обёртка над HTTP API координатора."""

    def __init__(
        self,
        config: CoordinatorConfig,
        http: HttpClient,
        retry: RetryConfig,
        outbox: Outbox | None = None,
    ) -> None:
        self.config = config
        self.http = http
        self.retry = retry
        self.outbox = outbox
        self.worker_id: str | None = None

    # ------------------------------------------------------------------
    def _url(self, path: str) -> str:
        return f"{self.config.base_url}{path}"

    def _headers(self, idempotency_key: str | None = None) -> dict[str, str]:
        headers = {
            "X-Agent-Token": self.config.token,
            "X-Protocol-Version": str(self.config.protocol_version),
            "User-Agent": "lab-agent/1.0",
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def _post(
        self,
        path: str,
        payload: Mapping[str, Any],
        expect: tuple[int, ...] = (200, 201, 204),
        idempotency_key: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        url = self._url(path)
        attempts = self.retry.max_retries + 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                status, headers, body = self.http.post_json(
                    url,
                    payload,
                    headers=self._headers(idempotency_key),
                    timeout=self.config.request_timeout,
                )
            except NetworkError as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    break
                delay = self.retry.delay(attempt, _random())
                _log.warning(
                    "координатор недоступен, повтор",
                    extra={"path": path, "attempt": attempt + 1, "delay": round(delay, 2)},
                )
                time.sleep(delay)
                continue

            data = _decode(body)
            if status in expect:
                return status, data
            if status in _RETRYABLE_STATUSES and attempt + 1 < attempts:
                retry_after = _retry_after(headers.get("retry-after"))
                delay = retry_after if retry_after is not None else self.retry.delay(attempt, _random())
                _log.warning(
                    "координатор ответил повтором",
                    extra={"path": path, "status": status, "delay": round(delay, 2)},
                )
                time.sleep(delay)
                continue
            raise CoordinatorError(
                f"координатор вернул {status} на {path}: {data or body[:200]!r}",
                status=status,
                retryable=status in _RETRYABLE_STATUSES,
            )
        raise CoordinatorError(f"координатор недоступен: {last_error}", retryable=True)

    # ------------------------------------------------------------------
    def register(self, payload: Mapping[str, Any]) -> RegisterResult:
        _, data = self._post("/api/workers/register", payload)
        worker_id = data.get("worker_id")
        if not worker_id:
            raise CoordinatorError("координатор не вернул worker_id")
        self.worker_id = str(worker_id)
        return RegisterResult(
            worker_id=str(worker_id),
            heartbeat_interval=float(data.get("heartbeat_interval") or self.config.heartbeat_interval),
            lease_ttl=float(data.get("lease_ttl") or self.config.lease_ttl),
        )

    def heartbeat(self, worker_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        _, data = self._post(f"/api/workers/{worker_id}/heartbeat", payload)
        return data

    def lease(self, worker_id: str, free_slots: int, ttl_hint: float | None = None) -> Chunk | None:
        status, data = self._post(
            f"/api/workers/{worker_id}/lease",
            {"worker_id": worker_id, "free_slots": free_slots},
            expect=(200, 204),
        )
        if status == 204 or not data:
            return None
        ttl = float(data.get("lease_ttl") or ttl_hint or self.config.lease_ttl)
        return Chunk(
            chunk_id=int(data["chunk_id"]),
            job_id=int(data.get("job_id", 0)),
            range_start=int(data["range_start"]),
            range_end=int(data["range_end"]),
            next_id=int(data.get("next_id", data["range_start"])),
            deadline=time.monotonic() + ttl,
            rps_share=_optional_float(data.get("rps_share")),
        )

    def progress(
        self,
        worker_id: str,
        chunk_id: int,
        next_id: int,
        results: list[dict[str, Any]],
        counters: Mapping[str, int],
    ) -> dict[str, Any]:
        payload = {
            "chunk_id": chunk_id,
            "next_id": next_id,
            "results": results,
            "counters": dict(counters),
        }
        _, data = self._post(f"/api/workers/{worker_id}/progress", payload)
        return data

    def complete(
        self,
        worker_id: str,
        chunk_id: int,
        next_id: int,
        results: list[dict[str, Any]],
        summary: Mapping[str, Any],
    ) -> bool:
        payload = {
            "chunk_id": chunk_id,
            "next_id": next_id,
            "results": results,
            "summary": dict(summary),
        }
        key = f"complete-{chunk_id}-{next_id}"
        try:
            self._post(f"/api/workers/{worker_id}/complete", payload, idempotency_key=key)
            return True
        except CoordinatorError as exc:
            _log.error(
                "не удалось закрыть блок, сохраняем в outbox",
                extra={"chunk_id": chunk_id, "error": str(exc)},
            )
            if self.outbox is not None:
                self.outbox.add("complete", {"worker_id": worker_id, **payload})
            return False

    def fail(
        self,
        worker_id: str,
        chunk_id: int,
        error_class: str,
        message: str,
    ) -> bool:
        payload = {"chunk_id": chunk_id, "error_class": error_class, "message": message[:500]}
        key = f"fail-{chunk_id}"
        try:
            self._post(f"/api/workers/{worker_id}/fail", payload, idempotency_key=key)
            return True
        except CoordinatorError as exc:
            _log.error(
                "не удалось сообщить об отказе блока",
                extra={"chunk_id": chunk_id, "error": str(exc)},
            )
            if self.outbox is not None:
                self.outbox.add("fail", {"worker_id": worker_id, **payload})
            return False

    # ------------------------------------------------------------------
    def flush_outbox(self) -> int:
        """Дослать сохранённые подтверждения. Возвращает число доставленных."""
        if self.outbox is None:
            return 0
        delivered = 0
        for entry_id, entry in self.outbox.pending():
            kind = entry["kind"]
            payload = dict(entry["payload"])
            worker_id = payload.pop("worker_id", None) or self.worker_id
            if not worker_id:
                continue
            try:
                if kind == "complete":
                    key = f"complete-{payload.get('chunk_id')}-{payload.get('next_id')}"
                    self._post(f"/api/workers/{worker_id}/complete", payload, idempotency_key=key)
                elif kind == "fail":
                    key = f"fail-{payload.get('chunk_id')}"
                    self._post(f"/api/workers/{worker_id}/fail", payload, idempotency_key=key)
                else:
                    self.outbox.remove(entry_id)
                    continue
            except CoordinatorError:
                break
            self.outbox.remove(entry_id)
            delivered += 1
        return delivered

    def health(self) -> bool:
        try:
            status, _, _ = self.http.get(
                self._url("/health"), headers=self._headers(), timeout=self.config.request_timeout
            )
            return status == 200
        except NetworkError:
            return False


# --------------------------------------------------------------------------
def _decode(body: bytes) -> dict[str, Any]:
    if not body:
        return {}
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _random() -> float:
    # небольшой джиттер без зависимости от global random state
    return (time.time_ns() % 1000) / 1000.0


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
