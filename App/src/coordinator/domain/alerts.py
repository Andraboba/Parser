"""Правила предупреждений из ТЗ, п. 12. Чистые функции над снимком состояния."""
from dataclasses import dataclass


@dataclass(frozen=True)
class AlertLimits:
    cpu_ratio: float = 0.9
    memory_ratio: float = 0.9
    error_ratio: float = 0.3
    min_checked: int = 20
    rate_limited_count: int = 10
    stall_seconds: float = 120.0
    speed_drop_ratio: float = 0.3
    min_running_seconds: float = 60.0
    restart_count: int = 3


@dataclass(frozen=True)
class WorkerSnapshot:
    id: int
    name: str
    enabled: bool
    online: bool
    heartbeat_age_seconds: float | None
    agent_version: str
    protocol_version: int
    cpu_cores_avg: float | None = None
    cpu_limit: float | None = None
    memory_bytes: int | None = None
    memory_limit: int | None = None
    disk_free_bytes: int | None = None
    min_free_bytes: int = 0
    checked_recently: int = 0
    failed_recently: int = 0
    rate_limited_recently: int = 0
    container_state: str | None = None
    restart_count: int = 0
    oom_killed: bool = False


@dataclass(frozen=True)
class JobSnapshot:
    id: int
    running_seconds: float
    seconds_since_progress: float | None
    current_ids_per_second: float
    average_ids_per_second: float
    has_active_workers: bool


@dataclass(frozen=True)
class Alert:
    code: str
    severity: str
    message: str
    worker_id: int | None = None
    job_id: int | None = None


def worker_alerts(
    worker: WorkerSnapshot, limits: AlertLimits, heartbeat_after: float,
) -> list[Alert]:
    found: list[Alert] = []

    def add(code: str, severity: str, message: str) -> None:
        found.append(Alert(code, severity, f"{worker.name}: {message}", worker_id=worker.id))

    if not worker.enabled:
        return found
    if worker.container_state == "unreachable":
        add("worker_unavailable", "critical", "сервер недоступен по SSH")
    elif worker.container_state in ("stopped", "not_installed"):
        add("worker_unavailable", "critical", "агент на сервере не работает")
    elif worker.container_state in ("exited", "dead", "missing", "created"):
        add("worker_unavailable", "critical", f"контейнер в состоянии {worker.container_state}")
    elif not worker.online:
        add("worker_unavailable", "critical", "узел недоступен")
    if worker.heartbeat_age_seconds is not None and worker.heartbeat_age_seconds > heartbeat_after:
        add("heartbeat_missing", "critical",
            f"нет heartbeat {round(worker.heartbeat_age_seconds)} с")
    if not worker.online:
        return found
    if worker.disk_free_bytes is not None and worker.disk_free_bytes < worker.min_free_bytes:
        add("disk_low", "critical", "свободное место ниже порога")
    if (
        worker.cpu_limit and worker.cpu_cores_avg is not None
        and worker.cpu_cores_avg > worker.cpu_limit * limits.cpu_ratio
    ):
        add("cpu_high", "warning", "высокая загрузка CPU")
    if (
        worker.memory_limit and worker.memory_bytes is not None
        and worker.memory_bytes > worker.memory_limit * limits.memory_ratio
    ):
        add("memory_high", "warning", "высокая загрузка памяти")
    if (
        worker.checked_recently >= limits.min_checked
        and worker.failed_recently > worker.checked_recently * limits.error_ratio
    ):
        add("errors_rising", "warning", f"ошибок {worker.failed_recently} из "
                                        f"{worker.checked_recently} за последнюю минуту")
    if worker.rate_limited_recently >= limits.rate_limited_count:
        add("rate_limited", "warning", f"ответов 429: {worker.rate_limited_recently} за минуту")
    if worker.oom_killed:
        add("oom_killed", "critical", "контейнер убит из-за нехватки памяти")
    elif worker.restart_count >= limits.restart_count:
        add("container_restarts", "warning", f"перезапусков контейнера: {worker.restart_count}")
    return found


def version_alerts(workers: list[WorkerSnapshot], protocol_version: int) -> list[Alert]:
    """Версия не совпадает с координатором (протокол) или с большинством узлов (агент)."""
    found = []
    versions: dict[str, int] = {}
    for worker in workers:
        if worker.online:
            versions[worker.agent_version] = versions.get(worker.agent_version, 0) + 1
    usual = max(versions, key=lambda v: versions[v]) if versions else None
    for worker in workers:
        if not worker.online:
            continue
        if worker.protocol_version != protocol_version:
            found.append(Alert(
                "version_mismatch", "warning",
                f"{worker.name}: протокол {worker.protocol_version}, координатор "
                f"{protocol_version}", worker_id=worker.id,
            ))
        elif len(versions) > 1 and worker.agent_version != usual:
            found.append(Alert(
                "version_mismatch", "warning",
                f"{worker.name}: версия агента {worker.agent_version}, у большинства {usual}",
                worker_id=worker.id,
            ))
    return found


def job_alerts(job: JobSnapshot, limits: AlertLimits) -> list[Alert]:
    found = []
    stalled = (
        job.seconds_since_progress is not None
        and job.seconds_since_progress > limits.stall_seconds
    )
    if stalled and job.has_active_workers:
        found.append(Alert(
            "job_stalled", "critical",
            f"задание {job.id} не продвигается {round(job.seconds_since_progress or 0)} с",
            job_id=job.id,
        ))
    elif (
        job.running_seconds > limits.min_running_seconds and job.average_ids_per_second > 0
        and job.current_ids_per_second < job.average_ids_per_second * limits.speed_drop_ratio
    ):
        found.append(Alert(
            "speed_dropped", "warning",
            f"скорость задания {job.id} упала до {job.current_ids_per_second:.1f} ид./с "
            f"при средней {job.average_ids_per_second:.1f}", job_id=job.id,
        ))
    return found
