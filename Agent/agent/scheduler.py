# -*- coding: utf-8 -*-
"""Планировщик агента: аренда блока, параллельная проверка, отчёт о прогрессе.

Модель работы:

1. агент регистрируется и начинает отправлять heartbeat;
2. когда появились свободные слоты, арендует блок идентификаторов;
3. проверяет элементы блока параллельно, скачивает найденное;
4. раз в несколько секунд отправляет прогресс, который продлевает аренду;
5. закрывает блок — complete с полем next_id либо fail с классом ошибки.

Гарантия — at least once: повторная обработка возможна, поэтому сохранение
результатов идемпотентно, а незавершённый блок возвращается с next_id.
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from . import __version__
from .adapter import STATUS_FOUND, STATUS_RATE_LIMITED, SiteAdapter
from .config import AgentConfig
from .coordinator import Chunk, CoordinatorClient
from .downloader import Downloader
from .errors import CoordinatorError, DownloadError, NetworkError
from .http_client import HttpClient
from .journal import Outbox, StateStore
from .logging_setup import container_id, get_logger
from .ratelimit import AdaptiveLimiter
from .storage import Storage
from .telemetry import Telemetry

__all__ = ["AgentRuntime", "Counters"]

_log = get_logger("agent")

PROGRESS_INTERVAL = 2.0
PROGRESS_BATCH = 50
MAX_BUFFERED_RESULTS = 2000


@dataclass
class Counters:
    """Счётчики одного блока и агента в целом."""

    checked: int = 0
    found: int = 0
    completed: int = 0
    not_found: int = 0
    forbidden: int = 0
    rate_limited: int = 0
    failed: int = 0
    bytes_downloaded: int = 0

    def add(self, result: dict[str, Any]) -> None:
        self.checked += 1
        status = result.get("status")
        if status == "completed":
            self.completed += 1
            self.found += 1
        elif status == "found":
            self.found += 1
        elif status == "not_found":
            self.not_found += 1
        elif status == "forbidden":
            self.forbidden += 1
        elif status == "rate_limited":
            self.rate_limited += 1
        elif status == "failed":
            self.failed += 1
        file_info = result.get("file")
        if isinstance(file_info, dict):
            self.bytes_downloaded += int(file_info.get("size") or 0)

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


class AgentRuntime:
    """Рабочий цикл агента."""

    def __init__(
        self,
        config: AgentConfig,
        *,
        storage: Storage | None = None,
        http: HttpClient | None = None,
        client: CoordinatorClient | None = None,
        adapter: SiteAdapter | None = None,
        downloader: Downloader | None = None,
        telemetry: Telemetry | None = None,
    ) -> None:
        self.config = config
        self.concurrency = max(1, min(config.threads, config.limits.check_concurrency))
        self.limiter = AdaptiveLimiter(config.limits.rps)
        self.storage = storage or Storage(
            config.storage_path, int(config.min_free_space_gb * 1024 ** 3)
        )
        self.http = http or HttpClient(
            timeout=config.site.request_timeout_seconds,
            max_connections=config.limits.max_connections,
            limiter=self.limiter,
        )
        self.outbox = Outbox(config.state_dir)
        self.state = StateStore(config.state_dir)
        self.client = client or CoordinatorClient(
            config.coordinator, self.http, config.retry, self.outbox
        )
        self.adapter = adapter or SiteAdapter(config.site, self.http)
        self.downloader = downloader or Downloader(
            self.storage,
            self.http,
            max_bytes=config.site.download.max_file_size_bytes,
            timeout=max(60.0, config.site.request_timeout_seconds * 3),
        )
        self.telemetry = telemetry or Telemetry(self.storage)

        self.counters = Counters()
        self._heartbeat_interval = config.coordinator.heartbeat_interval
        self._lease_ttl = config.coordinator.lease_ttl
        self._stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self._current_chunk: Chunk | None = None
        self._cancelled_chunks: set[int] = set()
        self._worker_id: str | None = None
        self._download_slots = threading.BoundedSemaphore(max(1, config.limits.download_concurrency))

    # ------------------------------------------------------------------
    @property
    def worker_id(self) -> str | None:
        return self._worker_id

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def stop(self) -> None:
        """Попросить агента завершиться после текущего элемента."""
        self._stop.set()

    # ------------------------------------------------------------------
    def run(self, once: bool = False) -> int:
        """Основной цикл. Возвращает код выхода."""
        _log.info(
            "агент запускается",
            extra={
                "worker": self.config.name,
                "threads": self.concurrency,
                "storage": str(self.config.storage_path),
                "template": self.config.site.page_url_template,
                "rps": self.config.limits.rps,
            },
        )
        removed = self.storage.cleanup_partials()
        if removed:
            _log.info("удалены старые недокачанные файлы", extra={"count": removed})

        try:
            self._register()
        except CoordinatorError as exc:
            _log.error("регистрация не удалась", extra={"error": str(exc)})
            return 2

        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, name="heartbeat", daemon=True
        )
        self._heartbeat_thread.start()

        try:
            while not self._stop.is_set():
                chunk = self._lease()
                if chunk is None:
                    if once:
                        break
                    self._stop.wait(self.config.coordinator.poll_interval)
                    continue
                self._run_chunk(chunk)
                if once:
                    break
        finally:
            self._shutdown()
        return 0

    # ------------------------------------------------------------------
    def _register(self) -> None:
        metrics = self.telemetry.snapshot()
        payload = {
            "name": self.config.name,
            "agent_version": __version__,
            "protocol_version": self.config.coordinator.protocol_version,
            "threads": self.concurrency,
            "storage_path": str(self.config.storage_path),
            "container_id": container_id(),
            "image": os.environ.get("AGENT_IMAGE"),
            "limits": {
                "cpus": metrics.get("cpu_limit"),
                "memory_mb": (
                    round(metrics["memory_limit"] / 1024 ** 2)
                    if metrics.get("memory_limit")
                    else None
                ),
            },
        }
        result = self.client.register(payload)
        self._worker_id = result.worker_id
        # интервалы задаёт координатор: он же следит за арендой
        self._heartbeat_interval = max(1.0, result.heartbeat_interval)
        self._lease_ttl = max(self._heartbeat_interval * 2, result.lease_ttl)
        _log.info(
            "агент зарегистрирован",
            extra={
                "worker_id": result.worker_id,
                "heartbeat_interval": result.heartbeat_interval,
                "lease_ttl": result.lease_ttl,
            },
        )

    def _lease(self) -> Chunk | None:
        if self._worker_id is None:
            return None
        try:
            chunk = self.client.lease(
                self._worker_id,
                free_slots=self.concurrency,
                ttl_hint=self._lease_ttl,
            )
        except CoordinatorError as exc:
            _log.warning("не удалось арендовать блок", extra={"error": str(exc)})
            return None
        if chunk is not None:
            _log.info(
                "блок получен",
                extra={
                    "chunk_id": chunk.chunk_id,
                    "job_id": chunk.job_id,
                    "range_start": chunk.range_start,
                    "range_end": chunk.range_end,
                    "next_id": chunk.next_id,
                    "rps_share": chunk.rps_share,
                },
            )
            if chunk.rps_share:
                self.limiter.bucket.set_rate(max(0.1, chunk.rps_share))
        return chunk

    # ------------------------------------------------------------------
    def _run_chunk(self, chunk: Chunk) -> None:
        self._current_chunk = chunk
        deadline = chunk.deadline
        started = datetime.now(tz=timezone.utc)
        self.state.save(
            {
                "chunk_id": chunk.chunk_id,
                "job_id": chunk.job_id,
                "range_start": chunk.range_start,
                "range_end": chunk.range_end,
                "next_id": chunk.next_id,
                "started_at": started.isoformat(timespec="seconds"),
            }
        )

        next_id = chunk.next_id
        submitted = chunk.next_id
        inflight: dict[Future[dict[str, Any]], int] = {}
        done_ids: set[int] = set()
        buffer: list[dict[str, Any]] = []
        counters = Counters()
        window = max(4, self.concurrency * 2)
        last_progress = time.monotonic()
        expired = False

        pool = ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix="check")
        try:
            while True:
                now = time.monotonic()
                if now >= deadline:
                    if not expired:
                        _log.warning(
                            "аренда блока истекла",
                            extra={"chunk_id": chunk.chunk_id, "next_id": next_id},
                        )
                    expired = True

                while (
                    not expired
                    and not self._stop.is_set()
                    and chunk.chunk_id not in self._cancelled_chunks
                    and len(inflight) < window
                    and submitted <= chunk.range_end
                ):
                    inflight[pool.submit(self._process_item, submitted)] = submitted
                    submitted += 1

                if not inflight:
                    break

                done, _ = wait(set(inflight), timeout=0.2, return_when=FIRST_COMPLETED)
                for future in done:
                    item_id = inflight.pop(future)
                    try:
                        result = future.result()
                    except Exception as exc:  # noqa: BLE001 - один элемент не должен ронять блок
                        _log.exception("сбой обработки элемента", extra={"item_id": item_id})
                        result = {
                            "external_id": item_id,
                            "status": "failed",
                            "error_class": type(exc).__name__,
                        }
                    buffer.append(result)
                    counters.add(result)
                    done_ids.add(item_id)
                    while next_id in done_ids:
                        done_ids.discard(next_id)
                        next_id += 1

                now = time.monotonic()
                if buffer and (len(buffer) >= PROGRESS_BATCH or now - last_progress >= PROGRESS_INTERVAL):
                    if self._send_progress(chunk.chunk_id, next_id, buffer, counters):
                        buffer = []
                        last_progress = now
                        deadline = max(deadline, now + self._lease_ttl)
                if len(buffer) > MAX_BUFFERED_RESULTS:
                    _log.warning(
                        "буфер результатов переполнен, часть будет отправлена в complete",
                        extra={"chunk_id": chunk.chunk_id, "buffered": len(buffer)},
                    )
                    break

            if buffer:
                self._send_progress(chunk.chunk_id, next_id, buffer, counters)
                buffer = []
        finally:
            pool.shutdown(wait=True)

        summary = {
            "counters": counters.to_dict(),
            "agent_counters": self.counters.to_dict(),
            "started_at": started.isoformat(timespec="seconds"),
            "duration_seconds": round((datetime.now(tz=timezone.utc) - started).total_seconds(), 1),
            "expired": expired,
        }
        if expired:
            # блок уже мог уйти другому агенту: complete всё равно отправляем,
            # координатор сам решит, принимать ли его
            _log.warning("закрываем блок после истечения аренды", extra={"chunk_id": chunk.chunk_id})
        self.client.complete(
            self._worker_id or "", chunk.chunk_id, next_id, buffer, summary
        )
        _log.info(
            "блок закрыт",
            extra={
                "chunk_id": chunk.chunk_id,
                "next_id": next_id,
                **{f"c_{key}": value for key, value in counters.to_dict().items()},
            },
        )
        self.counters.checked += counters.checked
        self.counters.found += counters.found
        self.counters.completed += counters.completed
        self.counters.not_found += counters.not_found
        self.counters.forbidden += counters.forbidden
        self.counters.rate_limited += counters.rate_limited
        self.counters.failed += counters.failed
        self.counters.bytes_downloaded += counters.bytes_downloaded
        self._current_chunk = None
        self._cancelled_chunks.discard(chunk.chunk_id)
        self.state.clear()

    # ------------------------------------------------------------------
    def _send_progress(
        self,
        chunk_id: int,
        next_id: int,
        results: list[dict[str, Any]],
        counters: Counters,
    ) -> bool:
        if self._worker_id is None:
            return False
        try:
            self.client.progress(self._worker_id, chunk_id, next_id, results, counters.to_dict())
            return True
        except CoordinatorError as exc:
            _log.warning("прогресс не отправлен", extra={"chunk_id": chunk_id, "error": str(exc)})
            return False

    def _process_item(self, item_id: int) -> dict[str, Any]:
        check = self.adapter.check(item_id)
        if check.status == STATUS_RATE_LIMITED:
            self.limiter.on_throttled()
            retry_after = check.retry_after or 5.0
            if self._stop.wait(min(retry_after, 60.0)):
                return {"external_id": item_id, "status": "cancelled", "error_class": "shutdown"}
        else:
            self.limiter.on_success()
        status = check.status
        error_class = check.error_class
        file_info: dict[str, Any] | None = None

        if (
            check.status == STATUS_FOUND
            and check.download_url
            and self.config.site.download.policy != "never"
        ):
            with self._download_slots:
                try:
                    downloaded = self.downloader.download(
                        check.download_url,
                        item_id,
                        expected_size=check.size,
                        mime=check.mime,
                    )
                    file_info = downloaded.as_dict()
                    status = "completed"
                except (DownloadError, NetworkError, OSError) as exc:
                    error_class = getattr(exc, "error_class", "download_error")
                    status = "failed"
                    _log.warning(
                        "загрузка не удалась",
                        extra={"item_id": item_id, "error": str(exc), "class": error_class},
                    )

        result: dict[str, Any] = {
            "external_id": item_id,
            "status": status,
            "http_status": check.http_status,
            "error_class": error_class,
            "title": check.title,
            "page_url": check.page_url,
            "download_url": check.download_url,
            "file": file_info,
        }

        if check.status == STATUS_FOUND:
            metadata = check.as_metadata()
            metadata["file"] = file_info
            metadata["status"] = status
            metadata["downloaded_at"] = (
                datetime.now(tz=timezone.utc).isoformat(timespec="seconds") if file_info else None
            )
            try:
                self.storage.save_metadata(item_id, metadata)
            except OSError as exc:
                _log.warning("метаданные не сохранены", extra={"item_id": item_id, "error": str(exc)})

        return result

    # ------------------------------------------------------------------
    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(max(1.0, self._heartbeat_interval)):
            self._send_heartbeat()

    def _send_heartbeat(self) -> None:
        if self._worker_id is None:
            return
        chunk = self._current_chunk
        payload = {
            "status": "busy" if chunk is not None else "idle",
            "free_slots": 0 if chunk is not None else self.concurrency,
            "active_chunks": [chunk.chunk_id] if chunk is not None else [],
            "metrics": self.telemetry.snapshot(),
            "counters": self.counters.to_dict(),
        }
        try:
            answer = self.client.heartbeat(self._worker_id, payload)
        except CoordinatorError as exc:
            _log.warning("heartbeat не доставлен", extra={"error": str(exc)})
            return
        self._apply_instructions(answer)

    def _apply_instructions(self, answer: dict[str, Any]) -> None:
        """Указания координатора: доля общего лимита, число потоков и отмена блока."""
        share = answer.get("rps_share")
        if share:
            self.limiter.bucket.set_rate(max(0.1, float(share)))
        threads = answer.get("threads")
        if threads:
            # Пул создаётся на блок: новое значение начнёт действовать со следующего блока.
            self.concurrency = max(1, min(int(threads), self.config.limits.check_concurrency))
        self._cancelled_chunks.update(answer.get("cancel_chunks") or [])

    def _shutdown(self) -> None:
        self._stop.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=5)
        delivered = self.client.flush_outbox()
        if delivered:
            _log.info("отложенные подтверждения доставлены", extra={"count": delivered})
        self.http.close()
        _log.info("агент остановлен", extra={"counters": self.counters.to_dict()})
