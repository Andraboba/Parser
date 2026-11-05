"""Агентский API и узлы для интерфейса."""
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status

from coordinator.api.deps import (
    AgentTokenDep,
    AgentWorkerDep,
    RedisDep,
    SessionDep,
    SettingsDep,
    check_protocol,
    require_operator,
    require_viewer,
)
from coordinator.api.schemas import (
    AGENT_ERROR_RESPONSES,
    Ack,
    CompleteRequest,
    CompleteResponse,
    FailRequest,
    HeartbeatRequest,
    HeartbeatResponse,
    LeaseRequest,
    LeaseResponse,
    MetricSample,
    ProgressRequest,
    ProgressResponse,
    RegisterRequest,
    RegisterResponse,
    ResultIn,
    WorkerOut,
    WorkerPatch,
)
from coordinator.dispatcher import leasing
from coordinator.domain.balance import refusal_reason
from coordinator.domain.statuses import ItemStatus
from coordinator.errors import TokenForbidden
from coordinator.storage import events, ratelimit, workers
from coordinator.storage.models import Job

router = APIRouter(prefix="/api/workers", tags=["workers"])
agent_router = APIRouter(
    prefix="/api/workers", tags=["agent"], responses=AGENT_ERROR_RESPONSES,
)

IdempotencyKeyHeader = Annotated[str | None, Header(max_length=180)]


def _throttled(results: list[ResultIn]) -> int:
    """Ответы, по которым снижается общий лимит запросов: 429 и 5xx (ТЗ, п. 9)."""
    return sum(
        1 for r in results
        if r.status == ItemStatus.RATE_LIMITED or (r.http_status or 0) >= 500
    )


@agent_router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterRequest, request: Request, token: AgentTokenDep, session: SessionDep,
    settings: SettingsDep,
) -> RegisterResponse:
    check_protocol(body.protocol_version, settings)
    request.state.audit_actor = f"agent:{body.name}"
    if not token.allows(body.name):
        raise TokenForbidden(f"токен не принадлежит узлу {body.name}")
    worker_id = await workers.register_worker(session, workers.Registration(
        name=body.name, provisioning_mode=body.provisioning_mode, container_id=body.container_id,
        image=body.image, image_digest=body.image_digest, agent_version=body.agent_version,
        protocol_version=body.protocol_version, threads=body.threads, cpu_limit=body.limits.cpus,
        mem_limit_mb=body.limits.memory_mb, storage_path=body.storage_path,
        last_ip=request.client.host if request.client else None,
    ))
    await leasing.release_worker_leases(session, worker_id)
    await events.emit(
        session, "worker.register", "online",
        f"{body.name}, агент {body.agent_version}, потоков {body.threads}", worker_id=worker_id,
    )
    await session.commit()
    return RegisterResponse(
        worker_id=worker_id, heartbeat_interval=settings.heartbeat_interval,
        lease_ttl=settings.lease_ttl,
    )


@agent_router.post("/{worker_id}/heartbeat")
async def heartbeat(
    worker: AgentWorkerDep, body: HeartbeatRequest, session: SessionDep, settings: SettingsDep,
    redis: RedisDep,
) -> HeartbeatResponse:
    await workers.record_heartbeat(
        session, worker, agent_state=body.status, free_slots=body.free_slots,
        metrics=body.metrics.model_dump(exclude_none=True), counters=body.counters,
    )
    cancel = await leasing.renew_leases(
        session, worker.id, settings.lease_ttl, body.active_chunks,
        settings.heartbeat_interval * 2,
    )
    job_id = await leasing.active_job(session, worker.id)
    threads = await workers.resolve_threads(session, worker, job_id)
    share = await ratelimit.share_for(session, redis, settings, worker.id)
    await session.commit()
    return HeartbeatResponse(
        lease_ttl=settings.lease_ttl, cancel_chunks=cancel, rps_share=share,
        threads=threads if threads != worker.threads else None,
    )


