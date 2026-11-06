"""HTTP API runner-а. Доступ только по отдельному токену, каждый вызов журналируется.

Запуск: uvicorn coordinator.runner.main:app. Порт наружу не публикуется.
"""
import asyncio
import hmac
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from coordinator.config import Settings
from coordinator.runner.docker import DockerClient, DockerError
from coordinator.runner.service import Runner, RunnerError, WorkerSpec
from coordinator.runner.ssh import SshFailure, SshManager, SshTarget

log = logging.getLogger("coordinator.runner")


class CreateWorker(BaseModel):
    name: str = Field(max_length=63)
    image: str = Field(max_length=255)
    threads: int = Field(ge=1, le=1000)
    cpus: float = Field(ge=0.1, le=64)
    memory_mb: int = Field(ge=64, le=1024 * 1024)
    env: dict[str, str] = {}


class UpdateImage(BaseModel):
    image: str = Field(max_length=255)


class TargetIn(BaseModel):
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=22, ge=1, le=65535)
    user: str = Field(pattern=r"^[a-z_][a-z0-9_-]{0,31}$")
    key_ref: str = Field(max_length=64)
    # Только для первичного входа по паролю; в журнал и ответ не попадает.
    password: str | None = Field(default=None, max_length=256)

    def to_target(self) -> SshTarget:
        return SshTarget(self.host, self.port, self.user, self.key_ref, self.password)


class TargetBody(BaseModel):
    target: TargetIn


class LogsBody(TargetBody):
    tail: int = Field(default=200, ge=1, le=5000)


class DeployBody(TargetBody):
    kind: str = Field(max_length=64)
    env: dict[str, str] = {}


class FileBody(TargetBody):
    path: str = Field(min_length=1, max_length=4096)
    start: int = Field(default=0, ge=0)
    # Пусто — до конца файла.
    length: int | None = Field(default=None, gt=0)


class CreateServer(BaseModel):
    name: str = Field(max_length=63)
    cpus: float = Field(ge=0.1, le=64)
    memory_mb: int = Field(ge=64, le=1024 * 1024)
    key_ref: str = Field(max_length=64)


class Finish(BaseModel):
    full_id: str | None = Field(default=None, max_length=128)
    previous_id: str = Field(max_length=128)


