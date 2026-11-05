#!/usr/bin/env bash
# Пересобирает docs/openapi.json. Контрактный тест проверяет, что файл совпадает с приложением.
set -euo pipefail
cd "$(dirname "$0")/.."
bash scripts/init-env.sh >/dev/null
docker compose --profile ci build tests >/dev/null
docker compose --profile ci run --rm --no-deps -T tests python -m coordinator.api.openapi > docs/openapi.json
echo "обновлён docs/openapi.json"
