# -*- coding: utf-8 -*-
"""HTTP-клиент с пулом соединений, тайм-аутами и ограничением частоты.

Стандартная библиотека: внешние зависимости агенту не нужны.
Клиент не повторяет запросы сам — решение о повторе принимает слой,
который знает смысл ответа (адаптер сайта или клиент координатора).
"""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import threading
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import urljoin, urlsplit

from .errors import NetworkError
from .ratelimit import AdaptiveLimiter

__all__ = ["HttpClient", "Response", "url_host"]

_REDIRECT_STATUSES = (301, 302, 303, 307, 308)


def url_host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


class Response:
    """Ответ сервера: либо читается целиком, либо потоком."""

    def __init__(
        self,
        client: "HttpClient",
        key: tuple[str, str, int],
        connection: http.client.HTTPConnection,
        raw: http.client.HTTPResponse,
        url: str,
        on_close: "Callable[[], None] | None" = None,
    ) -> None:
        self._client = client
        self._key = key
        self._connection = connection
        self._raw = raw
        self._on_close = on_close
        self._finished = False
        self.url = url
        self.status = raw.status
        self.reason = raw.reason
        self.headers: dict[str, str] = {}
        for name, value in raw.getheaders():
            lowered = name.lower()
            if lowered in self.headers:
                self.headers[lowered] = f"{self.headers[lowered]}, {value}"
            else:
                self.headers[lowered] = value
        self._consumed = False

    # -- чтение ---------------------------------------------------------
    def read(self, amount: int) -> bytes:
        return self._raw.read(amount)

    def read_all(self, limit: int | None = None) -> bytes:
        if limit is None:
            data = self._raw.read()
        else:
            data = self._raw.read(limit + 1)
            if len(data) > limit:
                raise NetworkError(f"ответ больше предела {limit} байт: {self.url}")
        self._consumed = True
        return data

    def iter_chunks(self, size: int = 256 * 1024) -> Iterator[bytes]:
        while True:
            chunk = self._raw.read(size)
            if not chunk:
                break
            yield chunk
        self._consumed = True

    def header(self, name: str, default: str | None = None) -> str | None:
        return self.headers.get(name.lower(), default)

    def content_length(self) -> int | None:
        value = self.headers.get("content-length")
        if value is None:
            return None
        try:
            return int(value)
        except ValueError:
            return None

    # -- завершение -----------------------------------------------------
    def close(self) -> None:
        if self._finished:
            return
        self._finished = True
        try:
            if not self._consumed and not self._raw.isclosed():
                # недочитанный ответ нельзя вернуть в пул
                self._client.discard(self._key, self._connection)
                self._connection.close()
            elif self._raw.isclosed():
                self._client.release(self._key, self._connection)
            else:
                self._raw.close()
                self._client.release(self._key, self._connection)
        except Exception:  # noqa: BLE001 - закрытие не должно падать
            self._client.discard(self._key, self._connection)
            self._connection.close()
        finally:
            if self._on_close is not None:
                self._on_close()

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class HttpClient:
    """Пул соединений на хост с ограничением частоты и параллельности."""

    def __init__(
        self,
        timeout: float = 20.0,
        max_connections: int = 16,
        limiter: AdaptiveLimiter | None = None,
        verify_tls: bool = True,
        max_body_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        self.timeout = timeout
        self.max_connections = max(1, max_connections)
        self.max_body_bytes = max_body_bytes
        self.limiter = limiter
        self._verify_tls = verify_tls
        self._lock = threading.Lock()
        self._idle: dict[tuple[str, str, int], list[http.client.HTTPConnection]] = {}
        self._semaphore = threading.BoundedSemaphore(self.max_connections)
        self._ssl_context: ssl.SSLContext | None = None
        self._closed = False

    # -- пул ------------------------------------------------------------
    def _context(self) -> ssl.SSLContext:
        if self._ssl_context is None:
            context = ssl.create_default_context()
            if not self._verify_tls:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            self._ssl_context = context
        return self._ssl_context

    def _acquire_connection(
        self, key: tuple[str, str, int]
    ) -> http.client.HTTPConnection:
        with self._lock:
            bucket = self._idle.get(key)
            while bucket:
                candidate = bucket.pop()
                if candidate.sock is not None:
                    return candidate
                candidate.close()
        scheme, host, port = key
        if scheme == "https":
            return http.client.HTTPSConnection(
                host, port, timeout=self.timeout, context=self._context()
            )
        return http.client.HTTPConnection(host, port, timeout=self.timeout)

    def release(self, key: tuple[str, str, int], connection: http.client.HTTPConnection) -> None:
        if self._closed:
            connection.close()
            return
        with self._lock:
            bucket = self._idle.setdefault(key, [])
            if len(bucket) < self.max_connections:
                bucket.append(connection)
                return
        connection.close()

    def discard(self, key: tuple[str, str, int], connection: http.client.HTTPConnection) -> None:
        with self._lock:
            bucket = self._idle.get(key)
            if bucket and connection in bucket:
                bucket.remove(connection)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for bucket in self._idle.values():
                for connection in bucket:
                    try:
                        connection.close()
                    except Exception:  # noqa: BLE001
                        pass
                bucket.clear()
            self._idle.clear()

    # -- запросы --------------------------------------------------------
    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        timeout: float | None = None,
        follow_redirects: bool = False,
        allowed_hosts: tuple[str, ...] = (),
        max_redirects: int = 5,
    ) -> Response:
        current = url
        for _ in range(max_redirects + 1):
            response = self._single_request(method, current, headers, body, timeout)
            if not follow_redirects or response.status not in _REDIRECT_STATUSES:
                return response
            location = response.header("location")
            if not location:
                return response
            target = urljoin(current, location)
            host = url_host(target)
            if allowed_hosts and host not in allowed_hosts:
                response.close()
                raise NetworkError(f"редирект на недопустимый хост: {host}")
            response.read_all(limit=64 * 1024)
            response.close()
            if response.status in (301, 302, 303) and method not in ("GET", "HEAD"):
                method = "GET"
                body = None
            current = target
        raise NetworkError(f"слишком много редиректов: {url}")

    def _single_request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str] | None,
        body: bytes | None,
        timeout: float | None,
    ) -> Response:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise NetworkError(f"неподдерживаемая схема адреса: {url}")
        host = parts.hostname or ""
        port = parts.port or (443 if parts.scheme == "https" else 80)
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"

        key = (parts.scheme, host, port)
        if self.limiter is not None:
            self.limiter.acquire()

        request_headers = {
            "Host": parts.netloc,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "Connection": "keep-alive",
        }
        if headers:
            request_headers.update(dict(headers))
        if body is not None:
            request_headers["Content-Length"] = str(len(body))

        acquired = self._semaphore.acquire(timeout=self.timeout)
        if not acquired:
            raise NetworkError(f"нет свободных соединений: {url}")

        raw: http.client.HTTPResponse | None = None
        connection: http.client.HTTPConnection | None = None
        for attempt in range(2):
            connection = self._acquire_connection(key)
            try:
                connection.timeout = timeout if timeout is not None else self.timeout
                connection.request(method.upper(), path, body=body, headers=request_headers)
                raw = connection.getresponse()
                break
            except (TimeoutError, socket.timeout) as exc:
                connection.close()
                self._semaphore.release()
                if self.limiter is not None:
                    self.limiter.on_throttled()
                raise NetworkError(f"тайм-аут запроса: {url}") from exc
            except (ConnectionResetError, http.client.RemoteDisconnected, BrokenPipeError) as exc:
                # соединение из пула могло устареть: одна попытка с новым
                connection.close()
                if attempt == 0:
                    continue
                self._semaphore.release()
                raise NetworkError(f"соединение сброшено: {url}") from exc
            except (http.client.HTTPException, OSError) as exc:
                connection.close()
                self._semaphore.release()
                raise NetworkError(f"сетевая ошибка для {url}: {exc}") from exc

        if raw is None or connection is None:
            self._semaphore.release()
            raise NetworkError(f"не удалось выполнить запрос: {url}")

        return Response(
            self, key, connection, raw, url, on_close=self._semaphore.release
        )

    # -- удобные обёртки ------------------------------------------------
    def get(
        self,
        url: str,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        follow_redirects: bool = False,
        allowed_hosts: tuple[str, ...] = (),
        max_body_bytes: int | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        """GET с чтением тела целиком: (статус, заголовки, тело)."""
        limit = max_body_bytes if max_body_bytes is not None else self.max_body_bytes
        response = self.request(
            "GET",
            url,
            headers=headers,
            timeout=timeout,
            follow_redirects=follow_redirects,
            allowed_hosts=allowed_hosts,
        )
        try:
            payload = response.read_all(limit=limit)
            return response.status, response.headers, payload
        finally:
            response.close()

    def post_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers = {"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"}
        if headers:
            request_headers.update(dict(headers))
        response = self.request("POST", url, headers=request_headers, body=body, timeout=timeout)
        try:
            data = response.read_all(limit=self.max_body_bytes)
            return response.status, response.headers, data
        finally:
            response.close()

    def get_json(
        self,
        url: str,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        status, response_headers, payload = self.get(url, headers=headers, timeout=timeout)
        try:
            data = json.loads(payload.decode("utf-8")) if payload else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NetworkError(f"ответ не является JSON: {url}") from exc
        return status, data, response_headers
