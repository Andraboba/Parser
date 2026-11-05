"""Расчёт прогресса, скорости и оставшегося времени (ТЗ, п. 11.1 и 20)."""
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Sample:
    at: datetime
    processed_ids: int
    bytes_done: int


@dataclass(frozen=True)
class Rates:
    ids_per_second: float
    bytes_per_second: float


def current_rates(samples: Sequence[Sample], window_seconds: float) -> Rates:
    """Скорость за последнее окно. Снимки идут по времени, новые в конце."""
    if len(samples) < 2:
        return Rates(0.0, 0.0)
    last = samples[-1]
    first = next(
        (s for s in samples if (last.at - s.at).total_seconds() <= window_seconds), samples[0],
    )
    elapsed = (last.at - first.at).total_seconds()
    if elapsed <= 0:
        return Rates(0.0, 0.0)
    return Rates(
        max(0, last.processed_ids - first.processed_ids) / elapsed,
        max(0, last.bytes_done - first.bytes_done) / elapsed,
    )


def average_rate(amount: int, elapsed_seconds: float) -> float:
    return amount / elapsed_seconds if elapsed_seconds > 0 else 0.0


def eta_seconds(total_ids: int | None, processed_ids: int, ids_per_second: float) -> float | None:
    """Оставшееся время. None — неопределённо: диапазон без конца или работа стоит."""
    if total_ids is None or ids_per_second <= 0:
        return None
    return max(0, total_ids - processed_ids) / ids_per_second
