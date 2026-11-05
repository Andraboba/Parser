"""Эталонная заглушка агента: проходит полный контракт без реального сайта (Э7 плана).

Только стандартная библиотека. Настройки берутся из окружения (см. main).

Режимы STUB_MODE:
  ideal  — идеальный узел;
  slow   — медленный узел, в десять раз дольше на элемент;
  errors — часть элементов с ошибками, первый отказ каждого третьего блока;
  crash  — аварийное завершение после STUB_CRASH_AFTER идентификаторов.

Управление на ходу: JSON в файле STUB_CONTROL_FILE, например
  {"disk_free_bytes": 1000} — нехватка места, {"cpu_cores": 4} — перегрузка,
  {"rate_limited": true} — внешний сервис отвечает 429,
  {"crash": true} — аварийное завершение. Именно падение изнутри: docker kill Docker считает
  ручной остановкой, и политика перезапуска такой контейнер не поднимает,
  {"allocate_mb": 800} — выделить память, чтобы контейнер убило по лимиту (OOM).
"""
import hashlib
import json
import logging
import os
import shutil
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger("agent_stub")

AGENT_VERSION = "stub-0.2"
PROTOCOL_VERSION = 1
MAX_BACKOFF_S = 30.0
PROGRESS_BATCH = 100
CRASH_EXIT_CODE = 137
DELIVERY_ATTEMPTS = 8
# Пауза на пакет ограничена: новая доля лимита должна применяться быстро.
MAX_PACE_S = 5.0


class ApiError(Exception):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status


def cgroup_limits(root: Path = Path("/sys/fs/cgroup")) -> dict[str, float | int | None]:
    """Лимиты контейнера из cgroup v2. Вне контейнера и без лимита — None."""
    limits: dict[str, float | int | None] = {"cpus": None, "memory_mb": None}
    try:
        quota, period = (root / "cpu.max").read_text().split()
        if quota != "max":
            limits["cpus"] = int(quota) / int(period)
        memory = (root / "memory.max").read_text().strip()
        if memory != "max":
            limits["memory_mb"] = int(memory) // 1024**2
    except (OSError, ValueError):
        pass
    return limits


def results_for(next_id: int, range_end: int) -> list[dict[str, Any]]:
    return [
        {"external_id": i, "status": "found" if i % 7 == 0 else "not_found"}
        for i in range(next_id, range_end + 1)
    ]


