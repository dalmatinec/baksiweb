#!/usr/bin/env bash
# Сайт через уже установленный nginx (когда порты 80/443 заняты им): bash deploy/nginx-setup.sh
# Добавляет отдельный сайт для Baksi Cloud, выпускает сертификат certbot'ом. Остальные сайты nginx не трогает.
set -euo pipefail
cd "$(dirname "$0")/.."
[ "$(id -u)" -eq 0 ] || { echo "Запустите от root: sudo bash deploy/nginx-setup.sh"; exit 1; }
command -v nginx >/dev/null || { echo "nginx не найден"; exit 1; }

URL=$(grep -E '^PUBLIC_URL=' .env | cut -d= -f2- | tr -d '"' || true)
HOST=${URL#https://}; HOST=${HOST#http://}; HOST=${HOST%%/*}
[ -n "${HOST}" ] || { echo "В .env не задан PUBLIC_URL (например https://1.2.3.4.sslip.io)"; exit 1; }
echo "== Сайт: ${HOST}"

# Caddy не нужен: https делает nginx
docker compose --profile https rm -sf caddy >/dev/null 2>&1 || true
sed -i 's/^DOMAIN=.*/DOMAIN=/' .env

if grep -qE 'include\s+/etc/nginx/sites-enabled' /etc/nginx/nginx.conf && [ -d /etc/nginx/sites-enabled ]; then
  CONF=/etc/nginx/sites-available/baksi.conf
  LINK=/etc/nginx/sites-enabled/baksi.conf
else
  CONF=/etc/nginx/conf.d/baksi.conf
  LINK=""
fi

if [ ! -f "${CONF}" ]; then
  cat > "${CONF}" <<'EOF'
server {
    listen 80;
    listen [::]:80;
    server_name __HOST__;
    client_max_body_size 64m;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }
}
EOF
  sed -i "s/__HOST__/${HOST}/" "${CONF}"
  [ -n "${LINK}" ] && ln -sf "${CONF}" "${LINK}"
  echo "== Добавлен ${CONF}"
else
  echo "== ${CONF} уже есть, не трогаю"
fi

nginx -t
systemctl reload nginx

if ! command -v certbot >/dev/null; then
  echo "== Ставлю certbot"
  apt-get update -qq && apt-get install -y -qq certbot python3-certbot-nginx
elif ! certbot plugins 2>/dev/null | grep -q nginx; then
  apt-get install -y -qq python3-certbot-nginx
fi

read -rp "Email для уведомлений Let's Encrypt (Enter - без email): " EMAIL
ARGS=(--nginx -d "${HOST}" --non-interactive --agree-tos --redirect)
if [ -n "${EMAIL}" ]; then ARGS+=(-m "${EMAIL}"); else ARGS+=(--register-unsafely-without-email); fi
certbot "${ARGS[@]}"

docker compose up -d baksi
echo "== Жду, пока платформа запустится"
for _ in $(seq 1 30); do
  curl -fsS -m 3 http://127.0.0.1:8080/healthz >/dev/null 2>&1 && break
  sleep 2
done
if curl -fsS -m 10 "https://${HOST}/healthz" >/dev/null; then
  echo "✅ Сайт работает: https://${HOST}"
  echo "   Перезапускаю платформу, чтобы включилась кнопка Mini App в хаб-боте"
  docker compose restart baksi >/dev/null
else
  echo "⚠️  https://${HOST}/healthz не ответил. Пришлите вывод: docker compose logs --tail 50 baksi; nginx -t"
fi
