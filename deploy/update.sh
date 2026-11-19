#!/usr/bin/env bash
# Обновление: bash deploy/update.sh  (данные в ./data не трогаются, перед обновлением делается бэкап)
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose exec -T baksi python -m baksi backup || echo "Бэкап перед обновлением не удался, продолжаю"
git pull --ff-only
if grep -q "^DOMAIN=." .env; then
  docker compose --profile https up -d --build
else
  docker compose up -d --build
fi
docker image prune -f >/dev/null
echo "Обновлено. Логи: docker compose logs -f baksi"