def read_control(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


class StubAgent:
    def __init__(
        self, base_url: str, name: str, threads: int, token: str, *, mode: str = "ideal",
        item_delay: float = 0.0, timeout: float = 30.0, storage_path: str | None = None,
        file_size: int = 0, crash_after: int | None = None, control_file: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.name = name
        self.threads = threads
        self.token = token
        self.mode = mode
        self.item_delay = item_delay * (10 if mode == "slow" else 1)
        self.timeout = timeout
        self.storage_path = storage_path
        self.file_size = file_size
        self.crash_after = crash_after
        self.control_file = control_file
        self.worker_id: int | None = None
        self.heartbeat_interval = 5.0
        self.rps_share: float | None = None
        self.processed = 0
        self.counters = {"checked": 0, "found": 0, "failed": 0, "rate_limited": 0, "http_429": 0}
        self.started = time.monotonic()
        self._cancelled: set[int] = set()
        self._active: int | None = None
        self._failed_once: set[int] = set()
        self._cpu_sample: tuple[float, int] | None = None
        self._ballast = bytearray()

    def _post(
        self, path: str, payload: dict[str, Any], key: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        headers = {
            "Content-Type": "application/json",
            "X-Agent-Token": self.token,
            "X-Protocol-Version": str(PROTOCOL_VERSION),
        }
        if key:
            headers["Idempotency-Key"] = key
        request = urllib.request.Request(
            self.base_url + path, data=json.dumps(payload).encode(), method="POST", headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
                return response.status, json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            raise ApiError(exc.code, exc.read().decode(errors="replace")) from exc

    def _deliver(
        self, path: str, payload: dict[str, Any], key: str | None = None,
    ) -> dict[str, Any]:
        """Результаты нельзя потерять: сбой сети и 5xx повторяются. Кроме них — ошибка клиента."""
        delay = 1.0
        for attempt in range(DELIVERY_ATTEMPTS):
            try:
                return self._post(path, payload, key)[1]
            except ApiError as exc:
                if exc.status < 500 and exc.status != 429:
                    raise
                failure: Exception = exc
            except OSError as exc:
                failure = exc
            log.warning("доставка не удалась (%s): %s", attempt + 1, failure)
            time.sleep(delay)
            delay = min(delay * 2, MAX_BACKOFF_S)
        raise failure

    def register(self) -> None:
        _, data = self._post("/api/workers/register", {
            "name": self.name,
            "provisioning_mode": "docker",
            "container_id": os.environ.get("HOSTNAME"),
            "image": os.environ.get("AGENT_IMAGE"),
            "agent_version": AGENT_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "threads": self.threads,
            "limits": cgroup_limits(),
            "storage_path": self.storage_path,
        })
        self.worker_id = data["worker_id"]
        self.heartbeat_interval = float(data["heartbeat_interval"])
        log.info("зарегистрирован как узел %s", self.worker_id)

    def metrics(self) -> dict[str, Any]:
        snapshot: dict[str, Any] = {
            "uptime_seconds": round(time.monotonic() - self.started, 1),
            "active_checks": self.threads if self._active is not None else 0,
            "active_downloads": 0,
        }
        limits = cgroup_limits()
        if limits["cpus"] is not None:
            snapshot["cpu_limit"] = limits["cpus"]
        if limits["memory_mb"] is not None:
            snapshot["memory_limit"] = int(limits["memory_mb"]) * 1024**2
        root = Path("/sys/fs/cgroup")
        try:
            snapshot["memory_bytes"] = int((root / "memory.current").read_text())
            usage = next(
                int(line.split()[1])
                for line in (root / "cpu.stat").read_text().splitlines()
                if line.startswith("usage_usec")
            )
            now = time.monotonic()
            if self._cpu_sample is not None:
                elapsed = max(1e-6, now - self._cpu_sample[0])
                snapshot["cpu_cores"] = round((usage - self._cpu_sample[1]) / 1e6 / elapsed, 3)
            self._cpu_sample = (now, usage)
        except (OSError, ValueError, StopIteration):
            pass
        try:
            disk = shutil.disk_usage(self.storage_path or "/")
            snapshot["disk_total_bytes"] = disk.total
            snapshot["disk_free_bytes"] = disk.free
        except OSError:
            pass
        control = read_control(self.control_file)
        snapshot.update({k: v for k, v in control.items() if k != "rate_limited"})
        return snapshot

    def heartbeat(self) -> dict[str, Any]:
        active = self._active
        _, data = self._post(f"/api/workers/{self.worker_id}/heartbeat", {
            "status": "busy" if active is not None else "idle",
            "free_slots": 0 if active is not None else self.threads,
            "active_chunks": [active] if active is not None else [],
            "metrics": self.metrics(),
            "counters": self.counters,
        })
        self._cancelled.update(data.get("cancel_chunks") or [])
        if data.get("threads"):
            self.threads = int(data["threads"])
        if data.get("rps_share") is not None:
            self.rps_share = float(data["rps_share"])
        return data

    def _write_file(self, item_id: int) -> dict[str, Any] | None:
        if not self.file_size or not self.storage_path:
            return None
        directory = Path(self.storage_path) / str(item_id)[:3] / str(item_id)
        target = directory / "video.mp4"
        # Повторная обработка не создаёт второй файл (ТЗ, п. 21).
        if not target.exists():
            directory.mkdir(parents=True, exist_ok=True)
            part = directory / "video.mp4.part"
            part.write_bytes(b"\0" * self.file_size)
            part.replace(target)
        data = target.read_bytes()
        return {
            "path": str(target), "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "mime": "video/mp4",
        }

    def _result(self, item_id: int, rate_limited: bool) -> dict[str, Any]:
        self.counters["checked"] += 1
        if rate_limited:
            self.counters["rate_limited"] += 1
            self.counters["http_429"] += 1
            return {"external_id": item_id, "status": "rate_limited", "http_status": 429,
                    "error_class": "rate_limited"}
        if self.mode == "errors" and item_id % 5 == 0:
            self.counters["failed"] += 1
            return {"external_id": item_id, "status": "failed", "http_status": 500,
                    "error_class": "server_error"}
        if item_id % 7 != 0:
            return {"external_id": item_id, "status": "not_found", "http_status": 404}
        self.counters["found"] += 1
        try:
            file_info = self._write_file(item_id)
        except OSError as exc:
            # Диск заполнен: материал найден, но не сохранён, повтор решит оператор.
            log.warning("файл %s не записан: %s", item_id, exc)
            self.counters["failed"] += 1
            return {"external_id": item_id, "status": "failed", "http_status": 200,
                    "error_class": "insufficient_space"}
        return {
            "external_id": item_id, "status": "completed" if file_info else "found",
            "http_status": 200, "title": f"Материал {item_id}",
            "page_url": f"http://stub.invalid/{item_id}",
            "download_url": f"http://stub.invalid/{item_id}/video.mp4", "file": file_info,
        }

    def _pace(self, count: int) -> None:
        delay = count * self.item_delay / self.threads
        if self.rps_share:
            delay = max(delay, count / self.rps_share)
        if delay:
            time.sleep(min(delay, MAX_PACE_S))

    def _crash_if_due(self, count: int) -> None:
        self.processed += count
        due = (
            self.mode == "crash" and self.crash_after is not None
            and self.processed >= self.crash_after
        )
        control = read_control(self.control_file)
        if control.get("allocate_mb"):
            # Настоящий OOM-kill: ядро убьёт процесс при достижении лимита контейнера.
            if self.control_file:
                Path(self.control_file).unlink(missing_ok=True)
            log.warning("выделяю память: %s МБ", control["allocate_mb"])
            self._ballast = bytearray(int(control["allocate_mb"]) * 1024**2)
            for position in range(0, len(self._ballast), 4096):
                self._ballast[position] = 1
        if due or control.get("crash"):
            log.warning("аварийное завершение по сценарию")
            # Иначе после перезапуска контейнера файл снова уронит агента.
            if self.control_file:
                Path(self.control_file).unlink(missing_ok=True)
            os._exit(CRASH_EXIT_CODE)

    def _process(self, chunk: dict[str, Any]) -> None:
        chunk_id, end = chunk["chunk_id"], chunk["range_end"]
        worker = f"/api/workers/{self.worker_id}"
        next_id = chunk["next_id"]
        if self.mode == "errors" and chunk_id % 3 == 0 and chunk_id not in self._failed_once:
            self._failed_once.add(chunk_id)
            self._deliver(f"{worker}/fail", {
                "chunk_id": chunk_id, "error_class": "network", "message": "сценарий errors",
                "next_id": next_id,
            }, key=f"fail-{chunk_id}")
            return
        while True:
            upto = min(next_id + PROGRESS_BATCH - 1, end)
            rate_limited = bool(read_control(self.control_file).get("rate_limited"))
            batch = [self._result(i, rate_limited) for i in range(next_id, upto + 1)]
            self._pace(len(batch))
            self._crash_if_due(len(batch))
            next_id = upto + 1
            if next_id > end:
                break
            outcome = self._deliver(f"{worker}/progress", {
                "chunk_id": chunk_id, "next_id": next_id, "results": batch,
                "counters": self.counters,
            })
            if outcome.get("cancel") or chunk_id in self._cancelled:
                batch = []
                log.info("блок %s отменён на %s", chunk_id, next_id)
                break
        self._deliver(f"{worker}/complete", {
            "chunk_id": chunk_id, "next_id": next_id, "results": batch,
        }, key=f"complete-{chunk_id}-{next_id}")
        log.info("блок %s закрыт на %s из %s", chunk_id, next_id, end + 1)

    def step(self) -> bool:
        """Берёт один блок и обрабатывает его. False — работы нет."""
        status, chunk = self._post(
            f"/api/workers/{self.worker_id}/lease", {"free_slots": self.threads},
        )
        if status == 204:
            return False
        if chunk.get("rps_share"):
            self.rps_share = float(chunk["rps_share"])
        self._active = chunk["chunk_id"]
        try:
            self._process(chunk)
        except ApiError as exc:
            # Аренду забрали: блок уже у другого узла, бросаем его и берём следующий.
            if exc.status != 409:
                raise
            log.warning("блок %s потерян: %s", chunk["chunk_id"], exc)
        finally:
            self._active = None
            self._cancelled.discard(chunk["chunk_id"])
        return True


def heartbeat_loop(agent: StubAgent, stop: threading.Event) -> None:
    while not stop.wait(agent.heartbeat_interval):
        agent._crash_if_due(0)
        if agent.worker_id is None:
            continue
        try:
            agent.heartbeat()
        except ApiError as exc:
            log.warning("heartbeat отклонён: %s", exc)
            if exc.status == 404:
                agent.worker_id = None
        except OSError as exc:
            log.warning("heartbeat не доставлен: %s", exc)


def run(agent: StubAgent, idle_pause: float) -> None:
    backoff = 1.0
    stop = threading.Event()
    threading.Thread(target=heartbeat_loop, args=(agent, stop), daemon=True).start()
    while True:
        try:
            if agent.worker_id is None:
                agent.register()
            if not agent.step():
                time.sleep(idle_pause)
            backoff = 1.0
        except (ApiError, OSError) as exc:
            log.warning("ошибка обмена с координатором: %s; пауза %.0f с", exc, backoff)
            # Узел могли удалить из базы, повторная регистрация вернёт актуальный id.
            if isinstance(exc, ApiError) and exc.status == 404:
                agent.worker_id = None
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_S)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Процесс с PID 1 без обработчика игнорирует SIGTERM, и docker stop ждал бы таймаут.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    crash_after = os.environ.get("STUB_CRASH_AFTER")
    run(
        StubAgent(
            base_url=os.environ.get("COORD_URL", "http://api:8000"),
            name=os.environ["WORKER_NAME"],
            threads=int(os.environ.get("WORKER_THREADS", "1")),
            # Файл секрета предпочтительнее: окружение контейнера видно через docker inspect.
            token=(
                Path(os.environ["AGENT_TOKEN_FILE"]).read_text().strip()
                if "AGENT_TOKEN_FILE" in os.environ else os.environ["AGENT_TOKEN"]
            ),
            mode=os.environ.get("STUB_MODE", "ideal"),
            # Секунд на один идентификатор в одном потоке, чтобы прогресс был заметен глазу.
            item_delay=float(os.environ.get("STUB_ITEM_DELAY", "0.01")),
            storage_path=os.environ.get("STUB_STORAGE_PATH", "/data/videos"),
            file_size=int(os.environ.get("STUB_FILE_SIZE", "4096")),
            crash_after=int(crash_after) if crash_after else None,
            control_file=os.environ.get("STUB_CONTROL_FILE", "/tmp/stub-control.json"),
        ),
        idle_pause=float(os.environ.get("STUB_IDLE_PAUSE", "2")),
    )


if __name__ == "__main__":
    main()
