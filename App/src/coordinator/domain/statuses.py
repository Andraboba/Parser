"""Перечисления статусов и таблица переходов задания (ТЗ, п. 7 и 19)."""
from enum import StrEnum


class JobStatus(StrEnum):
    CREATED = "created"
    VALIDATING = "validating"
    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    COMPLETED = "completed"
    FAILED = "failed"


class JobAction(StrEnum):
    START = "start"
    VALIDATED = "validated"
    PAUSE = "pause"
    RESUME = "resume"
    STOP = "stop"
    DRAINED = "drained"
    FINISH = "finish"
    FAIL = "fail"
    RESTART = "restart"


_S = JobStatus
_A = JobAction

# Всё, чего нет в таблице, запрещено. Завершённое задание возвращается в работу
# только явным перезапуском (RESTART), обычный запуск или возобновление отклоняются.
JOB_TRANSITIONS: dict[tuple[JobStatus, JobAction], JobStatus] = {
    (_S.CREATED, _A.START): _S.VALIDATING,
    (_S.VALIDATING, _A.VALIDATED): _S.RUNNING,
    (_S.VALIDATING, _A.FAIL): _S.FAILED,
    (_S.RUNNING, _A.PAUSE): _S.PAUSING,
    (_S.RUNNING, _A.STOP): _S.STOPPING,
    (_S.RUNNING, _A.FINISH): _S.COMPLETED,
    (_S.RUNNING, _A.FAIL): _S.FAILED,
    (_S.PAUSING, _A.DRAINED): _S.PAUSED,
    (_S.PAUSING, _A.RESUME): _S.RUNNING,
    (_S.PAUSING, _A.STOP): _S.STOPPING,
    (_S.PAUSING, _A.FINISH): _S.COMPLETED,
    (_S.PAUSING, _A.FAIL): _S.FAILED,
    (_S.PAUSED, _A.RESUME): _S.RUNNING,
    (_S.PAUSED, _A.STOP): _S.STOPPING,
    (_S.STOPPING, _A.DRAINED): _S.STOPPED,
    (_S.STOPPED, _A.RESTART): _S.RUNNING,
    (_S.COMPLETED, _A.RESTART): _S.RUNNING,
    (_S.FAILED, _A.RESTART): _S.RUNNING,
}


def next_status(current: str, action: JobAction) -> JobStatus | None:
    """Новый статус задания или None, если такой переход запрещён."""
    return JOB_TRANSITIONS.get((JobStatus(current), action))


class ChunkStatus(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    DONE = "done"
    FAILED = "failed"


class ItemStatus(StrEnum):
    """Статусы обработки идентификатора (ТЗ, п. 7)."""

    PENDING = "pending"
    CHECKING = "checking"
    NOT_FOUND = "not_found"
    FOUND = "found"
    DOWNLOAD_QUEUED = "download_queued"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FORBIDDEN = "forbidden"
    RATE_LIMITED = "rate_limited"
    RETRY_WAIT = "retry_wait"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkerStatus(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"
