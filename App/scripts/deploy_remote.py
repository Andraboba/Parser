"""Развёртывание всего стенда на удалённый сервер по IP, логину и паролю.

Запускается из scripts/deploy-remote.sh внутри контейнера с Python: на машине оператора
ничего, кроме Docker, не нужно. Пароль приходит в переменной DEPLOY_PASSWORD и никуда не пишется.

Что делает на сервере:
  1. ставит Docker с Compose, openssl и ssh-keygen, если их нет;
  2. загружает архив репозитория в каталог установки (секреты и сертификаты на сервере не трогает);
  3. прописывает в App/.env порты и внешний адрес;
  4. выполняет scripts/init-env.sh и docker compose up -d --build;
  5. ждёт, пока интерфейс начнёт отвечать, и печатает адрес и пароль администратора.
"""
import argparse
import asyncio
import os
import shlex
import sys
import time

import asyncssh

ENV_KEYS = (
    "COORD_HTTPS_PORT", "COORD_HTTP_PORT", "COORD_PUBLIC_HOST", "COORD_AGENT_PUBLIC_URL",
)


def say(text: str) -> None:
    print(f"==> {text}", flush=True)


class Remote:
    def __init__(self, connection: asyncssh.SSHClientConnection, user: str, password: str) -> None:
        self.connection = connection
        self.password = password
        self.root = user == "root"

    def wrap(self, script: str) -> tuple[str, str | None]:
        """Команда целиком от root: напрямую или через sudo с паролем на stdin."""
        command = f"bash -c {shlex.quote(script)}"
        if self.root:
            return command, None
        return f"sudo -S -p '' {command}", self.password + "\n"

    async def run(self, script: str, *, check: bool = True, limit: float = 120) -> str:
        command, stdin = self.wrap(script)
        result = await asyncio.wait_for(
            self.connection.run(command, input=stdin, check=False), limit,
        )
        if check and result.exit_status != 0:
            raise SystemExit(
                f"команда завершилась с кодом {result.exit_status}:\n{script}\n"
                f"{str(result.stderr or '')[-2000:]}"
            )
        return str(result.stdout or "")

    async def stream(self, script: str, *, limit: float = 3600) -> None:
        """Длинная команда с выводом по мере выполнения: сборка образов идёт минутами."""
        command, stdin = self.wrap(script)
        command += " 2>&1"
        async with self.connection.create_process(command) as process:
            if stdin:
                process.stdin.write(stdin)
            process.stdin.write_eof()

            async def pump() -> None:
                async for line in process.stdout:
                    print(f"    {line}", end="", flush=True)

            await asyncio.wait_for(pump(), limit)
            await process.wait()
            if process.exit_status != 0:
                raise SystemExit(f"команда завершилась с кодом {process.exit_status}: {script}")


PREREQUISITES = r"""
set -e
if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  echo "Docker не найден, ставлю официальным скриптом get.docker.com"
  if ! command -v curl >/dev/null 2>&1; then
    (apt-get update -qq && apt-get install -y -qq curl) \
      || dnf install -y curl || yum install -y curl
  fi
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker >/dev/null 2>&1 || service docker start >/dev/null 2>&1 || true
need=""
command -v openssl >/dev/null 2>&1 || need="$need openssl"
command -v ssh-keygen >/dev/null 2>&1 || need="$need ssh-keygen"
command -v tar >/dev/null 2>&1 || need="$need tar"
command -v curl >/dev/null 2>&1 || need="$need curl"
if [ -n "$need" ]; then
  echo "ставлю:$need"
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq && apt-get install -y -qq openssl openssh-client tar curl
  elif command -v dnf >/dev/null 2>&1; then dnf install -y openssl openssh-clients tar curl
  elif command -v yum >/dev/null 2>&1; then yum install -y openssl openssh-clients tar curl
  elif command -v apk >/dev/null 2>&1; then apk add --no-cache openssl openssh-keygen tar bash curl
  fi
fi
docker version --format 'Docker {{.Server.Version}}'
docker compose version
"""


def env_update(values: dict[str, str]) -> str:
    """Меняет только свои строки App/.env, остальное оставляет как было."""
    lines = []
    for key, value in values.items():
        lines.append(f"sed -i '/^{key}=/d' .env")
        lines.append(f"printf '%s\\n' {shlex.quote(f'{key}={value}')} >> .env")
    return "\n".join(lines)


