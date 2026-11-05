"""Управление контейнерами узлов из интерфейса: через runner, сокета Docker здесь нет."""
import asyncio
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select

from coordinator.api.deps import (
    SessionDep,
    SettingsDep,
    require_admin,
    require_operator,
    require_viewer,
)
from coordinator.api.schemas import ContainerCreate, ContainerImage, ContainerLogs, ContainerOut
from coordinator.errors import WorkerNotFound
from coordinator.runner_client import RunnerClient, RunnerRejected
from coordinator.storage import tokens
from coordinator.storage.models import Worker

router = APIRouter(prefix="/api", tags=["containers"])


def _runner(request: Request) -> RunnerClient:
    return request.app.state.runner


RunnerDep = Annotated[RunnerClient, Depends(_runner)]


async def _worker_name(session: SessionDep, worker_id: int) -> str:
    worker = await session.get(Worker, worker_id)
    if worker is None:
        raise WorkerNotFound(f"узел {worker_id} не найден")
    return worker.name


WorkerNameDep = Annotated[str, Depends(_worker_name)]


ADMIN = [Depends(require_admin)]
OPERATOR = [Depends(require_operator)]
VIEWER = [Depends(require_viewer)]


@router.get("/containers", dependencies=VIEWER)
async def list_containers(runner: RunnerDep) -> list[ContainerOut]:
    return [ContainerOut.model_validate(c) for c in await runner.call("GET", "/containers")]


@router.get("/containers/images", dependencies=VIEWER)
async def allowed_images(runner: RunnerDep) -> list[str]:
    return await runner.call("GET", "/images")


@router.post("/containers", status_code=201, dependencies=ADMIN)
async def create_container(
    body: ContainerCreate, runner: RunnerDep, session: SessionDep, settings: SettingsDep,
) -> ContainerOut:
    """Новый узел без правки compose (раздел 13 плана). Токен выпускается здесь и уходит только
    в окружение контейнера: ни в ответ, ни в журнал он не попадает."""
    token = secrets.token_hex(24)
    await tokens.upsert_token(session, body.name, token)
    await session.commit()
    await runner.call("POST", "/containers", json={
        "name": body.name, "image": body.image, "threads": body.threads, "cpus": body.cpus,
        "memory_mb": body.memory_mb,
        "env": {"AGENT_TOKEN": token, "COORD_URL": settings.agent_coordinator_url,
                "AGENT_COORDINATOR_URL": settings.agent_coordinator_url},
    })
    return ContainerOut.model_validate(await runner.call("GET", f"/containers/{body.name}"))


@router.get("/workers/{worker_id}/container", dependencies=VIEWER)
async def container_status(name: WorkerNameDep, runner: RunnerDep) -> ContainerOut:
    return ContainerOut.model_validate(await runner.call("GET", f"/containers/{name}"))


@router.post("/workers/{worker_id}/container/start", dependencies=OPERATOR)
async def start_container(name: WorkerNameDep, runner: RunnerDep) -> ContainerOut:
    await runner.call("POST", f"/containers/{name}/start")
    return ContainerOut.model_validate(await runner.call("GET", f"/containers/{name}"))


@router.post("/workers/{worker_id}/container/stop", dependencies=OPERATOR)
async def stop_container(name: WorkerNameDep, runner: RunnerDep) -> ContainerOut:
    await runner.call("POST", f"/containers/{name}/stop")
    return ContainerOut.model_validate(await runner.call("GET", f"/containers/{name}"))


@router.post("/workers/{worker_id}/container/restart", dependencies=OPERATOR)
async def restart_container(name: WorkerNameDep, runner: RunnerDep) -> ContainerOut:
    await runner.call("POST", f"/containers/{name}/restart")
    return ContainerOut.model_validate(await runner.call("GET", f"/containers/{name}"))


@router.get("/workers/{worker_id}/container/logs", dependencies=OPERATOR)
async def container_logs(
    name: WorkerNameDep, runner: RunnerDep, tail: Annotated[int, Query(ge=1, le=5000)] = 200,
) -> ContainerLogs:
    body = await runner.call("GET", f"/containers/{name}/logs", params={"tail": tail})
    return ContainerLogs(logs=body["logs"])


async def _registered(request: Request, name: str, container_id: str) -> bool:
    # Каждый раз новая сессия: иначе транзакция не увидела бы свежую регистрацию.
    async with request.app.state.sessionmaker() as session:
        worker = await session.scalar(select(Worker).where(Worker.name == name))
        reported = (worker.container_id or "") if worker else ""
    return bool(reported) and (
        reported.startswith(container_id) or container_id.startswith(reported)
    )


@router.post("/workers/{worker_id}/container/update-image", dependencies=ADMIN)
async def update_image(
    body: ContainerImage, name: WorkerNameDep, runner: RunnerDep, request: Request,
    settings: SettingsDep,
) -> ContainerOut:
    """Новый контейнер из нового образа, ожидание регистрации агента, при неудаче — откат."""
    update = await runner.call(
        "POST", f"/containers/{name}/update-image", json={"image": body.image},
    )
    deadline = asyncio.get_running_loop().time() + settings.image_update_timeout
    while asyncio.get_running_loop().time() < deadline:
        if await _registered(request, name, update["id"]):
            await runner.call("POST", "/updates/commit", json={
                "previous_id": update["previous_id"],
            })
            return ContainerOut.model_validate(await runner.call("GET", f"/containers/{name}"))
        await asyncio.sleep(0.5)
    await runner.call("POST", "/updates/rollback", json={
        "full_id": update["full_id"], "previous_id": update["previous_id"],
    })
    raise RunnerRejected(
        502, "update_rolled_back",
        f"агент из образа {body.image} не зарегистрировался, возвращён прежний контейнер",
    )
