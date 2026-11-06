"""Серверы с агентами по SSH (ТЗ, п. 4 и 5.1). Ключи здесь не ходят: только имя секрета."""
import fnmatch
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select

from coordinator import provisioning
from coordinator.api.deps import (
    SessionDep,
    SettingsDep,
    require_admin,
    require_operator,
)
from coordinator.api.schemas import WorkerOut
from coordinator.errors import (
    HostNotAllowed,
    InvalidServer,
    ServerExists,
    WorkerNotFound,
)
from coordinator.runner_client import RunnerClient, RunnerRejected
from coordinator.storage import events, workers
from coordinator.storage.models import Worker

router = APIRouter(prefix="/api", tags=["servers"])

ADMIN = [Depends(require_admin)]
OPERATOR = [Depends(require_operator)]
AGENT_KINDS = ("real", "stub")


class ServerCreate(BaseModel):
    """Параметры сервера из ТЗ, п. 4."""

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    host: str | None = Field(default=None, min_length=1, max_length=255)
    ssh_port: int = Field(default=22, ge=1, le=65535)
    ssh_user: str = Field(default="agent", pattern=r"^[a-z_][a-z0-9_-]{0,31}$")
    # Имя секрета в хранилище, а не сам ключ (ТЗ, п. 11.4).
    ssh_key: str = Field(default="ssh_client_key", pattern=r"^[A-Za-z0-9_-]{1,64}$")
    # Пароль только для первого входа; после него координатор ставит свой ключ.
    ssh_password: str | None = Field(default=None, min_length=1, max_length=256)
    threads: int = Field(default=4, ge=1, le=1000)
    storage_path: str = Field(default="/data/videos", pattern=r"^/[^\x00]{0,1023}$")
    min_free_space_gb: float | None = Field(default=None, ge=0)
    agent_kind: str | None = Field(default=None, pattern=r"^(real|stub)$")
    start: bool = True
    # Только на стенде: создать контейнер, который будет этим сервером.
    create_container: bool = False
    cpus: float = Field(default=0.5, ge=0.1, le=64)
    memory_mb: int = Field(default=256, ge=64, le=1024 * 1024)

    @model_validator(mode="after")
    def check_host(self) -> "ServerCreate":
        if not self.create_container and not self.host:
            raise ValueError("укажите адрес сервера или создайте контейнер")
        return self


class DeployRequest(BaseModel):
    agent_kind: str | None = Field(default=None, pattern=r"^(real|stub)$")


