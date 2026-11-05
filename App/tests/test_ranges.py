import pytest

from coordinator.domain.chunks import ChunkBounds, chunks_needed, next_batch
from coordinator.domain.ranges import MAX_ID, RangeSpec, from_bounds, parse_range


@pytest.mark.parametrize(("text", "expected"), [
    ("[5,10]", RangeSpec(5, 10)),
    ("[0,0]", RangeSpec(0, 0)),
    (" [ 5 , 10 ] ", RangeSpec(5, 10)),
    ("100", RangeSpec(0, 100)),
    ("0", RangeSpec(0, 0)),
    ("[7,]", RangeSpec(7, None)),
    ("[7, ]", RangeSpec(7, None)),
    ("", RangeSpec(0, None)),
    ("   ", RangeSpec(0, None)),
    (None, RangeSpec(0, None)),
    (f"[0,{MAX_ID}]", RangeSpec(0, MAX_ID)),
])
def test_parse_valid_ranges(text: str | None, expected: RangeSpec) -> None:
    assert parse_range(text) == expected


@pytest.mark.parametrize("text", [
    "[10,5]", "-1", "[-1,5]", "[1,-5]", "abc", "[,5]", "[,]", "[1,2", "1,2", "[1,2,3]",
    "1.5", "٣", f"{MAX_ID + 1}", f"[0,{MAX_ID + 1}]", f"[{MAX_ID + 1},]",
])
def test_parse_rejects_invalid_ranges(text: str) -> None:
    with pytest.raises(ValueError):
        parse_range(text)


@pytest.mark.parametrize(("start", "end", "expected"), [
    (5, 10, RangeSpec(5, 10)),
    (None, 10, RangeSpec(0, 10)),
    (5, None, RangeSpec(5, None)),
    (None, None, RangeSpec(0, None)),
])
def test_from_bounds_matches_command_line_semantics(
    start: int | None, end: int | None, expected: RangeSpec,
) -> None:
    assert from_bounds(start, end) == expected


def test_total_of_open_range_is_unknown() -> None:
    assert RangeSpec(5, None).total is None
    assert RangeSpec(5, 10).total == 6


def test_next_batch_of_finite_range_stops_at_end() -> None:
    assert next_batch(1, 25, 10, 2) == [ChunkBounds(1, 10), ChunkBounds(11, 20)]
    assert next_batch(21, 25, 10, 2) == [ChunkBounds(21, 25)]
    assert next_batch(26, 25, 10, 2) == []


def test_next_batch_of_open_range_never_ends() -> None:
    assert next_batch(10**12, None, 5, 2) == [
        ChunkBounds(10**12, 10**12 + 4), ChunkBounds(10**12 + 5, 10**12 + 9),
    ]


def test_batches_cover_range_without_gaps_or_overlaps() -> None:
    cursor, seen = 3, []
    while batch := next_batch(cursor, 1_000, 7, 11):
        seen.extend(batch)
        cursor = batch[-1].end + 1
    assert seen[0].start == 3
    assert seen[-1].end == 1_000
    assert all(b.start == a.end + 1 for a, b in zip(seen, seen[1:], strict=False))


@pytest.mark.parametrize(("total", "size", "expected"), [(25, 10, 3), (30, 10, 3), (1, 10, 1)])
def test_chunks_needed(total: int, size: int, expected: int) -> None:
    assert chunks_needed(total, size) == expected
