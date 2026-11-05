"""Формат диапазона идентификаторов (ТЗ, п. 3)."""
import re
from dataclasses import dataclass

# На единицу меньше BIGINT: контрольная точка закрытого блока равна range_end + 1.
MAX_ID = 2**63 - 2

_BRACKETS = re.compile(r"\[\s*([0-9]+)\s*,\s*([0-9]*)\s*\]")


@dataclass(frozen=True)
class RangeSpec:
    """Диапазон с границами включительно. end=None — верхней границы нет."""

    start: int
    end: int | None

    def __post_init__(self) -> None:
        if self.start < 0:
            raise ValueError("начало диапазона не может быть отрицательным")
        if self.start > MAX_ID or (self.end is not None and self.end > MAX_ID):
            raise ValueError(f"граница диапазона больше {MAX_ID}")
        if self.end is not None and self.end < self.start:
            raise ValueError(f"конец диапазона меньше начала: {self.start} > {self.end}")

    @property
    def total(self) -> int | None:
        return None if self.end is None else self.end - self.start + 1


def parse_range(text: str | None) -> RangeSpec:
    """Разбирает `[A,B]`, `A`, `[A,]` и пустое значение."""
    value = (text or "").strip()
    if not value:
        return RangeSpec(0, None)
    if re.fullmatch(r"[0-9]+", value):
        return RangeSpec(0, int(value))
    match = _BRACKETS.fullmatch(value)
    if match is None:
        raise ValueError(f"диапазон «{value}» не в формате [A,B], A, [A,] или пусто")
    start, end = match.groups()
    return RangeSpec(int(start), int(end) if end else None)


def from_bounds(range_from: int | None, range_to: int | None) -> RangeSpec:
    """Эквивалент --from A --to B: без начала — от 0, без конца — открытый диапазон."""
    return RangeSpec(range_from or 0, range_to)