class AuthorizeRequest(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class AgentLogs(BaseModel):
    logs: str


class ServerCheck(BaseModel):
    reachable: bool
    installed: bool = False
    running: bool = False
    pid: int | None = None
    release: str | None = None
    python: str | None = None
    kernel: str | None = None
    error: str | None = None
    error_code: str | None = None


def _host_allowed(host: str, patterns: list[str]) -> bool:
    """Имена — по allowlist, настоящая машина по IP — разрешена администратору."""
    return provisioning.is_ip_address(host) or any(
        fnmatch.fnmatchcase(host, pattern) for pattern in patterns
    )


def _runner(request: Request) -> RunnerClient:
    return request.app.state.runner


RunnerDep = Annotated[RunnerClient, Depends(_runner)]


async def _server(session: SessionDep, worker_id: int) -> Worker:
    worker = await session.get(Worker, worker_id, populate_existing=True)
    if worker is None:
        raise WorkerNotFound(f"узел {worker_id} не найден")
    if worker.provisioning_mode != provisioning.SSH_MODE:
        raise InvalidServer(f"узел {worker.name} управляется не по SSH")
    return worker


ServerDep = Annotated[Worker, Depends(_server)]


async def _card(session: Any, worker_id: int) -> WorkerOut:
    return WorkerOut.from_view(await workers.get_worker_view(session, worker_id))


@router.post("/servers", status_code=status.HTTP_201_CREATED, dependencies=ADMIN)
async def create_server(
    body: ServerCreate, session: SessionDep, runner: RunnerDep, settings: SettingsDep,
) -> WorkerOut:
    exists = await session.scalar(
        select(func.count()).select_from(Worker).where(Worker.name == body.name)
    )
    if exists:
        raise ServerExists(f"узел {body.name} уже существует")
    host = body.host
    if body.create_container:
        created = await runner.call("POST", "/ssh/servers", json={
            "name": body.name, "cpus": body.cpus, "memory_mb": body.memory_mb,
            "key_ref": body.ssh_key,
        })
        host = created["host"]
    elif host is None or not _host_allowed(host, settings.ssh_host_patterns()):
        # Подключение всё равно запретит runner. Здесь отказ сразу и без мёртвой строки в базе.
        raise HostNotAllowed(
            f"адрес {host or '—'} вне разрешённых, см. COORD_SSH_ALLOWED_HOSTS"
        )
    if body.ssh_password and not body.create_container:
        # Вход по паролю один раз: ставим ключ координатора, дальше только он.
        # Неверный пароль — отказ сразу, без строки в базе. Пароль нигде не сохраняется.
        await runner.call("POST", "/ssh/authorize", json={"target": {
            "host": host, "port": body.ssh_port, "user": body.ssh_user,
            "key_ref": body.ssh_key, "password": body.ssh_password,
        }})
    worker = Worker(
        name=body.name, provisioning_mode=provisioning.SSH_MODE, ssh_host=host,
        ssh_port=body.ssh_port, ssh_user=body.ssh_user, ssh_key_ref=body.ssh_key,
        threads=body.threads, storage_path=body.storage_path,
        min_free_space_gb=body.min_free_space_gb,
        agent_kind=body.agent_kind or settings.agent_kind,
        desired_agent_state=(
            provisioning.AGENT_RUNNING if body.start else provisioning.AGENT_STOPPED
        ),
        status="offline", agent_version="", protocol_version=0,
    )
    session.add(worker)
    await session.flush()
    await events.emit(
        session, "server.create", "ok", f"{body.name}: {host}:{body.ssh_port}",
        worker_id=worker.id,
    )
    await session.commit()
    return await _card(session, worker.id)


@router.post("/workers/{worker_id}/agent/check", dependencies=OPERATOR)
async def check_server(worker: ServerDep, runner: RunnerDep) -> ServerCheck:
    """Проверка доступности сервера (ТЗ, п. 5.1). Недоступность — ответ, а не ошибка запроса."""
    try:
        data = await runner.call("POST", "/ssh/status", json={
            "target": provisioning.target_of(worker),
        })
    except RunnerRejected as exc:
        return ServerCheck(reachable=False, error=str(exc), error_code=exc.code)
    return ServerCheck(reachable=True, **data)


async def _set_desired(session: Any, worker: Worker, state: str) -> None:
    worker.desired_agent_state = state
    await session.commit()


@router.post("/workers/{worker_id}/agent/start", dependencies=OPERATOR)
async def start_agent(worker: ServerDep, session: SessionDep, runner: RunnerDep) -> WorkerOut:
    await _set_desired(session, worker, provisioning.AGENT_RUNNING)
    await runner.call("POST", "/ssh/start", json={"target": provisioning.target_of(worker)})
    return await _card(session, worker.id)


@router.post("/workers/{worker_id}/agent/stop", dependencies=OPERATOR)
async def stop_agent(worker: ServerDep, session: SessionDep, runner: RunnerDep) -> WorkerOut:
    # Сначала желаемое состояние: иначе диспетчер успеет поднять агента обратно.
    await _set_desired(session, worker, provisioning.AGENT_STOPPED)
    await runner.call("POST", "/ssh/stop", json={"target": provisioning.target_of(worker)})
    return await _card(session, worker.id)


@router.post("/workers/{worker_id}/agent/restart", dependencies=OPERATOR)
async def restart_agent(worker: ServerDep, session: SessionDep, runner: RunnerDep) -> WorkerOut:
    await _set_desired(session, worker, provisioning.AGENT_RUNNING)
    await runner.call("POST", "/ssh/restart", json={"target": provisioning.target_of(worker)})
    return await _card(session, worker.id)


@router.get("/workers/{worker_id}/agent/logs", dependencies=OPERATOR)
async def agent_logs(
    worker: ServerDep, runner: RunnerDep, tail: Annotated[int, Query(ge=1, le=5000)] = 200,
) -> AgentLogs:
    body = await runner.call("POST", "/ssh/logs", json={
        "target": provisioning.target_of(worker), "tail": tail,
    })
    return AgentLogs(logs=body["logs"])


@router.post("/workers/{worker_id}/agent/deploy", dependencies=ADMIN)
async def deploy_agent(
    body: DeployRequest, worker: ServerDep, session: SessionDep, runner: RunnerDep,
    settings: SettingsDep, request: Request,
) -> WorkerOut:
    """Установка или обновление: новый выпуск, перезапуск, ждём регистрации, при неудаче откат."""
    await provisioning.deploy_agent(session, runner, worker, settings, body.agent_kind)
    target = provisioning.target_of(worker)
    since = datetime.now(UTC)
    await _set_desired(session, worker, provisioning.AGENT_RUNNING)
    await runner.call("POST", "/ssh/restart", json={"target": target})
    if await provisioning.wait_for_registration(
        request.app.state.sessionmaker, worker.id, since, settings.image_update_timeout,
    ):
        return await _card(session, worker.id)
    await runner.call("POST", "/ssh/rollback", json={"target": target})
    await events.emit(
        session, "agent.deploy", "rolled_back", "агент не зарегистрировался, возвращён прежний",
        level="error", component="runner", worker_id=worker.id, error_code="update_rolled_back",
    )
    await session.commit()
    raise RunnerRejected(
        502, "update_rolled_back",
        f"новый агент на {worker.name} не зарегистрировался, возвращена прежняя версия",
    )


@router.post("/workers/{worker_id}/agent/authorize", dependencies=ADMIN)
async def authorize_server(
    body: AuthorizeRequest, worker: ServerDep, session: SessionDep, runner: RunnerDep,
) -> dict[str, bool]:
    """Сервер отклоняет ключ (переустановлен, ключ стёрт): войти по паролю и поставить заново."""
    await runner.call("POST", "/ssh/authorize", json={
        "target": provisioning.target_of(worker, body.password),
    })
    await events.emit(
        session, "server.authorize", "ok", f"{worker.name}: установлен ключ координатора",
        component="runner", worker_id=worker.id,
    )
    await session.commit()
    return {"ok": True}


@router.post("/workers/{worker_id}/agent/forget-host-key", dependencies=ADMIN)
async def forget_host_key(worker: ServerDep, runner: RunnerDep) -> dict[str, bool]:
    """После законной замены сервера: следующее подключение запомнит его новый ключ."""
    await runner.call("POST", "/ssh/forget-host-key", json={
        "target": provisioning.target_of(worker),
    })
    return {"ok": True}
