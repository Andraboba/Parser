# -*- coding: utf-8 -*-
"""Мок-сервисы для тестов и демонстрации.

* ``MockSite`` — управляемый «целевой сайт»: отдаёт страницы с материалом,
  отсутствующие страницы, 403, 429, 500 и файлы с поддержкой Range.
* ``MockCoordinator`` — заглушка координатора: реализует контракт
  register/heartbeat/lease/progress/complete/fail и записывает вызовы.

Стенд нужен, чтобы проверять агент без обращения к настоящему сайту.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

__all__ = ["MockSite", "MockCoordinator"]

VIDEO_PAYLOAD = b"KINESCOPE-TEST-VIDEO-" + b"x" * 32768


class _QuietHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: Any) -> None:  # noqa: D401 - подавляем вывод в тестах
        return


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def _send(handler: BaseHTTPRequestHandler, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
    handler.send_response(status)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    for key, value in (headers or {}).items():
        handler.send_header(key, value)
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(body)


class MockSite:
    """Тестовый сайт с предсказуемым поведением по идентификатору."""

    def __init__(self, video: bytes = VIDEO_PAYLOAD, support_range: bool = True) -> None:
        self.video = video
        self.support_range = support_range
        self.requests: list[tuple[str, str]] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- жизнь сервера ---------------------------------------------------
    def start(self) -> str:
        site = self

        class Handler(_QuietHandler):
            def do_GET(self) -> None:  # noqa: N802 - имя задано базовым классом
                site._handle(self)

            def do_HEAD(self) -> None:  # noqa: N802
                site._handle(self)

        self._server = _QuietServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.url

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("сервер не запущен")
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> "MockSite":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # -- маршруты --------------------------------------------------------
    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        parsed = urlsplit(handler.path)
        self.requests.append((handler.command, parsed.path))

        if parsed.path == "/health":
            _send(handler, 200, b'{"status":"ok"}', {"Content-Type": "application/json"})
            return

        if parsed.path.startswith("/video/"):
            self._serve_video(handler, parsed.path)
            return

        if parsed.path.startswith("/page/"):
            item = parsed.path.rsplit("/", 1)[-1]
            self._serve_page(handler, item)
            return

        _send(handler, 404, b"<html><title>404</title>not found</html>")

    def _serve_page(self, handler: BaseHTTPRequestHandler, item: str) -> None:
        try:
            number = int(item)
        except ValueError:
            _send(handler, 404, b"<html><title>bad id</title></html>")
            return

        variant = number % 10
        if variant == 0:
            _send(handler, 404, b"<html><title>not found</title>page not found</html>")
            return
        if variant == 1:
            _send(handler, 403, b"<html><title>forbidden</title>access denied</html>")
            return
        if variant == 2:
            _send(
                handler,
                429,
                b"<html><title>slow down</title></html>",
                {"Retry-After": "1"},
            )
            return
        if variant == 3:
            _send(handler, 500, b"<html><title>error</title></html>")
            return
        if variant == 4:
            _send(
                handler,
                200,
                "<html><head><title>Видео</title></head><body>страница не найдена</body></html>".encode(
                    "utf-8"
                ),
            )
            return

        page_url = f"{self.url}/page/{number}"
        video_url = f"{self.url}/video/{number}.mp4"
        title = f"Материал {number}"
        html = f"""<!doctype html>
<html><head>
  <meta charset="utf-8">
  <title>{title}</title>
  <meta property="og:title" content="{title}">
</head><body>
  <h1>{title}</h1>
  <script type="application/ld+json">
  {{"@type":"VideoObject","name":"{title}","contentUrl":"{video_url}",
    "size":{len(self.video)},"uploadDate":"2026-01-01"}}
  </script>
  <a href="{page_url}">источник</a>
