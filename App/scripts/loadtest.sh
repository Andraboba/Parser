#!/usr/bin/env bash
# Запускает нагрузочный тест на поднятом стенде и снимает потребление контейнеров.
# Результат: docs/load-results.md (таблицы) и docs/load-stats.txt (docker stats раз в 5 секунд).
set -euo pipefail
cd "$(dirname "$0")/.."
export MSYS_NO_PATHCONV=1

STATS=docs/load-stats.txt
: > "$STATS"
(
  while :; do
    docker stats --no-stream --format '{{.Name}} {{.CPUPerc}} {{.MemUsage}}' \
      coord-api-1 coord-db-1 coord-web-1 coord-dispatcher-1 coord-redis-1 >> "$STATS" 2>/dev/null || true
    sleep 5
  done
) &
STATS_PID=$!
trap 'kill "$STATS_PID" 2>/dev/null || true' EXIT

docker compose --profile ci build tests >/dev/null
docker compose --profile ci run --rm --no-deps -T \
  -v "$PWD/docs:/out" \
  -v coord-tls-ca:/certs:ro \
  -v "$PWD/secrets/coord_admin_password:/run/secrets/coord_admin_password:ro" \
  -e PYTHONIOENCODING=utf-8 \
  tests python scripts/loadtest.py "$@"
