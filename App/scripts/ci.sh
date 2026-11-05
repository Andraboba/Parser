#!/usr/bin/env bash
# Полный прогон CI в контейнерах: сборка образов, линтер, миграции на чистой базе, тесты.
# Работает одинаково локально (Git Bash / Linux) и в CI-раннере.
set -euo pipefail
cd "$(dirname "$0")/.."

# Отдельный проект compose — своя чистая база, рабочий стенд не трогается.
export COMPOSE_PROJECT_NAME=coord-ci
# Без проброса портов наружу, чтобы не конфликтовать с запущенным стендом.
compose() { docker compose -f compose.yaml -f deploy/compose.ci.yaml "$@"; }
trap 'compose --profile ci down -v --remove-orphans >/dev/null 2>&1 || true' EXIT

echo "==> build"
compose --profile ci build

echo "==> lint"
compose --profile ci run --rm --no-deps tests ruff check src tests migrations

echo "==> migrations on clean db"
compose run --rm migrate

echo "==> tests"
compose --profile ci run --rm tests pytest -q

echo "CI OK"
