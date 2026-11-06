#!/usr/bin/env bash
# Развёртывание всего стенда на удалённый сервер по IP, логину и паролю.
#
#   bash scripts/deploy-remote.sh 203.0.113.5 root
#   bash scripts/deploy-remote.sh 203.0.113.5 ubuntu --port 9443 --http-port 9080
#
# Пароль спрашивается без отображения (или берётся из DEPLOY_PASSWORD). Нужен только Docker на
# этой машине: подключение по SSH выполняет Python-скрипт в одноразовом контейнере.
# Остальные параметры: bash scripts/deploy-remote.sh --help
set -euo pipefail
cd "$(dirname "$0")/.."

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ] || [ $# -lt 1 ]; then
  cat <<'HELP'
Использование: bash scripts/deploy-remote.sh <IP> [логин] [параметры]

  --port N         порт HTTPS интерфейса (по умолчанию 8443)
  --http-port N    порт HTTP с перенаправлением (по умолчанию 8080)
  --ssh-port N     порт SSH сервера (по умолчанию 22)
  --public-host H  внешний адрес, если отличается от IP подключения
  --dir PATH       каталог установки на сервере (по умолчанию /opt/ushastiy)
  --renew-tls      перевыпустить сертификаты (например, после смены адреса)

Логин по умолчанию root. Обычному пользователю нужен sudo, пароль тот же.
HELP
  exit 0
fi

host="$1"
shift
user="root"
if [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; then
  user="$1"
  shift
fi

if [ -z "${DEPLOY_PASSWORD:-}" ]; then
  read -rsp "Пароль $user@$host: " DEPLOY_PASSWORD
  echo
fi
export DEPLOY_PASSWORD

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# Только файлы репозитория и рабочие правки, без секретов, сертификатов и .env:
# у сервера будут свои, созданные на месте.
echo "==> упаковываю репозиторий"
( cd .. && git ls-files -co --exclude-standard -z -- Agent App README.md \
    | grep -zv '\.docx$\|\.pdf$' \
    | tar --null -T - -czf "$work/app.tar.gz" )
cp scripts/deploy_remote.py "$work/"

# Git Bash на Windows: путь для docker в виде C:/..., без преобразований MSYS.
mount="$work"
if command -v cygpath >/dev/null 2>&1; then
  mount="$(cygpath -m "$work")"
  export MSYS_NO_PATHCONV=1
fi

docker run --rm -i -e DEPLOY_PASSWORD -v "$mount:/work:ro" python:3.13-slim sh -c \
  'pip install -q --root-user-action=ignore --disable-pip-version-check asyncssh >/dev/null &&
   exec python -u /work/deploy_remote.py --archive /work/app.tar.gz "$@"' \
  deploy --host "$host" --user "$user" "$@"
