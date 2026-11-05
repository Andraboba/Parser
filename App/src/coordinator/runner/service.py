"""Операции над контейнерами агентов и ограничения на них.

Разрешён только узкий набор действий и только над контейнерами с меткой lab.worker. Запуск
привилегированных контейнеров, произвольные тома и команды сюда просто не заложены.
"""
import fnmatch
import re
from dataclasses import dataclass
from typing import Any

from coordinator.runner.docker import DockerClient, DockerError

WORKER_LABEL = "lab.worker"
SERVER_LABEL = "lab.server"
MANAGED_LABEL = "lab.managed"
NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
# Что можно передать контейнеру агента. Остальное (LD_PRELOAD, PATH и прочее) отбрасывается.
ALLOWED_ENV = re.compile(r"(WORKER|AGENT|STUB|COORD)_[A-Z0-9_]+")


class RunnerError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def image_allowed(image: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(image, pattern) for pattern in patterns)


def filter_env(env: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in env.items() if ALLOWED_ENV.fullmatch(k)}


@dataclass(frozen=True)
class WorkerSpec:
    name: str
    image: str
    threads: int
    cpus: float
    memory_mb: int
    env: dict[str, str]


def _summary(container: dict[str, Any]) -> dict[str, Any]:
    return {
        "worker": container["Labels"].get(WORKER_LABEL),
        "id": container["Id"][:12],
        "image": container["Image"],
        "state": container["State"],
        "status": container.get("Status"),
    }


class TerminalStream:
    """Сеанс docker exec с псевдотерминалом. Команда только оболочка, пользователь — из образа."""

    def __init__(self, docker: DockerClient, exec_id: str, reader: Any, writer: Any) -> None:
        self.docker = docker
        self.exec_id = exec_id
        self.reader = reader
        self.writer = writer

    async def read(self) -> bytes:
        return await self.reader.read(65536)

    async def write(self, data: bytes) -> None:
        self.writer.write(data)
        await self.writer.drain()

    async def resize(self, columns: int, rows: int) -> None:
        await self.docker.resize_exec(self.exec_id, columns, rows)

    async def close(self) -> None:
        self.writer.close()


