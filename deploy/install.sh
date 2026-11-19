#!/usr/bin/env bash
# Установка Baksi Cloud на чистый Ubuntu/Debian: bash deploy/install.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v docker >/dev/null 2>&1; then
  echo "== Устанавливаю Docker"
  curl -fsSL https://get.docker.com | sh
fi

if [ ! -f .env ]; then
  cp .env.example .env
  read -rp "Токен хаб-бота из @BotFather: " HUB
  read -rp "Ваш Telegram ID (через запятую, если владельцев несколько): " OWNERS
  read -rp "Токен Crypto Pay из @CryptoBot (Enter - пропустить): " CP
  read -rp "Домен сайта без https:// (Enter - пока без сайта): " DOMAIN
  SECRET=$(openssl rand -hex 32 2>/dev/null || head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
  sed -i "s|^HUB_BOT_TOKEN=.*|HUB_BOT_TOKEN=${HUB}|" .env
  sed -i "s|^OWNER_IDS=.*|OWNER_IDS=${OWNERS}|" .env
  sed -i "s|^SECRET_KEY=.*|SECRET_KEY=${SECRET}|" .env
  sed -i "s|^CRYPTOPAY_TOKEN=.*|CRYPTOPAY_TOKEN=${CP}|" .env
  if [ -n "${DOMAIN}" ]; then
    sed -i "s|^DOMAIN=.*|DOMAIN=${DOMAIN}|" .env
    sed -i "s|^PUBLIC_URL=.*|PUBLIC_URL=https://${DOMAIN}|" .env
  fi
  echo
  echo "!!! Сохраните SECRET_KEY отдельно от сервера (без него токены из бэкапа не расшифровать):"
  echo "    ${SECRET}"
  echo
fi

mkdir -p data
chown -R 1000:1000 data

if grep -q "^DOMAIN=." .env; then
  docker compose --profile https up -d --build
else
  docker compose up -d --build
fi

echo
echo "Готово. Логи: docker compose logs -f baksi"
echo "Откройте хаб-бота в Telegram и нажмите /start: владельцу придёт сообщение о запуске."
echo "Цены тарифов задаются в админке (хаб-бот → Админка → Веб-админка или сайт /app#/admin/catalog)."