</body></html>"""
        _send(handler, 200, html.encode("utf-8"))

    def _serve_video(self, handler: BaseHTTPRequestHandler, path: str) -> None:
        data = self.video
        total = len(data)
        range_header = handler.headers.get("Range")
        if range_header and self.support_range and range_header.startswith("bytes="):
            try:
                start = int(range_header[6:].split("-", 1)[0] or 0)
            except ValueError:
                start = 0
            start = max(0, min(start, total))
            body = data[start:]
            status = 206
            headers = {
                "Content-Range": f"bytes {start}-{total - 1}/{total}",
                "Accept-Ranges": "bytes",
                "Content-Type": "video/mp4",
            }
        else:
            body = data
            status = 200
            headers = {"Content-Type": "video/mp4"}
            if self.support_range:
                headers["Accept-Ranges"] = "bytes"
        handler.send_response(status)
        handler.send_header("Content-Length", str(len(body)))
        for key, value in headers.items():
            handler.send_header(key, value)
        handler.end_headers()
        if handler.command != "HEAD":
            handler.wfile.write(body)


class MockCoordinator:
    """Заглушка координатора, реализующая контракт агентского API."""

    def __init__(
        self,
        chunk: dict[str, Any] | None = None,
        token: str | None = None,
        fail_first_lease: int = 0,
    ) -> None:
        self.token = token
        self.chunk = chunk or {
            "chunk_id": 1,
            "job_id": 1,
            "range_start": 100,
            "range_end": 104,
            "next_id": 100,
            "lease_ttl": 30,
            "rps_share": 10,
            # Параметры задания, которые координатор отдаёт в lease.
            "request_timeout_seconds": 20,
            "max_retries": 2,
        }
        self.fail_first_lease = fail_first_lease
        self.registered: list[dict[str, Any]] = []
        self.heartbeats: list[dict[str, Any]] = []
        self.progress: list[dict[str, Any]] = []
        self.completed: list[dict[str, Any]] = []
        self.failed: list[dict[str, Any]] = []
        self.lease_calls = 0
        self.results: list[dict[str, Any]] = []
        self._lease_served = False
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- жизнь сервера ---------------------------------------------------
    def start(self) -> str:
        coordinator = self

        class Handler(_QuietHandler):
            def do_GET(self) -> None:  # noqa: N802
                coordinator._route(self, "GET")

            def do_POST(self) -> None:  # noqa: N802
                coordinator._route(self, "POST")

        self._server = _QuietServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.url

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("сервер не запущен")
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> "MockCoordinator":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # -- маршрутизация ---------------------------------------------------
    def _route(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        parsed = urlsplit(handler.path)
        path = parsed.path

        if method == "GET" and path == "/health":
            self._json(handler, 200, {"status": "ok"})
            return

        if method != "POST":
            self._json(handler, 405, {"error": "method not allowed"})
            return

        if self.token is not None and handler.headers.get("X-Agent-Token") != self.token:
            self._json(handler, 401, {"error": "bad token"})
            return

        body = self._read_json(handler)
        with self._lock:
            if path == "/api/workers/register":
                self.registered.append(body)
                self._json(
                    handler,
                    201,
                    {"worker_id": "w-test", "heartbeat_interval": 0.2, "lease_ttl": 30},
                )
                return

            if path.endswith("/heartbeat"):
                self.heartbeats.append(body)
                self._json(handler, 200, {"ok": True})
                return

            if path.endswith("/lease"):
                self.lease_calls += 1
                if self.fail_first_lease and self.lease_calls <= self.fail_first_lease:
                    self._json(handler, 503, {"error": "busy"})
                    return
                if self._lease_served:
                    self._json(handler, 204, {})
                    return
                self._lease_served = True
                self._json(handler, 200, self.chunk)
                return

            if path.endswith("/progress"):
                self.progress.append(body)
                self.results.extend(body.get("results") or [])
                self._json(handler, 200, {"ok": True})
                return

            if path.endswith("/complete"):
                self.completed.append(body)
                self.results.extend(body.get("results") or [])
                self._json(handler, 200, {"ok": True})
                return

            if path.endswith("/fail"):
                self.failed.append(body)
                self._json(handler, 200, {"ok": True})
                return

        self._json(handler, 404, {"error": "not found"})

    # -- утилиты ---------------------------------------------------------
    @staticmethod
    def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
        length = int(handler.headers.get("Content-Length") or 0)
        raw = handler.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _json(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        if handler.command != "HEAD":
            handler.wfile.write(body)

    # -- удобные проверки ------------------------------------------------
    def statuses(self) -> dict[int, str]:
        mapping: dict[int, str] = {}
        for result in self.results:
            try:
                mapping[int(result["external_id"])] = str(result.get("status"))
            except (KeyError, TypeError, ValueError):
                continue
        return mapping


def wait_for(predicate: Any, timeout: float = 5.0) -> bool:
    """Дождаться условия — удобно в тестах."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())
