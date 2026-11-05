"""Балансировка: выдавать ли узлу новый блок и какую часть работы он потянет (ТЗ, п. 6.3)."""
from dataclasses import dataclass
from math import ceil


@dataclass(frozen=True)
class Thresholds:
    cpu_ratio: float = 0.95
    memory_ratio: float = 0.97
    error_ratio: float = 0.5
    # Меньше этого числа проверок за окно — доля ошибок ещё ничего не значит.
    min_checked_for_errors: int = 20
    threads_per_chunk: int = 16
    threads_per_cpu: int = 8


@dataclass(frozen=True)
class NodeState:
    enabled: bool
    free_slots: int
    threads: int
    leased_chunks: int
    cpu_limit: float | None = None
    cpu_cores_avg: float | None = None
    memory_bytes: int | None = None
    memory_limit: int | None = None
    disk_free_bytes: int | None = None
    min_free_bytes: int = 0
    checked_recently: int = 0
    failed_recently: int = 0


def effective_threads(threads: int, cpu_limit: float | None, per_cpu: int) -> float:
    """Заявленные потоки с поправкой на лимит CPU: на 0,5 CPU 16 потоков не отработают."""
    return threads if cpu_limit is None else min(threads, max(1.0, cpu_limit * per_cpu))


def chunk_quota(state: NodeState, limits: Thresholds) -> int:
    """Сколько блоков узел держит одновременно: слабый получает меньше сильного."""
    usable = effective_threads(state.threads, state.cpu_limit, limits.threads_per_cpu)
    return max(1, ceil(usable / limits.threads_per_chunk))


def refusal_reason(state: NodeState, limits: Thresholds) -> str | None:
    """Причина не выдавать блок. None — можно выдавать."""
    if not state.enabled:
        return "disabled"
    if state.free_slots < 1:
        return "no_free_slots"
    if state.leased_chunks >= chunk_quota(state, limits):
        return "chunk_quota"
    if state.disk_free_bytes is not None and state.disk_free_bytes < state.min_free_bytes:
        return "disk_low"
    if (
        state.cpu_limit and state.cpu_cores_avg is not None
        and state.cpu_cores_avg > state.cpu_limit * limits.cpu_ratio
    ):
        return "cpu_overload"
    if (
        state.memory_limit and state.memory_bytes is not None
        and state.memory_bytes > state.memory_limit * limits.memory_ratio
    ):
        return "memory_overload"
    if (
        state.checked_recently >= limits.min_checked_for_errors
        and state.failed_recently > state.checked_recently * limits.error_ratio
    ):
        return "too_many_errors"
    return None
