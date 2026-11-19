#!/usr/bin/env bash
# Восстановление всей платформы из бэкапа.
#   bash deploy/restore.sh data/backups/baksi-20260101-040000.zip
#   bash deploy/restore.sh ~/baksi-20260101-040000.zip.001     # бэкап частями из Telegram: укажите первую часть
# Текущие данные не удаляются: старая база и папка ботов переименовываются в *.before-restore-<время>.
set -euo pipefail
cd "$(dirname "$0")/.."
FILE="${1:?Укажите файл бэкапа: data/backups/baksi-XXXX.zip или первую часть baksi-XXXX.zip.001}"
mkdir -p data/backups
case "$FILE" in
  *.zip.0*)  # части по 45 МБ из канала бэкапов: склеиваем
    BASE="${FILE%.*}"
    OUT="data/backups/$(basename "$BASE")"
    echo "Склеиваю части: $(ls "$BASE".0* | wc -l) шт."
    cat "$BASE".0* > "$OUT"
    FILE="$OUT" ;;
  data/*) ;;
  *) cp "$FILE" data/backups/ && FILE="data/backups/$(basename "$FILE")" ;;
esac
python3 -c "import zipfile,sys; z=zipfile.ZipFile(sys.argv[1]); bad=z.testzip(); sys.exit('Архив повреждён: '+bad if bad else 0)" "$FILE" \
  || { echo "Архив повреждён или склеен не полностью"; exit 1; }
docker compose stop baksi 2>/dev/null || true
docker compose run --rm baksi python -m baksi restore "/data/${FILE#data/}"
docker compose up -d baksi
echo "Восстановлено из $FILE. Логи: docker compose logs -f baksi"
