"""Управление заданиями из интерфейса."""
import csv
import io
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import StreamingResponse

from coordinator.api.deps import SessionDep, require_operator, require_viewer
from coordinator.api.schemas import (
    ErrorSummaryRow,
    ItemOut,
    ItemsPage,
    JobCreate,
    JobOut,
    JobPatch,
    RetryOut,
)
from coordinator.domain.retry import error_group
from coordinator.domain.statuses import ItemStatus
from coordinator.storage import jobs, results

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

OPERATOR = [Depends(require_operator)]
VIEWER = [Depends(require_viewer)]

StatusFilter = Annotated[ItemStatus | None, Query()]
WorkerFilter = Annotated[str | None, Query(max_length=100)]
SearchFilter = Annotated[str | None, Query(max_length=200)]


@router.post("", status_code=status.HTTP_201_CREATED, dependencies=OPERATOR)
async def create_job(body: JobCreate, session: SessionDep) -> JobOut:
    job_id = await jobs.create_job(session, body.to_params())
    return JobOut.from_view(await jobs.get_job_view(session, job_id))


@router.get("", dependencies=VIEWER)
async def list_jobs(session: SessionDep) -> list[JobOut]:
    return [JobOut.from_view(v) for v in await jobs.list_job_views(session)]


@router.get("/{job_id}", dependencies=VIEWER)
async def get_job(job_id: int, session: SessionDep) -> JobOut:
    return JobOut.from_view(await jobs.get_job_view(session, job_id))


@router.patch("/{job_id}", dependencies=OPERATOR)
async def patch_job(job_id: int, body: JobPatch, session: SessionDep) -> JobOut:
    await jobs.apply_settings(session, job_id, body.model_dump(exclude_unset=True))
    return JobOut.from_view(await jobs.get_job_view(session, job_id))


@router.post("/{job_id}/start", dependencies=OPERATOR)
async def start_job(job_id: int, session: SessionDep) -> JobOut:
    await jobs.start_job(session, job_id)
    return JobOut.from_view(await jobs.get_job_view(session, job_id))


@router.post("/{job_id}/pause", dependencies=OPERATOR)
async def pause_job(job_id: int, session: SessionDep) -> JobOut:
    await jobs.pause_job(session, job_id)
    return JobOut.from_view(await jobs.get_job_view(session, job_id))


@router.post("/{job_id}/resume", dependencies=OPERATOR)
async def resume_job(job_id: int, session: SessionDep) -> JobOut:
    await jobs.resume_job(session, job_id)
    return JobOut.from_view(await jobs.get_job_view(session, job_id))


@router.post("/{job_id}/stop", dependencies=OPERATOR)
async def stop_job(job_id: int, session: SessionDep) -> JobOut:
    await jobs.stop_job(session, job_id)
    return JobOut.from_view(await jobs.get_job_view(session, job_id))


@router.post("/{job_id}/restart", dependencies=OPERATOR)
async def restart_job(job_id: int, session: SessionDep) -> JobOut:
    await jobs.restart_job(session, job_id)
    return JobOut.from_view(await jobs.get_job_view(session, job_id))


@router.post("/{job_id}/retry-errors", dependencies=OPERATOR)
async def retry_errors(job_id: int, session: SessionDep) -> RetryOut:
    """Возвращает в работу элементы с ошибками и упавшие блоки (ТЗ, п. 11.3)."""
    outcome = await jobs.retry_errors(session, job_id)
    return RetryOut(
        items_requeued=outcome.items_requeued, chunks_created=outcome.chunks_created,
        chunks_requeued=outcome.chunks_requeued,
        job=JobOut.from_view(await jobs.get_job_view(session, job_id)),
    )


@router.get("/{job_id}/items", dependencies=VIEWER)
async def list_items(
    job_id: int, session: SessionDep,
    item_status: Annotated[StatusFilter, Query(alias="status")] = None,
    worker: WorkerFilter = None, q: SearchFilter = None,
    after_id: Annotated[int | None, Query(ge=0, le=2**63 - 1)] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> ItemsPage:
    await jobs.get_job_view(session, job_id)
    rows = await results.list_items(
        session, job_id, status=item_status, worker=worker, q=q, after_id=after_id, limit=limit,
    )
    return ItemsPage(
        items=[ItemOut.from_view(r) for r in rows],
        next_after_id=rows[-1].external_id if len(rows) == limit else None,
    )


_CSV_COLUMNS = (
    "external_id", "status", "error_class", "http_status", "attempts", "worker", "updated_at",
    "title", "page_url", "download_url", "mime", "checksum", "file_path", "file_size",
)


def csv_cell(value: object) -> object:
    """Названия приходят с внешнего сайта: без защиты Excel выполнит «=формулу» как код."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return "" if value is None else value


@router.get("/{job_id}/items.csv", dependencies=VIEWER)
async def export_items(
    job_id: int, request: Request, session: SessionDep,
    item_status: Annotated[StatusFilter, Query(alias="status")] = None,
    worker: WorkerFilter = None, q: SearchFilter = None,
) -> StreamingResponse:
    await jobs.get_job_view(session, job_id)
    sessions = request.app.state.sessionmaker

    async def rows() -> AsyncIterator[str]:
        # Своя сессия: зависимость закрывается раньше, чем заканчивается потоковый ответ.
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(_CSV_COLUMNS)
        yield "﻿" + buffer.getvalue()
        async with sessions() as stream_session:
            async for item in results.stream_items(
                stream_session, job_id, status=item_status, worker=worker, q=q,
            ):
                buffer.seek(0)
                buffer.truncate()
                writer.writerow([csv_cell(getattr(item, c)) for c in _CSV_COLUMNS])
                yield buffer.getvalue()

    return StreamingResponse(
        rows(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="job-{job_id}-items.csv"'},
    )


@router.get("/{job_id}/errors", dependencies=VIEWER)
async def job_errors(job_id: int, session: SessionDep) -> list[ErrorSummaryRow]:
    await jobs.get_job_view(session, job_id)
    return [
        ErrorSummaryRow(
            status=item_status, error_class=error_class, error_group=error_group(error_class),
            count=count,
        )
        for item_status, error_class, count in await results.error_summary(session, job_id)
    ]