class Runner:
    def __init__(self, docker: DockerClient, image_patterns: list[str], network: str) -> None:
        self.docker = docker
        self.image_patterns = image_patterns
        self.network = network

    def _require_image(self, image: str) -> None:
        if not image_allowed(image, self.image_patterns):
            raise RunnerError(403, "image_not_allowed", f"образ {image} вне списка разрешённых")

    async def _container(self, worker: str) -> dict[str, Any]:
        found = await self.docker.list_by_label(WORKER_LABEL, worker)
        if not found:
            raise RunnerError(404, "container_not_found", f"контейнер узла {worker} не найден")
        return found[0]

    async def list_containers(self) -> list[dict[str, Any]]:
        details = []
        for container in await self.docker.list_by_label(WORKER_LABEL):
            summary = _summary(container)
            inspected = await self.docker.inspect(container["Id"])
            summary["restart_count"] = inspected.get("RestartCount", 0)
            summary["oom_killed"] = inspected.get("State", {}).get("OOMKilled", False)
            summary["started_at"] = inspected.get("State", {}).get("StartedAt")
            details.append(summary)
        return details

    async def status(self, worker: str) -> dict[str, Any]:
        for summary in await self.list_containers():
            if summary["worker"] == worker:
                return summary
        raise RunnerError(404, "container_not_found", f"контейнер узла {worker} не найден")

    async def start(self, worker: str) -> None:
        await self.docker.start((await self._container(worker))["Id"])

    async def stop(self, worker: str) -> None:
        await self.docker.stop((await self._container(worker))["Id"])

    async def restart(self, worker: str) -> None:
        await self.docker.restart((await self._container(worker))["Id"])

    async def logs(self, worker: str, tail: int) -> str:
        return await self.docker.logs((await self._container(worker))["Id"], tail)

    async def create(self, spec: WorkerSpec) -> dict[str, Any]:
        if not NAME_PATTERN.fullmatch(spec.name):
            raise RunnerError(422, "invalid_name", "имя узла: строчные буквы, цифры и дефис")
        self._require_image(spec.image)
        if await self.docker.list_by_label(WORKER_LABEL, spec.name):
            raise RunnerError(409, "already_exists", f"узел {spec.name} уже существует")
        env = {
            **filter_env(spec.env), "WORKER_NAME": spec.name, "AGENT_NAME": spec.name,
            "WORKER_THREADS": str(spec.threads), "AGENT_THREADS": str(spec.threads),
            "AGENT_IMAGE": spec.image, "SSL_CERT_FILE": "/certs/ca.crt",
        }
        await self.docker.ensure_image(spec.image)
        container_id = await self.docker.create(f"lab-{spec.name}", {
            "Image": spec.image,
            "Env": [f"{k}={v}" for k, v in env.items()],
            "Labels": {WORKER_LABEL: spec.name, MANAGED_LABEL: "true"},
            "HostConfig": {
                "NanoCpus": int(spec.cpus * 1e9),
                "Memory": spec.memory_mb * 1024**2,
                "RestartPolicy": {"Name": "unless-stopped"},
                "Binds": [f"lab-videos-{spec.name}:/data/videos", "coord-tls-ca:/certs:ro"],
                "NetworkMode": self.network,
                "Privileged": False,
                # Корень только для чтения, запись — в том и /tmp; лимит процессов.
                "ReadonlyRootfs": True,
                "Tmpfs": {"/tmp": "rw,noexec,nosuid,size=64m"},
                "PidsLimit": 256,
            },
        })
        await self.docker.start(container_id)
        return {"worker": spec.name, "id": container_id[:12], "image": spec.image}

    async def create_server(
        self, name: str, image: str, cpus: float, memory_mb: int, public_key: str,
    ) -> dict[str, str]:
        """Новый контейнер-сервер. Дальше координатор работает с ним только по SSH."""
        if not NAME_PATTERN.fullmatch(name):
            raise RunnerError(422, "invalid_name", "имя сервера: строчные буквы, цифры и дефис")
        if await self.docker.list_by_label(SERVER_LABEL, name):
            raise RunnerError(409, "already_exists", f"сервер {name} уже существует")
        host = f"lab-{name}"
        await self.docker.ensure_image(image)
        container_id = await self.docker.create(host, {
            "Image": image,
            "Hostname": host,
            "Env": [f"SSH_AUTHORIZED_KEY={public_key}"],
            "Labels": {SERVER_LABEL: name, MANAGED_LABEL: "true"},
            "HostConfig": {
                "NanoCpus": int(cpus * 1e9),
                "Memory": memory_mb * 1024**2,
                "RestartPolicy": {"Name": "unless-stopped"},
                "Init": True,
                "Binds": [
                    f"lab-videos-{name}:/data/videos", f"lab-sshkeys-{name}:/etc/ssh/keys",
                    "coord-tls-ca:/certs:ro",
                ],
                "NetworkMode": self.network,
                "Privileged": False,
                "PidsLimit": 256,
            },
        })
        await self.docker.start(container_id)
        return {"server": name, "host": host, "id": container_id[:12]}

    async def update_image(self, worker: str, image: str) -> dict[str, str]:
        """Шаг 1 обновления: новый контейнер из нового образа. Старый только остановлен, поэтому
        откат быстрый. Что делать дальше, решает координатор: зарегистрировался ли агент."""
        self._require_image(image)
        await self.docker.ensure_image(image)
        current = await self._container(worker)
        inspected = await self.docker.inspect(current["Id"])
        name = inspected["Name"].lstrip("/")
        # От прежнего контейнера берём только настройки оператора. Команда, переменные вроде
        # PATH и пользователь заданы образом: новый образ должен запуститься со своими.
        env = filter_env(dict(
            e.split("=", 1) for e in inspected["Config"].get("Env") or [] if "=" in e
        ))
        if "AGENT_IMAGE" in env:
            env["AGENT_IMAGE"] = image
        config: dict[str, Any] = {
            "Image": image,
            "Env": [f"{k}={v}" for k, v in env.items()],
            "Labels": inspected["Config"].get("Labels") or {},
            "HostConfig": inspected["HostConfig"],
        }
        networks = inspected.get("NetworkSettings", {}).get("Networks") or {}
        config["NetworkingConfig"] = {"EndpointsConfig": {
            net: {"Aliases": value.get("Aliases") or []} for net, value in networks.items()
        }}

        await self.docker.stop(current["Id"])
        previous_name = f"{name}-previous"
        await self.docker.rename(current["Id"], previous_name)
        try:
            container_id = await self.docker.create(name, config)
            await self.docker.start(container_id)
        except DockerError as exc:
            await self.docker.rename(current["Id"], name)
            await self.docker.start(current["Id"])
            raise RunnerError(502, "update_failed", f"откат: {exc}") from exc
        return {"id": container_id[:12], "full_id": container_id, "previous_id": current["Id"]}

    async def open_terminal(self, worker: str) -> TerminalStream:
        container = await self._container(worker)
        if container["State"] != "running":
            raise RunnerError(409, "container_not_running", f"контейнер {worker} не запущен")
        exec_id = await self.docker.create_exec(container["Id"], ["/bin/sh"])
        reader, writer = await self.docker.attach_exec(exec_id)
        return TerminalStream(self.docker, exec_id, reader, writer)

    async def commit_update(self, previous_id: str) -> None:
        await self.docker.remove(previous_id)

    async def rollback_update(self, full_id: str, previous_id: str) -> None:
        inspected = await self.docker.inspect(full_id)
        name = inspected["Name"].lstrip("/")
        await self.docker.remove(full_id)
        await self.docker.rename(previous_id, name)
        await self.docker.start(previous_id)
