"""Агенты на серверах по SSH: установка, запуск и приведение к желаемому состоянию.

Команды выполняет runner, здесь решения: что запускать, какой токен выдать, что делать при сбое.
"""
import asyncio
import ipaddress
import logging
import secrets
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coordinator.config import Settings
from coordinator.domain.statuses import WorkerStatus
from coordinator.errors import CoordinatorError, InvalidServer
from coordinator.runner_client import RunnerClient
from coordinator.storage import events, tokens
from coordinator.storage.models import Worker

log = logging.getLogger("coordinator.provisioning")

SSH_MODE = "ssh"
AGENT_RUNNING = "running"
AGENT_STOPPED = "stopped"
UNKNOWN_STATE = {"worker": None, "id": "—", "image": "—", "restart_count": 0, "oom_killed": False}


def is_ip_address(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def target_of(worker: Worker, password: str | None = None) -> dict[str, Any]:
    if worker.provisioning_mode != SSH_MODE or not worker.ssh_host:
        raise InvalidServer(f"узел {worker.name} управляется не по SSH")
    target = {
        "host": worker.ssh_host, "port": worker.ssh_port or 22,
        "user": worker.ssh_user or "agent", "key_ref": worker.ssh_key_ref or "ssh_client_key",
    }
    if password:
        # Пароль живёт только в этом запросе, в Worker и журналы не попадает.
        target["password"] = password
    return target


def agent_env(worker: Worker, token: str, settings: Settings) -> dict[str, str]:
    """Настройки агента на сервере. Имена для настоящего агента и заглушки разные."""
    url = settings.agent_coordinator_url
    if settings.agent_public_url and worker.ssh_host and is_ip_address(worker.ssh_host):
        # Настоящая машина вне сети стенда: имя web она не знает.
        url = settings.agent_public_url.rstrip("/")
    storage = worker.storage_path or "/data/videos"
    threads = str(worker.desired_threads or worker.threads)
    env = {
        "AGENT_TOKEN": token, "AGENT_COORDINATOR_URL": url, "COORD_URL": url,
        "AGENT_NAME": worker.name, "WORKER_NAME": worker.name,
        "AGENT_THREADS": threads, "WORKER_THREADS": threads,
        "AGENT_STORAGE_PATH": storage, "STUB_STORAGE_PATH": storage,
        "AGENT_IMAGE": f"ssh:{worker.agent_kind or settings.agent_kind}",
        "SSL_CERT_FILE": settings.agent_ca_file,
    }
    if settings.agent_site_template:
        env["AGENT_SITE_TEMPLATE"] = settings.agent_site_template
    return env


async def deploy_agent(
    session: AsyncSession, runner: RunnerClient, worker: Worker, settings: Settings,
    kind: str | None = None,
) -> dict[str, Any]:
    """Загружает выпуск агента и новый токен. Прежние токены узла сразу отзываются:
    каждая установка заодно становится ротацией."""
    token = secrets.token_hex(24)
    await tokens.replace_for_worker(session, worker.name, token)
    if kind:
        worker.agent_kind = kind
    chosen = worker.agent_kind or settings.agent_kind
    worker.agent_kind = chosen
    await session.commit()
    result = await runner.call("POST", "/ssh/deploy", json={
        "target": target_of(worker), "kind": chosen, "env": agent_env(worker, token, settings),
    })
    # Иначе оператор до следующей проверки сервера видел бы старый выпуск.
    worker.container_state = {
        **(worker.container_state or UNKNOWN_STATE), "image": result["release"],
    }
    await events.emit(
        session, "agent.deploy", "ok", f"{worker.name}: выпуск {result['release']}",
        component="runner", worker_id=worker.id,
    )
    await session.commit()
    return result


def _state_of(status: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    if status["running"]:
        state = AGENT_RUNNING
    else:
        state = AGENT_STOPPED if status["installed"] else "not_installed"
    return {
        "worker": None, "id": f"pid {status['pid']}" if status.get("pid") else "—",
        "image": status.get("release") or "—", "state": state, "ssh": "ok",
        "python": status.get("python"), "kernel": status.get("kernel"),
        "restart_count": int((previous or {}).get("restart_count") or 0), "oom_killed": False,
    }


async def _check_one(runner: RunnerClient, worker: Worker) -> dict[str, Any]:
    """Сетевая часть проверки сервера. База здесь не трогается: опрос идёт параллельно."""
    try:
        return await runner.call("POST", "/ssh/status", json={"target": target_of(worker)})
    except CoordinatorError as exc:
        return {"error": str(exc), "error_code": getattr(exc, "code", "ssh_error")}


async def reconcile_servers(
    session: AsyncSession, runner: RunnerClient, settings: Settings,
) -> None:
    """Приводит серверы к желаемому состоянию. Заменяет restart policy: агент здесь обычный
    процесс на машине, и упавшего или ещё не установленного поднимает координатор (ТЗ, п. 5.1)."""
    servers = list(await session.scalars(
        select(Worker).where(Worker.provisioning_mode == SSH_MODE)
        .execution_options(populate_existing=True)
    ))
    checks = await asyncio.gather(*(_check_one(runner, w) for w in servers))
    for worker, status in zip(servers, checks, strict=True):
        previous = worker.container_state
        was_running = bool(previous) and previous.get("state") == AGENT_RUNNING
        if "error" in status:
            worker.container_state = {
                **(previous or UNKNOWN_STATE),
                "state": "unreachable", "ssh": status["error_code"], "error": status["error"],
            }
            if worker.status != WorkerStatus.OFFLINE or was_running:
                await events.emit(
                    session, "server.unreachable", "offline", status["error"], level="warning",
                    component="dispatcher", worker_id=worker.id, error_code=status["error_code"],
                )
            worker.status = WorkerStatus.OFFLINE
            continue
        worker.container_state = _state_of(status, previous)
        if not status["running"]:
            worker.status = WorkerStatus.OFFLINE
            if worker.desired_agent_state == AGENT_RUNNING:
                await _bring_up(session, runner, worker, status, settings, was_running)
    await session.commit()


async def _bring_up(
    session: AsyncSession, runner: RunnerClient, worker: Worker, status: dict[str, Any],
    settings: Settings, was_running: bool,
) -> None:
    try:
        if not status["installed"]:
            await deploy_agent(session, runner, worker, settings)
        await runner.call("POST", "/ssh/start", json={"target": target_of(worker)})
    except CoordinatorError as exc:
        worker.container_state = {**worker.container_state, "error": str(exc)}
        await events.emit(
            session, "agent.start", "failed", str(exc), level="error", component="dispatcher",
            worker_id=worker.id, error_code=getattr(exc, "code", "ssh_error"),
        )
        return
    state = dict(worker.container_state)
    if was_running:
        # Агент работал и умер сам: это перезапуск, который виден оператору в предупреждениях.
        state["restart_count"] = int(state.get("restart_count") or 0) + 1
    state["state"] = "starting"
    state.pop("error", None)
    worker.container_state = state
    await events.emit(
        session, "agent.start", "ok",
        f"{worker.name}: агент {'перезапущен после сбоя' if was_running else 'запущен'}",
        level="warning" if was_running else "info", component="dispatcher", worker_id=worker.id,
    )


async def wait_for_registration(
    sessions: Any, worker_id: int, since: Any, limit_seconds: float,
) -> bool:
    """Ждёт, что агент новой версии дошёл до координатора: новая отметка времени от него."""
    deadline = asyncio.get_running_loop().time() + limit_seconds
    while asyncio.get_running_loop().time() < deadline:
        async with sessions() as session:
            seen = await session.scalar(
                select(func.count()).select_from(Worker).where(
                    Worker.id == worker_id, Worker.status == WorkerStatus.ONLINE,
                    Worker.last_heartbeat_at > since,
                )
            )
        if seen:
            return True
        await asyncio.sleep(0.5)
    return False
