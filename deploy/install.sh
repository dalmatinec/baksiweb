#!/usr/bin/env bash
# Установка Baksi Cloud на чистый Ubuntu/Debian: bash deploy/install.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v docker >/dev/null 2>&1; then
  echo "== Устанавливаю Docker"
  curl -fsSL https://get.docker.com | sh
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "Запустите от root: sudo bash deploy/install.sh"; exit 1
fi

if ! curl -fsS -m 10 -o /dev/null https://api.telegram.org; then
  echo "⚠️  Сервер не достаёт до api.telegram.org. Укажите PROXY_URL в .env (socks5://…), иначе боты не запустятся."
fi

IP=$(curl -fsS -m 10 https://api.ipify.org 2>/dev/null || curl -fsS -m 10 https://ifconfig.me 2>/dev/null || true)

if [ ! -f .env ]; then
  cp .env.example .env
  read -rp "Токен хаб-бота из @BotFather: " HUB
  read -rp "Ваш Telegram ID (через запятую, если владельцев несколько): " OWNERS
  read -rp "Токен Crypto Pay из @CryptoBot (Enter - пропустить): " CP
  if [ -n "${IP}" ]; then
    echo "Нет своего домена? Для теста подойдёт бесплатный ${IP}.sslip.io: сертификат https выпустится сам,"
    echo "заработают сайт, кабинет и Mini App. Потом поменяете на свой домен в .env."
    read -rp "Домен без https:// (Enter = ${IP}.sslip.io, '-' = без сайта): " DOMAIN
    if [ -z "${DOMAIN}" ]; then DOMAIN="${IP}.sslip.io"; fi
    if [ "${DOMAIN}" = "-" ]; then DOMAIN=""; fi
  else
    read -rp "Домен сайта без https:// (Enter - пока без сайта): " DOMAIN
  fi
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

if grep -q "^DOMAIN=." .env && command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow 80/tcp >/dev/null && ufw allow 443/tcp >/dev/null && echo "Открыл порты 80 и 443 в ufw"
fi

if grep -q "^DOMAIN=." .env && ss -ltnp 2>/dev/null | grep -qE ':(80|443)\s.*nginx'; then
  echo "== Порты 80/443 заняты nginx: сайт подключу через него, а не через Caddy"
  docker compose up -d --build
  bash deploy/nginx-setup.sh
elif grep -q "^DOMAIN=." .env; then
  docker compose --profile https up -d --build
else
  docker compose up -d --build
fi

echo
echo "Готово. Логи: docker compose logs -f baksi"
if grep -q "^PUBLIC_URL=." .env; then echo "Сайт: $(grep ^PUBLIC_URL= .env | cut -d= -f2)"; fi
echo "Откройте хаб-бота в Telegram и нажмите /start: владельцу придёт сообщение о запуске."
echo "Цены тарифов задаются в админке (хаб-бот → Админка → Веб-админка или сайт /app#/admin/catalog)."
