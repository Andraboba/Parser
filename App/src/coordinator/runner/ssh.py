"""Управление агентом на сервере по SSH (ТЗ, п. 4 и 5.1).

Разрешён только фиксированный набор команд: установить, запустить, остановить, статус, журнал.
Произвольную команду через этот модуль выполнить нельзя, свободный ввод есть только в терминале,
а его открывает лишь администратор. Закрытый ключ остаётся здесь и никуда не передаётся.
"""
import asyncio
import fnmatch
import hashlib
import json
import logging
import re
import shlex
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncssh

log = logging.getLogger("coordinator.runner.ssh")

KEY_REF = re.compile(r"[A-Za-z0-9_-]{1,64}")
ENV_NAME = re.compile(r"(WORKER|AGENT|STUB|COORD)_[A-Z0-9_]+|SSL_CERT_FILE")
BUNDLE_FILES = (".py", ".sh", ".yaml")
CONNECT_TIMEOUT_S = 5.0
COMMAND_TIMEOUT_S = 30.0
MAX_LOG_LINES = 5000

# Общая часть: процесс из agent.pid жив и это действительно агент.
_RUNNING = r"""
dir="$HOME/agent"
pid="$(cat "$dir/agent.pid" 2>/dev/null || true)"
is_running() {
  [ -n "$pid" ] && [ -r "/proc/$pid/cmdline" ] \
    && tr '\0' ' ' < "/proc/$pid/cmdline" | grep -q python
}
"""

_STATUS = _RUNNING + r"""
[ -e "$dir/current/run.sh" ] && echo installed=1 || echo installed=0
echo "release=$(basename "$(readlink "$dir/current" 2>/dev/null)" 2>/dev/null)"
if is_running; then echo running=1; echo "pid=$pid"; else echo running=0; fi
echo "python=$(python3 -V 2>&1)"
echo "kernel=$(uname -r)"
"""

_START = _RUNNING + r"""
is_running && exit 0
cd "$dir/current" || { echo "агент не установлен" >&2; exit 3; }
log="$dir/agent.log"
if [ -f "$log" ] && [ "$(wc -c < "$log")" -gt 10485760 ]; then mv "$log" "$log.1"; fi
set -a
. "$dir/agent.env"
set +a
nohup setsid sh ./run.sh >> "$log" 2>&1 < /dev/null &
echo $! > "$dir/agent.pid"
"""

_STOP = _RUNNING + r"""
is_running || exit 0
kill -TERM "$pid"
i=0
while [ "$i" -lt 40 ]; do
  is_running || exit 0
  sleep 0.5
  i=$((i + 1))
done
kill -KILL "$pid"
"""

_ROLLBACK = _RUNNING + r"""
[ -L "$dir/previous" ] || { echo "прежней версии нет" >&2; exit 3; }
ln -sfn "$(readlink "$dir/previous")" "$dir/current"
"""