@agent_router.post(
    "/{worker_id}/lease", response_model=LeaseResponse,
    responses={204: {"description": "Работы нет"}},
)
async def lease(
    worker: AgentWorkerDep, body: LeaseRequest, session: SessionDep, settings: SettingsDep,
    redis: RedisDep,
) -> LeaseResponse | Response:
    state = await workers.node_state(session, worker, body.free_slots, settings)
    reason = refusal_reason(state, workers.thresholds(settings))
    worker.lease_block_reason = reason
    if reason is not None:
        await session.commit()
        return Response(
            status_code=status.HTTP_204_NO_CONTENT, headers={"X-Lease-Refused": reason},
        )
    chunk = await leasing.lease_chunk(session, worker.id, settings.lease_ttl)
    if chunk is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    job = await session.get(Job, chunk.job_id)
    assert job is not None
    return LeaseResponse(
        chunk_id=chunk.chunk_id, job_id=chunk.job_id, range_start=chunk.range_start,
        range_end=chunk.range_end, next_id=chunk.next_id, attempt=chunk.attempt,
        lease_expires_at=chunk.lease_expires_at, lease_ttl=settings.lease_ttl,
        rps_share=await ratelimit.share_for(session, redis, settings, worker.id),
        threads=await workers.resolve_threads(session, worker, chunk.job_id),
        request_timeout_seconds=job.request_timeout_seconds, max_retries=job.max_retries,
    )


@agent_router.post("/{worker_id}/progress")
async def progress(
    worker: AgentWorkerDep, body: ProgressRequest, session: SessionDep, settings: SettingsDep,
    redis: RedisDep,
) -> ProgressResponse:
    outcome = await leasing.record_progress(
        session, worker, body.chunk_id, body.next_id, [r.to_row() for r in body.results],
        settings.lease_ttl,
    )
    await ratelimit.report_throttled(redis, _throttled(body.results))
    return ProgressResponse(cancel=outcome.cancel, rejected=outcome.rejected)


@agent_router.post("/{worker_id}/complete")
async def complete(
    worker: AgentWorkerDep, body: CompleteRequest, session: SessionDep, redis: RedisDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> CompleteResponse:
    outcome = await leasing.complete_chunk(
        session, worker, body.chunk_id, body.next_id, [r.to_row() for r in body.results],
        idempotency_key,
    )
    await ratelimit.report_throttled(redis, _throttled(body.results))
    return CompleteResponse(rejected=outcome.rejected)


@agent_router.post("/{worker_id}/fail")
async def fail(
    worker: AgentWorkerDep, body: FailRequest, session: SessionDep, settings: SettingsDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Ack:
    await leasing.fail_chunk(
        session, worker, body.chunk_id, body.error_class, body.next_id,
        settings.max_chunk_attempts, idempotency_key, body.message,
    )
    return Ack()


@router.get("", dependencies=[Depends(require_viewer)])
async def list_workers(session: SessionDep) -> list[WorkerOut]:
    return [WorkerOut.from_view(v) for v in await workers.list_worker_views(session)]


@router.get("/{worker_id}", dependencies=[Depends(require_viewer)])
async def get_worker(worker_id: int, session: SessionDep) -> WorkerOut:
    return WorkerOut.from_view(await workers.get_worker_view(session, worker_id))


@router.patch("/{worker_id}", dependencies=[Depends(require_operator)])
async def patch_worker(worker_id: int, body: WorkerPatch, session: SessionDep) -> WorkerOut:
    await workers.apply_settings(session, worker_id, body.model_dump(exclude_unset=True))
    return WorkerOut.from_view(await workers.get_worker_view(session, worker_id))


@router.get("/{worker_id}/metrics", dependencies=[Depends(require_viewer)])
async def worker_metrics(
    worker_id: int, session: SessionDep, minutes: Annotated[int, Query(ge=1, le=1440)] = 15,
) -> list[MetricSample]:
    await workers.get_worker_view(session, worker_id)
    rows = await workers.metrics_series(session, worker_id, minutes)
    return [MetricSample.model_validate(r) for r in rows]
