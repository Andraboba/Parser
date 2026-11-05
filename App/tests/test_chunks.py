import pytest

from coordinator.domain.chunks import MAX_CHUNK_SIZE, ChunkBounds, split_range


def test_splits_with_short_last_chunk() -> None:
    assert split_range(10, 34, 10) == [
        ChunkBounds(10, 19), ChunkBounds(20, 29), ChunkBounds(30, 34),
    ]


def test_chunks_cover_range_without_gaps_or_overlaps() -> None:
    chunks = split_range(5, 1_004, 7)
    assert chunks[0].start == 5
    assert chunks[-1].end == 1_004
    assert all(b.start == a.end + 1 for a, b in zip(chunks, chunks[1:], strict=False))
    assert sum(c.size for c in chunks) == 1_000


def test_single_id_range() -> None:
    assert split_range(7, 7, 100) == [ChunkBounds(7, 7)]


def test_chunk_larger_than_range() -> None:
    assert split_range(0, 9, MAX_CHUNK_SIZE) == [ChunkBounds(0, 9)]


@pytest.mark.parametrize(
    ("range_from", "range_to", "chunk_size"),
    [(-1, 5, 1), (5, 4, 1), (0, 5, 0), (0, 5, MAX_CHUNK_SIZE + 1)],
)
def test_rejects_invalid_arguments(range_from: int, range_to: int, chunk_size: int) -> None:
    with pytest.raises(ValueError):
        split_range(range_from, range_to, chunk_size)
