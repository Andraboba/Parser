"""Галерея: список загруженных видео, поиск и выдача файла с сервера кусками по Range."""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

from coordinator.api.app import create_app
from coordinator.api.videos import MAX_RANGE, RangeNotSatisfiable, parse_range
from coordinator.config import Settings
from tests.conftest import FLEET_TOKEN, login
from tests.helpers import complete, must_lease, start_job
from tests.test_servers import FakeRunner

CONTENT = bytes(range(256)) * 40  # 10 240 байт


class StreamingRunner(FakeRunner):
    """Изображает /ssh/file: отдаёт запрошенный кусок CONTENT."""

    def __init__(self) -> None:
        super().__init__()
        self.reads: list[dict[str, Any]] = []

    @asynccontextmanager
    async def stream(self, method: str, path: str, **kwargs: Any) -> AsyncIterator[Any]:
        body = kwargs["json"]
        self.reads.append(body)
        start, length = body["start"], body["length"]
        part = CONTENT[start:start + length]

        class Response:
            headers = {"x-file-size": str(len(CONTENT))}

            async def aiter_raw(self) -> AsyncIterator[bytes]:
                yield part

        yield Response()


@pytest.mark.parametrize(("header", "expected"), [
    (None, None), ("", None), ("bytes=0-", (0, 999)), ("bytes=10-19", (10, 19)),
    ("bytes=-100", (900, 999)), ("bytes=900-5000", (900, 999)), ("items=0-1", None),
])
def test_parse_range(header: str | None, expected: tuple[int, int] | None) -> None:
    assert parse_range(header, 1000) == expected


def test_parse_range_caps_window() -> None:
    start, end = parse_range("bytes=0-", MAX_RANGE * 3)
    assert (start, end) == (0, MAX_RANGE - 1)


@pytest.mark.parametrize("header", ["bytes=1000-", "bytes=50-10"])
def test_parse_range_outside_file(header: str) -> None:
    with pytest.raises(RangeNotSatisfiable):
        parse_range(header, 1000)


def video(external_id: int, title: str) -> dict[str, Any]:
    return {
        "external_id": external_id, "status": "completed", "http_status": 200, "title": title,
        "page_url": f"http://site/page/{external_id}",
        "download_url": f"http://site/video/{external_id}.mp4",
        "file": {"path": f"/data/videos/{external_id}.mp4", "size": len(CONTENT),
                 "sha256": "abc", "mime": "video/mp4", "resumed": False, "skipped": False},
    }



@pytest.fixture
def runner() -> StreamingRunner:
    return StreamingRunner()


@pytest.fixture
def api(clean_db: None, runner: StreamingRunner) -> Any:
    settings = Settings(threads_per_chunk=1, agent_site_template="http://site/{id}")
    with TestClient(create_app(settings, runner=runner)) as client:
        client.headers["X-Agent-Token"] = FLEET_TOKEN
        login(client)
        resp = client.post("/api/servers", json={
            "name": "lab-srv-1", "host": "lab-srv-1", "threads": 4, "storage_path": "/data/videos",
        })
        assert resp.status_code == 201, resp.text
        worker_id = client.post("/api/workers/register", json={
            "name": "lab-srv-1", "agent_version": "t", "protocol_version": 1, "threads": 4,
            "storage_path": "/data/videos",
        }).json()["worker_id"]
        start_job(client)
        chunk = must_lease(client, worker_id)
        resp = complete(client, worker_id, chunk, results=[
            video(1, "Кошки на крыше"), video(2, "Собаки в парке"), video(3, "Кошки дома"),
            {"external_id": 4, "status": "not_found", "http_status": 404},
        ])
        assert resp.status_code == 200, resp.text
        yield client


@pytest.mark.integration
class TestGallery:
    def test_lists_only_downloaded_files_newest_first(self, api: TestClient) -> None:
        page = api.get("/api/videos").json()
        assert page["total"] == 3
        assert [v["external_id"] for v in page["items"]] == [3, 2, 1]
        first = page["items"][0]
        assert first["worker"] == "lab-srv-1"
        assert first["playable"] is True
        assert first["file_name"] == "3.mp4"

    @pytest.mark.parametrize(("q", "ids"), [
        ("кошки", [3, 1]), ("КОШКИ дома", [3]), ("2", [2]), ("page/1", [1]), ("нет", []),
    ])
    def test_search(self, api: TestClient, q: str, ids: list[int]) -> None:
        page = api.get("/api/videos", params={"q": q}).json()
        assert [v["external_id"] for v in page["items"]] == ids
        assert page["total"] == len(ids)

    def test_pages(self, api: TestClient) -> None:
        first = api.get("/api/videos", params={"limit": 2}).json()
        assert len(first["items"]) == 2
        rest = api.get(
            "/api/videos", params={"limit": 2, "before_id": first["next_before_id"]},
        ).json()
        assert [v["external_id"] for v in rest["items"]] == [1]
        assert rest["next_before_id"] is None

    def test_range_request_streams_part_of_file(
        self, api: TestClient, runner: StreamingRunner,
    ) -> None:
        file_id = api.get("/api/videos").json()["items"][0]["id"]
        resp = api.get(f"/api/videos/{file_id}/content", headers={"Range": "bytes=100-199"})
        assert resp.status_code == 206
        assert resp.content == CONTENT[100:200]
        assert resp.headers["content-range"] == f"bytes 100-199/{len(CONTENT)}"
        assert resp.headers["content-type"] == "video/mp4"
        read = runner.reads[-1]
        assert read["path"] == "/data/videos/3.mp4"
        assert read["target"]["host"] == "lab-srv-1"
        assert "password" not in read["target"]

    def test_whole_file_and_download(self, api: TestClient) -> None:
        file_id = api.get("/api/videos").json()["items"][0]["id"]
        resp = api.get(f"/api/videos/{file_id}/content", params={"download": "true"})
        assert resp.status_code == 200
        assert resp.content == CONTENT
        assert resp.headers["content-disposition"].startswith("attachment;")

    def test_range_outside_file_is_416(self, api: TestClient) -> None:
        file_id = api.get("/api/videos").json()["items"][0]["id"]
        resp = api.get(f"/api/videos/{file_id}/content", headers={"Range": "bytes=999999-"})
        assert resp.status_code == 416

    def test_unknown_video_is_404(self, api: TestClient) -> None:
        assert api.get("/api/videos/999999/content").status_code == 404

    def test_requires_login(self, api: TestClient) -> None:
        anonymous = TestClient(api.app)
        anonymous.portal = api.portal
        assert anonymous.get("/api/videos").status_code == 401
