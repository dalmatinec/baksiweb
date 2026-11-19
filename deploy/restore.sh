#!/usr/bin/env bash
# Восстановление всей платформы из бэкапа: bash deploy/restore.sh data/backups/baksi-XXXX.zip
set -euo pipefail
cd "$(dirname "$0")/.."
FILE="${1:?Укажите файл бэкапа, например data/backups/baksi-20260101-040000.zip}"
mkdir -p data/backups
case "$FILE" in data/*) ;; *) cp "$FILE" data/backups/ && FILE="data/backups/$(basename "$FILE")";; esac
docker compose stop baksi
docker compose run --rm baksi python -m baksi restore "/data/${FILE#data/}"
docker compose start baksi
echo "Восстановлено. Логи: docker compose logs -f baksi"
