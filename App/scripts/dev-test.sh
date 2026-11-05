#!/usr/bin/env bash
# Быстрый цикл разработки: код монтируется в контейнер, образ не пересобирается,
# база и redis остаются запущенными между прогонами. Аргументы передаются в pytest.
# После смены зависимостей в pyproject.toml: docker compose --profile ci build tests
# Полная проверка перед сдачей — только scripts/ci.sh.
set -euo pipefail
cd "$(dirname "$0")/.."
bash scripts/init-env.sh >/dev/null

export COMPOSE_PROJECT_NAME=coord-dev MSYS_NO_PATHCONV=1
compose() { docker compose -f compose.yaml -f deploy/compose.ci.yaml "$@"; }
in_tests() {
  compose --profile ci run --rm \
    -e PYTHONPATH=/app/src:/agent-src \
    -v "$PWD/src:/app/src" -v "$PWD/tests:/app/tests" -v "$PWD/stub:/app/stub" -v "$PWD/scripts:/app/scripts" \
    -v "$PWD/migrations:/app/migrations" -v "$PWD/docs:/app/docs" \
    -v "$PWD/pyproject.toml:/app/pyproject.toml" "$@"
}

in_tests --no-deps tests ruff check --fix --unsafe-fixes --output-format=concise \
  src tests migrations stub scripts
compose up -d --wait db redis 2>/dev/null
in_tests --no-deps tests alembic upgrade head 2>&1 | grep -v 'INFO' || true
in_tests --no-deps -T tests python -m coordinator.api.openapi > docs/.openapi.tmp 2>/dev/null \
  && mv docs/.openapi.tmp docs/openapi.json || rm -f docs/.openapi.tmp
in_tests tests pytest -q -x -W ignore "$@"
