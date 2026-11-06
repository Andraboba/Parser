"""Галерея загруженных видео: список с поиском и просмотр файла прямо с рабочего сервера.

Файлы остаются на серверах агентов. Координатор забирает нужный кусок по SFTP через runner
и отдаёт браузеру с поддержкой Range: видео можно перематывать, не скачивая целиком.
"""
import re
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from datetime import datetime
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from coordinator import provisioning
from coordinator.api.deps import SessionDep, require_viewer
from coordinator.domain.paths import is_inside
from coordinator.errors import CoordinatorError
from coordinator.runner_client import RunnerClient
from coordinator.storage import results
from coordinator.storage.models import Worker

router = APIRouter(prefix="/api/videos", tags=["videos"])
VIEWER = [Depends(require_viewer)]

_RANGE = re.compile(r"bytes=(\d*)-(\d*)$")
# Кусок на один запрос браузера: перемотка не тянет за собой весь файл.
MAX_RANGE = 8 * 1024 * 1024
_PLAYABLE = ("video/mp4", "video/webm", "video/ogg", "video/quicktime")


class VideoNotFound(CoordinatorError):
    status = 404
    code = "video_not_found"


class VideoUnavailable(CoordinatorError):
    status = 409
    code = "video_unavailable"


class RangeNotSatisfiable(CoordinatorError):
    status = 416
    code = "range_not_satisfiable"


class VideoOut(BaseModel):
    id: int
    job_id: int
    external_id: int
    title: str | None
    page_url: str | None
    mime: str | None
    size: int
    file_name: str
    worker: str | None
    created_at: datetime
    playable: bool

    @classmethod
    def from_view(cls, view: results.VideoView) -> "VideoOut":
        mime = view.mime or _guess_mime(view.storage_path)
        return cls(
            id=view.file_id, job_id=view.job_id, external_id=view.external_id,
            title=view.title, page_url=view.page_url, mime=mime, size=view.size,
            file_name=view.storage_path.rsplit("/", 1)[-1], worker=view.worker,
            created_at=view.created_at, playable=(mime or "").split(";")[0] in _PLAYABLE,
        )


class VideosPage(BaseModel):
    items: list[VideoOut]
    total: int
    # Передайте значением before_id, чтобы получить следующую страницу.
    next_before_id: int | None


def _guess_mime(path: str) -> str | None:
    suffix = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return {
        "mp4": "video/mp4", "m4v": "video/mp4", "webm": "video/webm", "ogv": "video/ogg",
        "mov": "video/quicktime", "mkv": "video/x-matroska", "avi": "video/x-msvideo",
    }.get(suffix)


@router.get("", dependencies=VIEWER)
async def list_videos(
    session: SessionDep,
    q: Annotated[str | None, Query(max_length=200)] = None,
    job_id: Annotated[int | None, Query(ge=1)] = None,
    worker: Annotated[str | None, Query(max_length=100)] = None,
    before_id: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 48,
) -> VideosPage:
    q = (q or "").strip() or None
    rows = await results.list_videos(
        session, q=q, job_id=job_id, worker=worker, before_id=before_id, limit=limit,
    )
    total = await results.count_videos(session, q=q, job_id=job_id, worker=worker)
    return VideosPage(
        items=[VideoOut.from_view(r) for r in rows], total=total,
        next_before_id=rows[-1].file_id if len(rows) == limit else None,
    )


@router.get("/{file_id}", dependencies=VIEWER)
async def get_video(file_id: int, session: SessionDep) -> VideoOut:
    view = await results.get_video(session, file_id)
    if view is None:
        raise VideoNotFound(f"видео {file_id} не найдено")
    return VideoOut.from_view(view)


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Range из заголовка браузера в (start, end) включительно. None — отдать файл целиком."""
    if not header:
        return None
    match = _RANGE.match(header.strip())
    if not match or not (match[1] or match[2]):
        return None
    if not match[1]:
        # bytes=-N: последние N байт.
        start = max(0, size - int(match[2]))
        end = size - 1
    else:
        start = int(match[1])
        end = int(match[2]) if match[2] else size - 1
    if size == 0 or start >= size or end < start:
        raise RangeNotSatisfiable(f"диапазон {header} вне файла размером {size}")
    return start, min(end, size - 1, start + MAX_RANGE - 1)


@router.get("/{file_id}/content", dependencies=VIEWER)
async def video_content(
    file_id: int, request: Request, session: SessionDep,
    range_header: Annotated[str | None, Header(alias="Range")] = None,
    download: bool = False,
) -> Response:
    view = await results.get_video(session, file_id)
    if view is None:
        raise VideoNotFound(f"видео {file_id} не найдено")
    worker = await session.get(Worker, view.worker_id) if view.worker_id else None
    if worker is None or worker.provisioning_mode != provisioning.SSH_MODE:
        raise VideoUnavailable("файл лежит на узле, к которому координатор не подключается по SSH")
    # Путь пришёл от агента: читать разрешено только внутри каталога хранения узла.
    if not is_inside(worker.storage_path, view.storage_path):
        raise VideoUnavailable("путь файла вне каталога хранения узла")
    target = provisioning.target_of(worker)
    await session.close()

    size = view.size
    window = parse_range(range_header, size)
    start, end = window if window else (0, size - 1)
    runner: RunnerClient = request.app.state.runner
    stack = AsyncExitStack()
    response = await stack.enter_async_context(runner.stream("POST", "/ssh/file", json={
        "target": target, "path": view.storage_path, "start": start,
        "length": max(end - start + 1, 1),
    }))
    real_size = int(response.headers.get("x-file-size") or size)
    if real_size != size:
        # В базе устаревший размер: пересчитываем окно по настоящему файлу.
        size = real_size
        if window:
            end = min(end, size - 1)
        else:
            end = size - 1

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in response.aiter_raw():
                yield chunk
        finally:
            await stack.aclose()

    mime = VideoOut.from_view(view).mime or "application/octet-stream"
    name = quote(view.storage_path.rsplit("/", 1)[-1])
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(max(end - start + 1, 0)),
        "Cache-Control": "private, max-age=3600",
        "Content-Disposition": f"{'attachment' if download else 'inline'}; filename*=UTF-8''{name}",
    }
    if window:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(
        body(), status_code=206 if window else 200, media_type=mime, headers=headers,
    )