async def deploy(args: argparse.Namespace, password: str) -> None:
    say(f"подключаюсь к {args.user}@{args.host}:{args.ssh_port}")
    try:
        connection = await asyncssh.connect(
            args.host, port=args.ssh_port, username=args.user, password=password,
            client_keys=None, known_hosts=None, connect_timeout=15,
        )
    except asyncssh.PermissionDenied:
        raise SystemExit("сервер отклонил логин или пароль") from None
    except (OSError, asyncssh.Error) as exc:
        raise SystemExit(f"не удалось подключиться: {exc}") from None
    fingerprint = connection.get_server_host_key().get_fingerprint()
    say(f"ключ сервера {fingerprint}")
    remote = Remote(connection, args.user, password)

    async with connection:
        if not remote.root:
            check = await connection.run("sudo -S -p '' true", input=password + "\n", check=False)
            if check.exit_status != 0:
                raise SystemExit(f"у пользователя {args.user} нет sudo: войдите как root")

        say("проверяю Docker и инструменты")
        await remote.stream(PREREQUISITES, limit=1800)

        directory = args.dir.rstrip("/")
        say(f"загружаю приложение в {directory}")
        upload = f"/tmp/ushastiy-{int(time.time())}.tar.gz"
        async with connection.start_sftp_client() as sftp:
            await sftp.put(args.archive, upload)
        # Распаковка поверх: secrets/, deploy/tls/ и .env в архив не входят и сохраняются.
        await remote.run(
            f"set -e; mkdir -p {shlex.quote(directory)}; "
            f"tar -xzf {shlex.quote(upload)} -C {shlex.quote(directory)}; "
            f"rm -f {shlex.quote(upload)}; "
            f"find {shlex.quote(directory)} -name '*.sh' -exec sed -i 's/\\r$//' {{}} +",
            limit=300,
        )

        app = f"{directory}/App"
        public_url = f"https://{args.public_host}:{args.port}"
        say(f"настраиваю порты: HTTPS {args.port}, HTTP {args.http_port}")
        await remote.run(
            f"set -e; cd {shlex.quote(app)}; [ -f .env ] || cp .env.example .env\n"
            + env_update({
                "COORD_HTTPS_PORT": str(args.port), "COORD_HTTP_PORT": str(args.http_port),
                "COORD_PUBLIC_HOST": args.public_host, "COORD_AGENT_PUBLIC_URL": public_url,
            }),
        )

        say("готовлю секреты и сертификаты")
        renew = " --renew-tls" if args.renew_tls else ""
        await remote.stream(
            f"cd {shlex.quote(app)} && COORD_PUBLIC_HOST={shlex.quote(args.public_host)} "
            f"bash scripts/init-env.sh{renew}",
        )

        # Перенаправление с HTTP ведёт на 8443: при другом порте поправим и его.
        if args.port != 8443:
            await remote.run(
                f"cd {shlex.quote(app)} && sed -i 's/:8443/:{args.port}/' web/nginx.conf",
                check=False,
            )

        await remote.run(
            "if command -v ufw >/dev/null 2>&1 && ufw status | grep -q 'Status: active'; then "
            f"ufw allow {args.port}/tcp; ufw allow {args.http_port}/tcp; fi",
            check=False,
        )

        say("собираю и запускаю контейнеры (первый раз несколько минут)")
        await remote.stream(f"cd {shlex.quote(app)} && docker compose up -d --build")

        say("жду, пока интерфейс начнёт отвечать")
        deadline = time.monotonic() + args.wait
        while True:
            code = (await remote.run(
                f"curl -sk -o /dev/null -w '%{{http_code}}' https://127.0.0.1:{args.port}/ || true",
                check=False,
            )).strip()
            if code == "200":
                break
            if time.monotonic() > deadline:
                await remote.stream(f"cd {shlex.quote(app)} && docker compose ps")
                raise SystemExit("интерфейс не ответил вовремя, состояние сервисов выше")
            await asyncio.sleep(3)

        admin = (await remote.run(
            f"cat {shlex.quote(app)}/secrets/coord_admin_password",
        )).strip()

    print()
    say("готово")
    print(f"    Интерфейс:  {public_url}")
    print("    Логин:      admin")
    print(f"    Пароль:     {admin}")
    print(f"    Каталог:    {app}")
    print(f"    Сертификат CA стенда: {app}/deploy/tls/ca.crt")
    print("    Серверы с агентами добавляются на странице «Узлы» по IP, логину и паролю.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--host", required=True, help="IP или имя сервера")
    parser.add_argument("--user", default="root", help="логин SSH (root или пользователь с sudo)")
    parser.add_argument("--ssh-port", type=int, default=22)
    parser.add_argument("--port", type=int, default=8443, help="порт HTTPS интерфейса")
    parser.add_argument("--http-port", type=int, default=8080, help="порт HTTP (перенаправление)")
    parser.add_argument(
        "--public-host", help="внешний адрес для браузера и агентов, по умолчанию --host",
    )
    parser.add_argument("--dir", default="/opt/ushastiy", help="каталог установки на сервере")
    parser.add_argument("--archive", required=True, help="архив репозитория")
    parser.add_argument("--renew-tls", action="store_true", help="перевыпустить сертификаты")
    parser.add_argument("--wait", type=float, default=600, help="сколько ждать запуска, секунд")
    args = parser.parse_args()
    args.public_host = args.public_host or args.host
    for port in (args.ssh_port, args.port, args.http_port):
        if not 1 <= port <= 65535:
            parser.error(f"порт {port} вне 1..65535")
    password = os.environ.pop("DEPLOY_PASSWORD", "")
    if not password:
        parser.error("нет пароля: передайте его в переменной DEPLOY_PASSWORD")
    try:
        asyncio.run(deploy(args, password))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
