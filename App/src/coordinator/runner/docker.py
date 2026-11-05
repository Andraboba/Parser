"""Узкий клиент Docker Engine API поверх unix-сокета. Здесь только нужные runner-у вызовы."""
import asyncio
import json
from typing import Any

import httpx

SOCKET_PATH = "/var/run/docker.sock"


class DockerError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def demux_logs(raw: bytes) -> str:
    """Журнал контейнера без TTY приходит кадрами: 8 байт заголовка, в них длина кадра."""
    out = bytearray()
    position = 0
    while position + 8 <= len(raw):
        size = int.from_bytes(raw[position + 4:position + 8], "big")
        out += raw[position + 8:position + 8 + size]
        position += 8 + size
    return out.decode("utf-8", errors="replace")


class DockerClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(
            transport=transport or httpx.AsyncHTTPTransport(uds=SOCKET_PATH),
            base_url="http://docker", timeout=60.0,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = await self._client.request(method, path, **kwargs)
        if response.status_code >= 400:
            try:
                message = response.json().get("message", response.text)
            except ValueError:
                message = response.text
            raise DockerError(response.status_code, message[:500])
        return response

    async def list_by_label(self, label: str, value: str | None = None) -> list[dict[str, Any]]:
        selector = label if value is None else f"{label}={value}"
        response = await self._call("GET", "/containers/json", params={
            "all": "true", "filters": json.dumps({"label": [selector]}),
        })
        return response.json()

    async def inspect(self, container_id: str) -> dict[str, Any]:
        return (await self._call("GET", f"/containers/{container_id}/json")).json()

    async def start(self, container_id: str) -> None:
        await self._call("POST", f"/containers/{container_id}/start")

    async def stop(self, container_id: str, grace_seconds: int = 15) -> None:
        await self._call("POST", f"/containers/{container_id}/stop", params={"t": grace_seconds})

    async def restart(self, container_id: str, grace_seconds: int = 15) -> None:
        await self._call(
            "POST", f"/containers/{container_id}/restart", params={"t": grace_seconds},
        )

    async def remove(self, container_id: str) -> None:
        await self._call("DELETE", f"/containers/{container_id}", params={"force": "true"})

    async def rename(self, container_id: str, name: str) -> None:
        await self._call("POST", f"/containers/{container_id}/rename", params={"name": name})

    async def create(self, name: str, config: dict[str, Any]) -> str:
        response = await self._call(
            "POST", "/containers/create", params={"name": name}, json=config,
        )
        return response.json()["Id"]

    async def logs(self, container_id: str, tail: int) -> str:
        response = await self._call("GET", f"/containers/{container_id}/logs", params={
            "stdout": "true", "stderr": "true", "tail": tail, "timestamps": "true",
        })
        return demux_logs(response.content)

    async def create_exec(self, container_id: str, command: list[str]) -> str:
        response = await self._call("POST", f"/containers/{container_id}/exec", json={
            "AttachStdin": True, "AttachStdout": True, "AttachStderr": True, "Tty": True,
            "Cmd": command, "Env": ["TERM=xterm-256color"],
        })
        return response.json()["Id"]

    async def resize_exec(self, exec_id: str, columns: int, rows: int) -> None:
        await self._call("POST", f"/exec/{exec_id}/resize", params={"h": rows, "w": columns})

    async def attach_exec(self, exec_id: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Docker переключает соединение на сырой поток через Upgrade, httpx этого не умеет."""
        reader, writer = await asyncio.open_unix_connection(SOCKET_PATH)
        body = json.dumps({"Detach": False, "Tty": True}).encode()
        writer.write(
            f"POST /exec/{exec_id}/start HTTP/1.1\r\nHost: docker\r\n"
            f"Content-Type: application/json\r\nConnection: Upgrade\r\nUpgrade: tcp\r\n"
            f"Content-Length: {len(body)}\r\n\r\n".encode() + body
        )
        await writer.drain()
        head = await reader.readuntil(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n", 1)[0] and b" 200 " not in head.split(b"\r\n", 1)[0]:
            writer.close()
            raise DockerError(502, head.split(b"\r\n", 1)[0].decode(errors="replace"))
        return reader, writer

    async def ensure_image(self, image: str) -> None:
        try:
            await self._call("GET", f"/images/{image}/json")
        except DockerError as exc:
            if exc.status != 404:
                raise
            await self._call(
                "POST", "/images/create", params={"fromImage": image}, timeout=600.0,
            )
