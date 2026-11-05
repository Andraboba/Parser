from dataclasses import dataclass

MIN_CHUNK_SIZE = 1
MAX_CHUNK_SIZE = 10_000


@dataclass(frozen=True)
class ChunkBounds:
    """Блок диапазона идентификаторов, обе границы включительно."""

    start: int
    end: int

    @property
    def size(self) -> int:
        return self.end - self.start + 1


def split_range(range_from: int, range_to: int, chunk_size: int) -> list[ChunkBounds]:
    """Режет конечный диапазон [range_from, range_to] на блоки не больше chunk_size."""
    if range_from < 0 or range_to < range_from:
        raise ValueError(f"некорректный диапазон: {range_from}..{range_to}")
    if not MIN_CHUNK_SIZE <= chunk_size <= MAX_CHUNK_SIZE:
        raise ValueError(f"размер блока вне {MIN_CHUNK_SIZE}..{MAX_CHUNK_SIZE}: {chunk_size}")
    return [
        ChunkBounds(start, min(start + chunk_size - 1, range_to))
        for start in range(range_from, range_to + 1, chunk_size)
    ]
