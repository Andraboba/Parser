"""Приём результатов и файлов: повтор не создаёт дубликатов (ТЗ, п. 6.2, 14, 21)."""
import csv
import io
from typing import Any

import pytest
from fastapi.testclient import TestClient

from coordinator.api.jobs import csv_cell
from coordinator.domain.paths import is_inside
from tests.helpers import complete, job_of, must_lease, progress, register, sql, start_job


@pytest.mark.parametrize(("root", "path", "expected"), [
    ("/data/videos", "/data/videos/1/1/video.mp4", True),
    ("/data/videos/", "/data/videos/video.mp4", True),
    ("/data/videos", "/data/videos", False),
    ("/data/videos", "/data/videos/../secret", False),
    ("/data/videos", "/data/videos/1/../../etc/passwd", False),
    ("/data/videos", "/data/videos-evil/x", False),
    ("/data/videos", "/etc/passwd", False),
    ("/data/videos", "relative/path", False),
    ("/data/videos", "", False),
    ("/data/videos", "/data/videos/a\x00b", False),
    ("/data/videos", "/data/videos/a\\..\\b", False),
    (None, "/data/videos/x", False),
    ("relative", "relative/x", False),
])
def test_path_must_stay_inside_storage(root: str | None, path: str, expected: bool) -> None:
    assert is_inside(root, path) is expected


@pytest.mark.parametrize(("value", "expected"), [
    ("=HYPERLINK(1)", "'=HYPERLINK(1)"), ("+1", "'+1"), ("-1", "'-1"), ("@x", "'@x"),
    ("обычное", "обычное"), (None, ""), (5, 5),
])
def test_csv_cell_neutralizes_formulas(value: object, expected: object) -> None:
    assert csv_cell(value) == expected


FOUND = {
    "external_id": 3, "status": "completed", "http_status": 200, "title": "Видео",
    "page_url": "http://site/3", "download_url": "http://site/3/v.mp4",
    "file": {"path": "/data/videos/3/3/video.mp4", "size": 100, "sha256": "abc",
             "mime": "video/mp4", "resumed": True, "skipped": False},
}


def counts() -> tuple[int, int, int, int]:
    row = sql(
        "SELECT (SELECT count(*) FROM items), (SELECT count(*) FROM media_metadata), "
        "(SELECT count(*) FROM downloads), (SELECT count(*) FROM files)"
    )[0]
    return tuple(row)


def items_of(client: TestClient, job_id: int, **params: Any) -> list[dict[str, Any]]:
    return client.get(f"/api/jobs/{job_id}/items", params=params).json()["items"]


