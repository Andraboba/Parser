"""Перечисления статусов. Таблица переходов задания появится в тикете 05."""
from enum import StrEnum


class JobStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"


class ChunkStatus(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    DONE = "done"


class ItemStatus(StrEnum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    ERROR = "error"


class WorkerStatus(StrEnum):
    ONLINE = "online"
