"""Структурные JSON-журналы с полями из ТЗ, п. 15. Секреты маскируются при записи."""
import json
import logging
from datetime import UTC, datetime

from coordinator.domain.security import mask_secrets

_FIELDS = (
    "job_id", "worker_id", "chunk_id", "external_id", "operation", "result", "error_code",
)


class JsonFormatter(logging.Formatter):
    def __init__(self, component: str) -> None:
        super().__init__()
        self.component = component

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "component": self.component,
            "logger": record.name,
            "message": mask_secrets(record.getMessage()),
        }
        for field in _FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                entry[field] = value
        if record.exc_info:
            entry["exception"] = mask_secrets(self.formatException(record.exc_info))
        return json.dumps(entry, ensure_ascii=False, default=str)


def setup_logging(component: str, level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter(component))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Журнал доступа uvicorn идёт через тот же формат, без своих обработчиков.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
