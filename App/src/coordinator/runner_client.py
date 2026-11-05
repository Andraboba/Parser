"""Клиент runner-а для api и dispatcher. Сокета Docker у них нет: только этот узкий HTTP-API."""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosedError, InvalidHandshake

from coordinator.errors import CoordinatorError


class RunnerUnavailable(CoordinatorError):
    status = 503
    code = "runner_unavailable"


class RunnerRejected(CoordinatorError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class RunnerClient:
    def __init__(
        self, base_url: str, token: str, transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.configured = bool(base_url)
        self._base_url = base_url
        self._token = token
        self._client = httpx.AsyncClient(
            base_url=base_url or "http://runner.invalid", transport=transport, timeout=120.0,
            headers={"X-Runner-Token": token},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    @asynccontextmanager
    async def terminal(self, worker: str) -> AsyncIterator[Any]:
        """WebSocket до runner-а. Токен знает только сервер: браузер его никогда не видит."""
        if not self.configured:
            raise RunnerUnavailable("runner не настроен: контейнеры управляются через compose")
        url = self._base_url.replace("http", "ws", 1).rstrip("/")
        try:
            async with connect(
                f"{url}/containers/{worker}/terminal", max_size=None,
                additional_headers={"X-Runner-Token": self._token},
            ) as connection:
                yield connection
        except (OSError, InvalidHandshake, ConnectionClosedError) as exc:
            raise RunnerUnavailable(f"терминал недоступен: {type(exc).__name__}") from exc

    async def call(self, method: str, path: str, **kwargs: Any) -> Any:
        if not self.configured:
            raise RunnerUnavailable("runner не настроен: контейнеры управляются через compose")
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise RunnerUnavailable(f"runner недоступен: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            try:
                body = response.json()
            except ValueError:
                body = {}
            raise RunnerRejected(
                response.status_code, str(body.get("code", "runner_error")),
                str(body.get("detail", "ошибка runner-а")),
            )
        return response.json() if response.content else None