class SshFailure(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class SshTarget:
    host: str
    port: int
    user: str
    key_ref: str

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"


def parse_key_value(output: str) -> dict[str, str]:
    pairs = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            pairs[key.strip()] = value.strip()
    return pairs


def bundle_files(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix in BUNDLE_FILES and "__pycache__" not in path.parts:
            yield path


def release_id(root: Path) -> str:
    """Имя выпуска по содержимому: одинаковый набор файлов — один и тот же выпуск."""
    digest = hashlib.sha256()
    for path in bundle_files(root):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return f"{root.name}-{digest.hexdigest()[:12]}"


class SshManager:
    def __init__(
        self, key_dir: Path, pin_file: Path, bundles_dir: Path, host_patterns: list[str],
    ) -> None:
        self.key_dir = key_dir
        self.pin_file = pin_file
        self.bundles_dir = bundles_dir
        self.host_patterns = host_patterns
        self._connections: dict[SshTarget, asyncssh.SSHClientConnection] = {}
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        for connection in self._connections.values():
            connection.close()
        self._connections.clear()

    # -- доступ ---------------------------------------------------------------
    def _check_target(self, target: SshTarget) -> None:
        if not any(fnmatch.fnmatchcase(target.host, p) for p in self.host_patterns):
            raise SshFailure(403, "host_not_allowed", f"адрес {target.host} вне разрешённых")

    def _client_key(self, key_ref: str) -> asyncssh.SSHKey:
        """Имя секрета, а не путь: выйти из каталога секретов через него нельзя."""
        if not KEY_REF.fullmatch(key_ref):
            raise SshFailure(422, "invalid_key_ref", "имя ключа: латиница, цифры, дефис, _")
        path = self.key_dir / key_ref
        if not path.is_file():
            raise SshFailure(422, "unknown_key_ref", f"ключа {key_ref} нет в хранилище секретов")
        try:
            return asyncssh.read_private_key(str(path))
        except (OSError, asyncssh.KeyImportError) as exc:
            # Текст исключения не передаём: в нём может оказаться часть файла.
            raise SshFailure(
                422, "unknown_key_ref", f"файл {key_ref} не читается как закрытый ключ",
            ) from exc

    def public_key(self, key_ref: str) -> str:
        return self._client_key(key_ref).export_public_key().decode().strip()

    def _pins(self) -> dict[str, str]:
        try:
            return json.loads(self.pin_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_pins(self, pins: dict[str, str]) -> None:
        self.pin_file.parent.mkdir(parents=True, exist_ok=True)
        self.pin_file.write_text(json.dumps(pins, indent=2), encoding="utf-8")

    async def _trusted_host_key(self, target: SshTarget) -> asyncssh.SSHKey:
        """Первое подключение запоминает ключ сервера, другой ключ потом — отказ (accept-new)."""
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT_S):
                presented = await asyncssh.get_server_host_key(target.host, target.port)
        except (TimeoutError, OSError, asyncssh.Error) as exc:
            raise SshFailure(
                502, "ssh_unreachable", f"сервер {target.address} недоступен: {type(exc).__name__}",
            ) from exc
        if presented is None:
            raise SshFailure(502, "ssh_unreachable", f"сервер {target.address} не отдал ключ")
        current = presented.export_public_key().decode().strip()
        pins = self._pins()
        known = pins.get(target.address)
        if known is None:
            pins[target.address] = current
            self._save_pins(pins)
            log.info("запомнен ключ сервера %s", target.address)
        elif known != current:
            raise SshFailure(
                409, "host_key_changed",
                f"ключ сервера {target.address} изменился: возможна подмена",
            )
        return presented

    def forget_host_key(self, target: SshTarget) -> None:
        pins = self._pins()
        pins.pop(target.address, None)
        self._save_pins(pins)
        log.warning("забыт ключ сервера %s", target.address)

    async def _connection(self, target: SshTarget) -> asyncssh.SSHClientConnection:
        self._check_target(target)
        async with self._lock:
            existing = self._connections.get(target)
            if existing is not None and not existing.is_closed():
                return existing
            key = await self._trusted_host_key(target)
            try:
                connection = await asyncssh.connect(
                    target.host, port=target.port, username=target.user,
                    client_keys=[self._client_key(target.key_ref)],
                    known_hosts=([key], [], []), config=None,
                    connect_timeout=CONNECT_TIMEOUT_S, keepalive_interval=15,
                )
            except asyncssh.PermissionDenied as exc:
                raise SshFailure(
                    403, "ssh_auth_failed", f"сервер {target.address} отклонил ключ",
                ) from exc
            except (OSError, asyncssh.Error, TimeoutError) as exc:
                raise SshFailure(
                    502, "ssh_unreachable",
                    f"сервер {target.address} недоступен: {type(exc).__name__}",
                ) from exc
            self._connections[target] = connection
            return connection

    def _drop(self, target: SshTarget) -> None:
        connection = self._connections.pop(target, None)
        if connection is not None:
            connection.close()

    async def _run(self, target: SshTarget, script: str, *, check: bool = True) -> str:
        """Выполняет свою фиксированную команду. Пересозданный сервер закрывает старое соединение,
        поэтому при обрыве пробуем ещё раз, уже с новым."""
        for attempt in (1, 2):
            connection = await self._connection(target)
            try:
                async with asyncio.timeout(COMMAND_TIMEOUT_S):
                    result = await connection.run(script, check=False)
                break
            except (OSError, asyncssh.Error, TimeoutError) as exc:
                self._drop(target)
                if attempt == 2:
                    raise SshFailure(
                        502, "ssh_unreachable",
                        f"соединение с {target.address} прервано: {type(exc).__name__}",
                    ) from exc
        if check and result.exit_status != 0:
            raise SshFailure(
                502, "ssh_command_failed",
                f"команда на {target.address} завершилась с кодом {result.exit_status}: "
                f"{str(result.stderr)[:200]}",
            )
        return str(result.stdout)

    # -- операции --------------------------------------------------------------
    async def status(self, target: SshTarget) -> dict[str, Any]:
        values = parse_key_value(await self._run(target, _STATUS))
        return {
            "installed": values.get("installed") == "1",
            "running": values.get("running") == "1",
            "pid": int(values["pid"]) if values.get("pid") else None,
            "release": values.get("release") or None,
            "python": values.get("python"),
            "kernel": values.get("kernel"),
        }

    async def start(self, target: SshTarget) -> None:
        await self._run(target, _START)

    async def stop(self, target: SshTarget) -> None:
        await self._run(target, _STOP)

    async def restart(self, target: SshTarget) -> None:
        await self.stop(target)
        await self.start(target)

    async def logs(self, target: SshTarget, tail: int) -> str:
        lines = max(1, min(tail, MAX_LOG_LINES))
        return await self._run(
            target, f'tail -n {lines} "$HOME/agent/agent.log" 2>/dev/null || true',
        )

    async def rollback(self, target: SshTarget) -> None:
        await self.stop(target)
        await self._run(target, _ROLLBACK)
        await self.start(target)

    async def deploy(
        self, target: SshTarget, kind: str, env: dict[str, str],
    ) -> dict[str, str]:
        """Загружает выпуск агента и его настройки, старый выпуск остаётся для отката.
        Запускать агента здесь не надо: это отдельное действие."""
        root = self.bundles_dir / kind
        if not KEY_REF.fullmatch(kind) or not root.is_dir():
            raise SshFailure(422, "unknown_agent_kind", f"нет выпуска агента {kind}")
        release = release_id(root)
        connection = await self._connection(target)
        home = (await self._run(target, 'printf %s "$HOME"')).strip()
        base = f"{home}/agent"
        directory = f"{base}/releases/{release}"
        try:
            async with asyncio.timeout(COMMAND_TIMEOUT_S * 4):
                await self._run(target, f"mkdir -p {shlex.quote(directory)}")
                async with connection.start_sftp_client() as sftp:
                    for path in bundle_files(root):
                        remote = f"{directory}/{path.relative_to(root).as_posix()}"
                        await sftp.makedirs(remote.rsplit("/", 1)[0], exist_ok=True)
                        async with sftp.open(remote, "wb") as remote_file:
                            await remote_file.write(path.read_bytes())
                    # Настройки с токеном читает только владелец.
                    attrs = asyncssh.SFTPAttrs(permissions=0o600)
                    async with sftp.open(f"{base}/agent.env", "wb", attrs) as remote_file:
                        await remote_file.write(_render_env(env).encode())
                    await sftp.chmod(f"{base}/agent.env", 0o600)
        except (OSError, asyncssh.Error, TimeoutError) as exc:
            self._drop(target)
            raise SshFailure(
                502, "ssh_deploy_failed", f"загрузка на {target.address}: {type(exc).__name__}",
            ) from exc
        await self._run(target, (
            f'dir={shlex.quote(base)}; '
            # При повторной установке того же выпуска прежняя версия не должна потеряться.
            f'new={shlex.quote(directory)}; '
            'if [ -L "$dir/current" ] && [ "$(readlink "$dir/current")" != "$new" ]; then '
            'ln -sfn "$(readlink "$dir/current")" "$dir/previous"; fi; '
            'ln -sfn "$new" "$dir/current"'
        ))
        return {"release": release}

    async def open_terminal(
        self, target: SshTarget, columns: int = 80, rows: int = 24,
    ) -> "SshTerminalStream":
        connection = await self._connection(target)
        try:
            process = await connection.create_process(
                term_type="xterm-256color", term_size=(columns, rows), encoding=None,
            )
        except (OSError, asyncssh.Error) as exc:
            self._drop(target)
            raise SshFailure(
                502, "ssh_unreachable", f"терминал {target.address}: {type(exc).__name__}",
            ) from exc
        return SshTerminalStream(process)


def _render_env(env: dict[str, str]) -> str:
    """Только разрешённые имена, значения в кавычках: файл выполняется оболочкой."""
    return "".join(
        f"{name}={shlex.quote(value)}\n" for name, value in sorted(env.items())
        if ENV_NAME.fullmatch(name)
    )


class SshTerminalStream:
    """Тот же интерфейс, что у терминала контейнера: read, write, resize, close."""

    def __init__(self, process: Any) -> None:
        self.process = process

    async def read(self) -> bytes:
        try:
            return await self.process.stdout.read(65536)
        except (OSError, asyncssh.Error):
            return b""

    async def write(self, data: bytes) -> None:
        self.process.stdin.write(data)
        await self.process.stdin.drain()

    async def resize(self, columns: int, rows: int) -> None:
        self.process.change_terminal_size(columns, rows)

    async def close(self) -> None:
        self.process.close()