@pytest.mark.integration
class TestResults:
    def test_metadata_and_file_are_saved(self, client: TestClient) -> None:
        job_id = start_job(client)
        worker = register(client)
        chunk = must_lease(client, worker)
        assert complete(client, worker, chunk, results=[FOUND]).json()["rejected"] == []

        item = items_of(client, job_id)[0]
        assert item["status"] == "completed"
        assert item["title"] == "Видео"
        assert item["worker"] == "worker-01"
        assert item["checksum"] == "abc"
        assert item["file_path"] == "/data/videos/3/3/video.mp4"
        assert item["file_size"] == 100

    def test_repeated_results_create_no_duplicates(self, client: TestClient) -> None:
        start_job(client)
        worker = register(client)
        chunk = must_lease(client, worker)
        progress(client, worker, chunk, 4, [FOUND])
        progress(client, worker, chunk, 4, [FOUND])
        complete(client, worker, chunk, results=[FOUND])
        complete(client, worker, chunk, results=[FOUND])
        assert counts() == (1, 1, 1, 1)

    def test_attempts_count_repeated_processing(self, client: TestClient) -> None:
        job_id = start_job(client)
        worker = register(client)
        chunk = must_lease(client, worker)
        progress(client, worker, chunk, 4, [FOUND])
        progress(client, worker, chunk, 4, [FOUND])
        assert items_of(client, job_id)[0]["attempts"] == 2

    def test_downloaded_item_is_not_downgraded(self, client: TestClient) -> None:
        job_id = start_job(client)
        worker = register(client)
        chunk = must_lease(client, worker)
        progress(client, worker, chunk, 4, [FOUND])
        progress(client, worker, chunk, 4, [
            {"external_id": 3, "status": "not_found", "http_status": 404},
        ])
        assert items_of(client, job_id)[0]["status"] == "completed"

    @pytest.mark.parametrize("path", [
        "/etc/passwd", "/data/videos/../etc/passwd", "relative/path", "/data/videos-evil/x",
    ])
    def test_file_outside_storage_is_rejected(self, client: TestClient, path: str) -> None:
        job_id = start_job(client)
        worker = register(client)
        chunk = must_lease(client, worker)
        answer = complete(client, worker, chunk, results=[
            {**FOUND, "file": {**FOUND["file"], "path": path}},
            {"external_id": 4, "status": "not_found"},
        ]).json()
        assert answer["rejected"] == [3]

        items = {i["external_id"]: i for i in items_of(client, job_id)}
        assert items[3]["status"] == "failed"
        assert items[3]["error_class"] == "invalid_path"
        assert items[3]["error_group"] == "security"
        assert items[3]["file_path"] is None
        assert items[4]["status"] == "not_found", "остальные результаты не теряются"
        assert counts()[3] == 0

    def test_file_is_rejected_for_worker_without_storage(self, client: TestClient) -> None:
        start_job(client)
        worker = register(client, storage_path=None)
        chunk = must_lease(client, worker)
        assert complete(client, worker, chunk, results=[FOUND]).json()["rejected"] == [3]

    def test_text_from_site_with_nul_and_huge_title_is_accepted(self, client: TestClient) -> None:
        job_id = start_job(client)
        worker = register(client)
        chunk = must_lease(client, worker)
        resp = complete(client, worker, chunk, results=[
            {"external_id": 1, "status": "found", "title": "a\x00b" + "x" * 5000,
             "error_class": "e" * 200},
        ])
        assert resp.status_code == 200, resp.text
        item = items_of(client, job_id)[0]
        assert item["title"].startswith("abx")
        assert len(item["title"]) == 2000
        assert len(item["error_class"]) == 64

    def test_status_outside_contract_returns_422(self, client: TestClient) -> None:
        start_job(client)
        worker = register(client)
        chunk = must_lease(client, worker)
        resp = complete(client, worker, chunk, results=[{"external_id": 1, "status": "nope"}])
        assert resp.status_code == 422

    def test_every_status_from_requirements_is_accepted(self, client: TestClient) -> None:
        statuses = [
            "pending", "checking", "not_found", "found", "download_queued", "downloading",
            "completed", "skipped", "forbidden", "rate_limited", "retry_wait", "failed",
            "cancelled",
        ]
        job_id = start_job(client, range=f"[1,{len(statuses)}]", chunk_size=100)
        worker = register(client)
        chunk = must_lease(client, worker)
        complete(client, worker, chunk, results=[
            {"external_id": i, "status": s} for i, s in enumerate(statuses, start=1)
        ])
        assert all(job_of(client, job_id)["items"][s] == 1 for s in statuses)

    def test_full_chunk_of_ten_thousand_results_fits_one_request(self, client: TestClient) -> None:
        job_id = start_job(client, range="[1,10000]", chunk_size=10000)
        worker = register(client)
        chunk = must_lease(client, worker)
        results = [{"external_id": i, "status": "found", "title": f"t{i}"}
                   for i in range(1, 10001)]
        assert complete(client, worker, chunk, results=results).status_code == 200
        assert job_of(client, job_id)["items"]["found"] == 10000