def create_runner_app(
    settings: Settings, docker: DockerClient | None = None, ssh: SshManager | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        client = docker or DockerClient()
        app.state.runner = Runner(
            client, settings.runner_image_patterns(), settings.runner_network,
        )
        app.state.ssh = ssh or SshManager(
            Path(settings.ssh_key_dir), Path(settings.ssh_pin_file),
            Path(settings.ssh_bundles_dir), settings.ssh_host_patterns(),
        )
        try:
            yield
        finally:
            await app.state.ssh.close()
            await client.aclose()

    def check_token(x_runner_token: Annotated[str, Header()] = "") -> None:
        if not settings.runner_token or not hmac.compare_digest(
            x_runner_token, settings.runner_token,
        ):
            raise HTTPException(401, "нужен токен runner-а")

    app = FastAPI(title="Runner", version="0.1.0", lifespan=lifespan)
    http = APIRouter(dependencies=[Depends(check_token)])

    @app.exception_handler(RunnerError)
    async def on_runner_error(_: Request, exc: RunnerError) -> JSONResponse:
        log.warning("отказ: %s %s", exc.code, exc)
        return JSONResponse(
            status_code=exc.status, content={"detail": str(exc), "code": exc.code},
        )

    @app.exception_handler(DockerError)
    async def on_docker_error(_: Request, exc: DockerError) -> JSONResponse:
        log.warning("ошибка docker: %s %s", exc.status, exc)
        return JSONResponse(status_code=502, content={"detail": str(exc), "code": "docker_error"})

    @app.exception_handler(SshFailure)
    async def on_ssh_failure(_: Request, exc: SshFailure) -> JSONResponse:
        log.warning("ssh: %s %s", exc.code, exc)
        return JSONResponse(
            status_code=exc.status, content={"detail": str(exc), "code": exc.code},
        )

    def ssh_of(request: Request) -> SshManager:
        return request.app.state.ssh

    SshDep = Annotated[SshManager, Depends(ssh_of)]

    def runner_of(request: Request) -> Runner:
        return request.app.state.runner

    RunnerDep = Annotated[Runner, Depends(runner_of)]

    @http.get("/containers")
    async def list_containers(runner: RunnerDep) -> list[dict[str, Any]]:
        return await runner.list_containers()

    @http.get("/images")
    async def allowed_images(runner: RunnerDep) -> list[str]:
        return runner.image_patterns

    @http.post("/containers", status_code=201)
    async def create(body: CreateWorker, runner: RunnerDep) -> dict[str, Any]:
        result = await runner.create(WorkerSpec(
            body.name, body.image, body.threads, body.cpus, body.memory_mb, body.env,
        ))
        # Окружение содержит токен агента, в журнал идут только имя и образ.
        log.info("создан контейнер узла %s из образа %s", body.name, body.image)
        return result

    @http.get("/containers/{worker}")
    async def status(worker: str, runner: RunnerDep) -> dict[str, Any]:
        return await runner.status(worker)

    @http.post("/containers/{worker}/start", status_code=204)
    async def start(worker: str, runner: RunnerDep) -> None:
        await runner.start(worker)
        log.info("запущен узел %s", worker)

    @http.post("/containers/{worker}/stop", status_code=204)
    async def stop(worker: str, runner: RunnerDep) -> None:
        await runner.stop(worker)
        log.info("остановлен узел %s", worker)

    @http.post("/containers/{worker}/restart", status_code=204)
    async def restart(worker: str, runner: RunnerDep) -> None:
        await runner.restart(worker)
        log.info("перезапущен узел %s", worker)

    @http.get("/containers/{worker}/logs")
    async def logs(
        worker: str, runner: RunnerDep, tail: Annotated[int, Query(ge=1, le=5000)] = 200,
    ) -> dict[str, str]:
        return {"logs": await runner.logs(worker, tail)}

    @http.post("/containers/{worker}/update-image")
    async def update_image(worker: str, body: UpdateImage, runner: RunnerDep) -> dict[str, str]:
        result = await runner.update_image(worker, body.image)
        log.info("узел %s переведён на образ %s", worker, body.image)
        return result

    @http.post("/updates/commit", status_code=204)
    async def commit_update(body: Finish, runner: RunnerDep) -> None:
        await runner.commit_update(body.previous_id)

    @http.post("/updates/rollback", status_code=204)
    async def rollback_update(body: Finish, runner: RunnerDep) -> None:
        if body.full_id is None:
            raise HTTPException(422, "нужен full_id")
        await runner.rollback_update(body.full_id, body.previous_id)
        log.warning("откат обновления, возвращён контейнер %s", body.previous_id[:12])

    @http.post("/ssh/status")
    async def ssh_status(body: TargetBody, manager: SshDep) -> dict[str, Any]:
        return await manager.status(body.target.to_target())

    @http.post("/ssh/authorize", status_code=204)
    async def ssh_authorize(body: TargetBody, manager: SshDep) -> None:
        await manager.authorize(body.target.to_target())
        log.info("на %s установлен ключ координатора", body.target.host)

    @http.post("/ssh/file")
    async def ssh_file(body: FileBody, manager: SshDep) -> StreamingResponse:
        """Часть файла с сервера для галереи. Какой файл можно читать, решил api."""
        path = PurePosixPath(body.path)
        if not path.is_absolute() or ".." in path.parts:
            raise HTTPException(422, "путь должен быть абсолютным и без ..")
        remote = await manager.open_file(body.target.to_target(), body.path)
        start = min(body.start, remote.size)
        length = remote.size - start
        if body.length is not None:
            length = min(length, body.length)
        return StreamingResponse(
            remote.iterate(start, length),
            media_type="application/octet-stream",
            headers={"X-File-Size": str(remote.size), "Content-Length": str(length)},
        )

    @http.post("/ssh/start", status_code=204)
    async def ssh_start(body: TargetBody, manager: SshDep) -> None:
        await manager.start(body.target.to_target())
        log.info("запущен агент на %s", body.target.host)

    @http.post("/ssh/stop", status_code=204)
    async def ssh_stop(body: TargetBody, manager: SshDep) -> None:
        await manager.stop(body.target.to_target())
        log.info("остановлен агент на %s", body.target.host)

    @http.post("/ssh/restart", status_code=204)
    async def ssh_restart(body: TargetBody, manager: SshDep) -> None:
        await manager.restart(body.target.to_target())
        log.info("перезапущен агент на %s", body.target.host)

    @http.post("/ssh/rollback", status_code=204)
    async def ssh_rollback(body: TargetBody, manager: SshDep) -> None:
        await manager.rollback(body.target.to_target())
        log.warning("откат агента на %s", body.target.host)

    @http.post("/ssh/logs")
    async def ssh_logs(body: LogsBody, manager: SshDep) -> dict[str, str]:
        return {"logs": await manager.logs(body.target.to_target(), body.tail)}

    @http.post("/ssh/deploy")
    async def ssh_deploy(body: DeployBody, manager: SshDep) -> dict[str, str]:
        result = await manager.deploy(body.target.to_target(), body.kind, body.env)
        # Окружение содержит токен агента, в журнал идут только сервер и выпуск.
        log.info("на %s установлен выпуск %s", body.target.host, result["release"])
        return result

    @http.post("/ssh/forget-host-key", status_code=204)
    async def ssh_forget(body: TargetBody, manager: SshDep) -> None:
        manager.forget_host_key(body.target.to_target())

    @http.post("/ssh/servers", status_code=201)
    async def ssh_create_server(
        body: CreateServer, runner: RunnerDep, manager: SshDep,
    ) -> dict[str, str]:
        result = await runner.create_server(
            body.name, settings.ssh_server_image, body.cpus, body.memory_mb,
            manager.public_key(body.key_ref),
        )
        log.info("создан контейнер-сервер %s", body.name)
        return result

    app.include_router(http)

    async def run_bridge(ws: WebSocket, stream: Any, name: str) -> None:
        """Мост между WebSocket и потоком терминала. Соединение уже принято."""
        log.info("открыт терминал %s", name)

        async def output() -> None:
            while chunk := await stream.read():
                await ws.send_bytes(chunk)

        async def input_() -> None:
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    return
                if message.get("bytes"):
                    await stream.write(message["bytes"])
                elif message.get("text"):
                    columns, rows = json.loads(message["text"])["resize"]
                    await stream.resize(int(columns), int(rows))

        tasks = {asyncio.create_task(output()), asyncio.create_task(input_())}
        try:
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        finally:
            await stream.close()
            log.info("закрыт терминал %s", name)
        try:
            await ws.close()
        except RuntimeError:
            pass  # другая сторона уже закрыла соединение

    @app.websocket("/ssh/terminal")
    async def ssh_terminal(ws: WebSocket) -> None:
        """Терминал сервера по SSH: куда подключаться, сообщает первым сообщением api."""
        token = ws.headers.get("x-runner-token", "")
        if not settings.runner_token or not hmac.compare_digest(token, settings.runner_token):
            await ws.close(4401)
            return
        await ws.accept()
        try:
            target = TargetIn.model_validate_json(await ws.receive_text()).to_target()
            stream = await ws.app.state.ssh.open_terminal(target)
        except (SshFailure, ValidationError) as exc:
            log.warning("терминал не открыт: %s", type(exc).__name__)
            await ws.send_text(f"Терминал недоступен: {exc}"[:300])
            await ws.close(4404)
            return
        await run_bridge(ws, stream, target.address)

    @app.websocket("/containers/{worker}/terminal")
    async def terminal(ws: WebSocket, worker: str) -> None:
        """Мост между WebSocket и docker exec. Кому можно — решил api, здесь только токен."""
        token = ws.headers.get("x-runner-token", "")
        if not settings.runner_token or not hmac.compare_digest(token, settings.runner_token):
            await ws.close(4401)
            return
        runner: Runner = ws.app.state.runner
        try:
            stream = await runner.open_terminal(worker)
        except (RunnerError, DockerError) as exc:
            log.warning("терминал не открыт: %s", exc)
            await ws.close(4404)
            return
        await ws.accept()
        await run_bridge(ws, stream, worker)

    return app
