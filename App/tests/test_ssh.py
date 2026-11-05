"""Агенты по SSH: настоящий протокол SSH против сервера asyncssh в этом же процессе."""
import asyncio
import contextlib
import shlex
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import asyncssh
import pytest
import pytest_asyncio

from coordinator.runner.ssh import (
    SshFailure,
    SshManager,
    SshTarget,
    _render_env,
    parse_key_value,
    release_id,
)

USER = "agent"


async def _shell(process: asyncssh.SSHServerProcess) -> None:
    """Выполняет команду настоящей оболочкой: сценарии из ssh.py проверяются по-настоящему."""
    command = process.command
    if command is None:
        process.exit(0)
        return
    shell = await asyncio.create_subprocess_shell(
        command, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await shell.communicate()
    process.stdout.write(stdout.decode())
    process.stderr.write(stderr.decode())
    process.exit(shell.returncode or 0)


class Server:
    def __init__(self, keys: Path, host_key: Any, client_public: Path) -> None:
        self.keys = keys
        self.host_key = host_key
        self.client_public = client_public
        self.acceptor: Any = None
        self.port = 0

    async def start(self, host_key: Any = None) -> None:
        self.acceptor = await asyncssh.create_server(
            None, "127.0.0.1", self.port, server_host_keys=[host_key or self.host_key],
            authorized_client_keys=str(self.client_public), process_factory=_shell,
            sftp_factory=True,
        )
        self.port = self.acceptor.get_port()

    async def stop(self) -> None:
        self.acceptor.close()
        # Ожидание закрытия ждёт и клиентов, а они ещё могут быть подключены.
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(0.5):
                await self.acceptor.wait_closed()


@pytest.fixture
def keys(tmp_path: Path) -> Path:
    directory = tmp_path / "keys"
    directory.mkdir()
    client = asyncssh.generate_private_key("ssh-ed25519")
    client.write_private_key(str(directory / "ssh_client_key"))
    client.write_public_key(str(directory / "ssh_client_key.pub"))
    asyncssh.generate_private_key("ssh-ed25519").write_private_key(str(directory / "other_key"))
    return directory


@pytest.fixture
def bundles(tmp_path: Path) -> Path:
    root = tmp_path / "bundles" / "stub"
    (root / "pkg").mkdir(parents=True)
    (root / "run.sh").write_text("#!/bin/sh\nexec python3 -c 'import time; time.sleep(120)'\n")
    (root / "pkg" / "mod.py").write_text("VALUE = 1\n")
    (root / "Dockerfile").write_text("не должен попасть на сервер\n")
    return tmp_path / "bundles"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    directory = tmp_path / "home"
    directory.mkdir()
    monkeypatch.setenv("HOME", str(directory))
    yield directory


@pytest_asyncio.fixture
async def server(keys: Path, home: Path) -> AsyncIterator[Server]:
    instance = Server(
        keys, asyncssh.generate_private_key("ssh-ed25519"), keys / "ssh_client_key.pub",
    )
    await instance.start()
    yield instance
    await instance.stop()


@pytest_asyncio.fixture
async def manager(keys: Path, bundles: Path, tmp_path: Path) -> AsyncIterator[SshManager]:
    instance = SshManager(keys, tmp_path / "state" / "pins.json", bundles, ["127.*"])
    yield instance
    await instance.close()


def target(server: Server, key_ref: str = "ssh_client_key") -> SshTarget:
    return SshTarget("127.0.0.1", server.port, USER, key_ref)


def test_key_value_output_is_parsed() -> None:
    assert parse_key_value("installed=1\nrelease=\nmusor\npython=Python 3.13\n") == {
        "installed": "1", "release": "", "python": "Python 3.13",
    }


def test_env_file_quotes_values_and_drops_foreign_names() -> None:
    text = _render_env({
        "AGENT_TOKEN": "a b$(touch x)", "SSL_CERT_FILE": "/certs/ca.crt",
        "LD_PRELOAD": "evil.so", "PATH": "/evil", "agent_token": "x",
    })
    assert "LD_PRELOAD" not in text
    assert "PATH" not in text
    assert "agent_token" not in text
    assert f"AGENT_TOKEN={shlex.quote('a b$(touch x)')}\n" in text
    assert "SSL_CERT_FILE=/certs/ca.crt\n" in text


def test_release_id_depends_on_content(bundles: Path) -> None:
    root = bundles / "stub"
    first = release_id(root)
    assert first.startswith("stub-")
    assert release_id(root) == first
    (root / "pkg" / "mod.py").write_text("VALUE = 2\n")
    assert release_id(root) != first


@pytest.mark.asyncio
class TestAccess:
    async def test_key_login_and_status(self, manager: SshManager, server: Server) -> None:
        status = await manager.status(target(server))
        assert status["installed"] is False
        assert status["running"] is False
        assert status["python"].startswith("Python")

    async def test_foreign_key_is_rejected(self, manager: SshManager, server: Server) -> None:
        with pytest.raises(SshFailure) as caught:
            await manager.status(target(server, "other_key"))
        assert caught.value.code == "ssh_auth_failed"

    @pytest.mark.parametrize("ref", ["../other_key", "/etc/passwd", "a/b", "..", ""])
    async def test_key_reference_cannot_leave_secret_store(
        self, manager: SshManager, server: Server, ref: str,
    ) -> None:
        with pytest.raises(SshFailure) as caught:
            await manager.status(target(server, ref))
        assert caught.value.code == "invalid_key_ref"

    async def test_unknown_key_reference(self, manager: SshManager, server: Server) -> None:
        with pytest.raises(SshFailure) as caught:
            await manager.status(target(server, "no_such_key"))
        assert caught.value.code == "unknown_key_ref"

    async def test_file_that_is_not_a_key(
        self, manager: SshManager, server: Server, keys: Path,
    ) -> None:
        (keys / "broken_key").write_text("не ключ\n", encoding="utf-8")
        with pytest.raises(SshFailure) as caught:
            await manager.status(target(server, "broken_key"))
        assert caught.value.code == "unknown_key_ref"
        assert "не ключ" not in str(caught.value)

    async def test_host_outside_allowlist_is_refused(self, manager: SshManager) -> None:
        with pytest.raises(SshFailure) as caught:
            await manager.status(SshTarget("evil.example", 22, USER, "ssh_client_key"))
        assert caught.value.code == "host_not_allowed"

    async def test_unreachable_server(self, manager: SshManager) -> None:
        with pytest.raises(SshFailure) as caught:
            await manager.status(SshTarget("127.0.0.1", 1, USER, "ssh_client_key"))
        assert caught.value.code == "ssh_unreachable"


@pytest.mark.asyncio
class TestHostKeyPinning:
    async def test_changed_host_key_is_refused(
        self, manager: SshManager, server: Server,
    ) -> None:
        await manager.status(target(server))
        await manager.close()
        await server.stop()
        await server.start(asyncssh.generate_private_key("ssh-ed25519"))
        with pytest.raises(SshFailure) as caught:
            await manager.status(target(server))
        assert caught.value.code == "host_key_changed"

    async def test_same_key_after_restart_is_accepted(
        self, manager: SshManager, server: Server,
    ) -> None:
        await manager.status(target(server))
        await manager.close()
        await server.stop()
        await server.start()
        assert (await manager.status(target(server)))["installed"] is False

    async def test_operator_can_forget_key_after_real_replacement(
        self, manager: SshManager, server: Server,
    ) -> None:
        await manager.status(target(server))
        await manager.close()
        await server.stop()
        await server.start(asyncssh.generate_private_key("ssh-ed25519"))
        manager.forget_host_key(target(server))
        assert (await manager.status(target(server)))["installed"] is False


@pytest.mark.asyncio
class TestAgentLifecycle:
    async def test_deploy_uploads_only_bundle_and_protects_env(
        self, manager: SshManager, server: Server, home: Path,
    ) -> None:
        result = await manager.deploy(target(server), "stub", {"AGENT_TOKEN": "секрет"})
        current = home / "agent" / "current"
        assert current.resolve().name == result["release"]
        assert (current / "pkg" / "mod.py").is_file()
        assert not (current / "Dockerfile").exists()
        env_file = home / "agent" / "agent.env"
        assert "AGENT_TOKEN=" in env_file.read_text(encoding="utf-8")
        assert env_file.stat().st_mode & 0o777 == 0o600
        status = await manager.status(target(server))
        assert status["installed"] is True
        assert status["release"] == result["release"]

    async def test_unknown_bundle_is_rejected(self, manager: SshManager, server: Server) -> None:
        for kind in ("nope", "../stub"):
            with pytest.raises(SshFailure) as caught:
                await manager.deploy(target(server), kind, {})
            assert caught.value.code == "unknown_agent_kind"

    async def test_start_status_stop(self, manager: SshManager, server: Server) -> None:
        await manager.deploy(target(server), "stub", {})
        await manager.start(target(server))
        await asyncio.sleep(0.3)
        status = await manager.status(target(server))
        assert status["running"] is True
        assert status["pid"] > 0

        await manager.start(target(server))
        again = await manager.status(target(server))
        assert again["pid"] == status["pid"], "повторный запуск не плодит агентов"

        await manager.stop(target(server))
        assert (await manager.status(target(server)))["running"] is False

    async def test_start_without_install_fails_clearly(
        self, manager: SshManager, server: Server,
    ) -> None:
        with pytest.raises(SshFailure) as caught:
            await manager.start(target(server))
        assert caught.value.code == "ssh_command_failed"

    async def test_logs_are_returned(
        self, manager: SshManager, server: Server, home: Path,
    ) -> None:
        await manager.deploy(target(server), "stub", {})
        (home / "agent" / "agent.log").write_text("строка 1\nстрока 2\n", encoding="utf-8")
        assert (await manager.logs(target(server), 1)).strip() == "строка 2"

    async def test_logs_before_first_start_are_empty(
        self, manager: SshManager, server: Server,
    ) -> None:
        assert await manager.logs(target(server), 10) == ""

    async def test_rollback_returns_previous_release(
        self, manager: SshManager, server: Server, bundles: Path, home: Path,
    ) -> None:
        first = await manager.deploy(target(server), "stub", {})
        (bundles / "stub" / "pkg" / "mod.py").write_text("VALUE = 2\n")
        second = await manager.deploy(target(server), "stub", {})
        assert first != second
        await manager.start(target(server))
        await manager.rollback(target(server))
        status = await manager.status(target(server))
        assert status["release"] == first["release"]
        assert status["running"] is True
        await manager.stop(target(server))

    async def test_same_release_again_keeps_rollback_target(
        self, manager: SshManager, server: Server, bundles: Path,
    ) -> None:
        first = await manager.deploy(target(server), "stub", {})
        (bundles / "stub" / "pkg" / "mod.py").write_text("VALUE = 2\n")
        await manager.deploy(target(server), "stub", {})
        await manager.deploy(target(server), "stub", {})
        await manager.rollback(target(server))
        assert (await manager.status(target(server)))["release"] == first["release"]

    async def test_rollback_without_previous_fails(
        self, manager: SshManager, server: Server,
    ) -> None:
        await manager.deploy(target(server), "stub", {})
        with pytest.raises(SshFailure):
            await manager.rollback(target(server))

    async def test_connection_is_reestablished_after_server_restart(
        self, manager: SshManager, server: Server,
    ) -> None:
        await manager.status(target(server))
        await server.stop()
        await server.start()
        assert (await manager.status(target(server)))["installed"] is False