@pytest.mark.integration
class TestIdempotencyKey:
    def test_same_key_has_no_second_effect(self, client: TestClient) -> None:
        job_id = start_job(client)
        worker = register(client)
        chunk = must_lease(client, worker)
        first = complete(client, worker, chunk, next_id=4, results=[FOUND], key="k1")
        assert first.status_code == 200
        replay = complete(client, worker, chunk, next_id=4, results=[
            {"external_id": 3, "status": "failed"},
        ], key="k1")
        assert replay.status_code == 200
        assert items_of(client, job_id)[0]["status"] == "completed"

    def test_same_key_on_new_attempt_is_a_new_request(self, client: TestClient) -> None:
        job_id = start_job(client)
        worker = register(client)
        chunk = must_lease(client, worker)
        complete(client, worker, chunk, next_id=4, key="k1")
        must_lease(client, worker)
        assert complete(client, worker, chunk, key="k1").status_code == 200
        assert job_of(client, job_id)["status"] == "completed"

    def test_key_is_per_worker(self, client: TestClient) -> None:
        start_job(client, range="[1,2]", chunk_size=1)
        first = register(client, "first")
        second = register(client, "second")
        chunk_a = must_lease(client, first)
        chunk_b = must_lease(client, second)
        assert complete(client, first, chunk_a, key="same").status_code == 200
        assert complete(client, second, chunk_b, key="same").status_code == 200
        assert sql("SELECT count(*) FROM idempotency_keys") == [(2,)]


@pytest.mark.integration
class TestItemsApi:
    @pytest.fixture
    def job_id(self, client: TestClient) -> int:
        job_id = start_job(client, range="[1,6]")
        first = register(client, "first")
        chunk = must_lease(client, first)
        complete(client, first, chunk, results=[
            {"external_id": 1, "status": "not_found"},
            {"external_id": 2, "status": "found", "title": "Кошки и собаки"},
            {"external_id": 3, "status": "found", "title": "=Формула"},
            {"external_id": 4, "status": "failed", "error_class": "server_error"},
            {"external_id": 5, "status": "failed", "error_class": "http_503"},
            {"external_id": 6, "status": "forbidden", "http_status": 403},
        ])
        return job_id

    def test_pagination_by_id(self, client: TestClient, job_id: int) -> None:
        first = client.get(f"/api/jobs/{job_id}/items", params={"limit": 4}).json()
        assert [i["external_id"] for i in first["items"]] == [1, 2, 3, 4]
        assert first["next_after_id"] == 4
        rest = client.get(f"/api/jobs/{job_id}/items", params={"limit": 4, "after_id": 4}).json()
        assert [i["external_id"] for i in rest["items"]] == [5, 6]
        assert rest["next_after_id"] is None

    def test_filter_by_status_worker_and_text(self, client: TestClient, job_id: int) -> None:
        assert len(items_of(client, job_id, status="failed")) == 2
        assert len(items_of(client, job_id, worker="first")) == 6
        assert items_of(client, job_id, worker="ghost") == []
        assert [i["external_id"] for i in items_of(client, job_id, q="кошки")] == [2]
        assert [i["external_id"] for i in items_of(client, job_id, q="5")] == [5]

    def test_search_treats_wildcards_literally(self, client: TestClient, job_id: int) -> None:
        assert items_of(client, job_id, q="%") == []

    def test_invalid_status_filter_returns_422(self, client: TestClient, job_id: int) -> None:
        assert client.get(f"/api/jobs/{job_id}/items", params={"status": "x"}).status_code == 422

    def test_items_of_unknown_job_return_404(self, client: TestClient) -> None:
        assert client.get("/api/jobs/999/items").status_code == 404
        assert client.get("/api/jobs/999/items.csv").status_code == 404

    def test_csv_export_respects_filter_and_neutralizes_formulas(
        self, client: TestClient, job_id: int,
    ) -> None:
        resp = client.get(f"/api/jobs/{job_id}/items.csv", params={"status": "found"})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv")
        assert f"job-{job_id}-items.csv" in resp.headers["content-disposition"]
        rows = list(csv.DictReader(io.StringIO(resp.text.lstrip("﻿"))))
        assert [r["external_id"] for r in rows] == ["2", "3"]
        assert rows[1]["title"] == "'=Формула"

    def test_error_summary_groups_by_class(self, client: TestClient, job_id: int) -> None:
        rows = client.get(f"/api/jobs/{job_id}/errors").json()
        assert {(r["error_class"], r["error_group"], r["count"]) for r in rows} == {
            ("server_error", "server", 1), ("http_503", "server", 1),
        }
