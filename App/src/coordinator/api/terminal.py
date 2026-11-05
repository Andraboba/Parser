"""Шлюз терминала: браузер ↔ api ↔ runner ↔ docker exec. Ключей и токенов браузер не видит."""
import asyncio
import json
import logging
import time
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from sqlalchemy.exc import DBAPIError

from coordinator.api.deps import SESSION_COOKIE
from coordinator.config import Settings
from coordinator.domain.security import Role, mask_secrets, role_allows
from coordinator.domain.terminal import CommandLine
from coordinator.errors import CoordinatorError
from coordinator.provisioning import SSH_MODE, target_of
from coordinator.storage import users
from coordinator.storage.models import Worker

router = APIRouter()
log = logging.getLogger("coordinator.terminal")

CLOSE_FORBIDDEN = 4403
CLOSE_UNAUTHORIZED = 4401
CLOSE_NOT_FOUND = 4404
_MAX_RESIZE = 1000


def same_origin(ws: WebSocket) -> bool:
    """Браузер приложит cookie к WebSocket с чужого сайта, а SameSite это не везде закрывает."""
    origin = ws.headers.get("origin")
    if not origin:
        return False
    expected = ws.headers.get("x-forwarded-host") or ws.headers.get("host")
    return urlsplit(origin).netloc == expected


async def _audit(ws: WebSocket, actor: str, action: str, target: str, result: str) -> None:
    try:
        async with ws.app.state.sessionmaker() as session:
            await users.record_audit(
                session, actor, action, target, result, ws.client.host if ws.client else None,
            )
    except DBAPIError as exc:
        log.error("запись аудита терминала не удалась: %s", type(exc).__name__)


async def _to_user(backend: Any, ws: WebSocket) -> None:
    async for message in backend:
        if isinstance(message, bytes):
            await ws.send_bytes(message)
        else:
            await ws.send_text(message)


async def _from_user(
    ws: WebSocket, backend: Any, actor: str, worker: str, settings: Settings,
) -> str:
    """Возвращает причину завершения. Неактивность считается только по вводу пользователя."""
    parser = CommandLine()
    deadline = time.monotonic() + settings.terminal_max_seconds
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return "max_duration"
        try:
            async with asyncio.timeout(min(settings.terminal_idle_seconds, left)):
                message = await ws.receive()
        except TimeoutError:
            return "idle" if deadline - time.monotonic() > 0 else "max_duration"
        if message["type"] == "websocket.disconnect":
            return "closed"
        text = message.get("text")
        if text is not None:
            await _control(backend, text)
            continue
        result = parser.feed(message.get("bytes") or b"")
        for command in result.commands:
            await _audit(ws, actor, "terminal.command", worker, mask_secrets(command)[:200])
        for command, reason in result.denied:
            await _audit(ws, actor, "terminal.denied", worker, mask_secrets(command)[:200])
            await ws.send_text(f"Команда запрещена политикой: {reason}")
        if result.forward:
            await backend.send(result.forward)


async def _control(backend: Any, text: str) -> None:
    """Управление от браузера: только размер окна, остальное отбрасывается."""
    try:
        size = json.loads(text).get("resize")
        columns, rows = int(size[0]), int(size[1])
    except (ValueError, TypeError, IndexError, AttributeError):
        return
    if 0 < columns <= _MAX_RESIZE and 0 < rows <= _MAX_RESIZE:
        await backend.send(json.dumps({"resize": [columns, rows]}))


@router.websocket("/api/workers/{worker_id}/terminal")
async def terminal(ws: WebSocket, worker_id: int) -> None:
    state = ws.app.state
    settings: Settings = state.settings
    if not same_origin(ws):
        await ws.close(CLOSE_FORBIDDEN)
        return
    token = ws.cookies.get(SESSION_COOKIE)
    async with state.sessionmaker() as session:
        user = await users.find_session(session, token) if token else None
        worker = await session.get(Worker, worker_id) if user else None
    if user is None:
        await ws.close(CLOSE_UNAUTHORIZED)
        return
    target = f"worker_id={worker_id}"
    if not role_allows(user.role, Role.ADMINISTRATOR):
        await _audit(ws, user.login, "terminal.open", target, "403")
        await ws.close(CLOSE_FORBIDDEN)
        return
    if worker is None:
        await ws.close(CLOSE_NOT_FOUND)
        return

    await ws.accept()
    await _audit(ws, user.login, "terminal.open", target, "accepted")
    reason = "error"
    try:
        opener = (
            state.runner.ssh_terminal(target_of(worker))
            if worker.provisioning_mode == SSH_MODE else state.runner.terminal(worker.name)
        )
        async with opener as backend:
            output = asyncio.create_task(_to_user(backend, ws))
            source = asyncio.create_task(_from_user(ws, backend, user.login, target, settings))
            done, pending = await asyncio.wait(
                {output, source}, return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            reason = source.result() if source in done else "shell_exited"
    except CoordinatorError as exc:
        await ws.send_text(f"Терминал недоступен: {exc}")
    except (OSError, WebSocketDisconnect) as exc:
        log.warning("соединение с терминалом прервано: %s", type(exc).__name__)
    finally:
        await _audit(ws, user.login, "terminal.close", target, reason)
    notices = {"idle": "Сессия завершена из-за неактивности.",
               "max_duration": "Сессия завершена: истёк срок её действия."}
    if reason in notices:
        await ws.send_text(notices[reason])
    try:
        await ws.close(status.WS_1000_NORMAL_CLOSURE)
    except RuntimeError:
        pass  # браузер уже закрыл соединение
