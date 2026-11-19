"""Вход в веб: одноразовая ссылка из хаб-бота, Telegram Mini App, Telegram Login Widget. Сессии и лимиты."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections import defaultdict, deque
from typing import Any
from urllib.parse import parse_qsl

from fastapi import HTTPException, Request

from ..app import App
from ..crypto import new_token
from ..db import now

SESSION_COOKIE = "bc_s"
SESSION_TTL = 30 * 86400
INITDATA_TTL = 24 * 3600


def check_webapp_init_data(init_data: str, bot_token: str) -> dict:
    """Проверка подписи данных Telegram Mini App (официальный алгоритм)."""
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", "")
    if not received:
        raise HTTPException(401, "Нет подписи Telegram")
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise HTTPException(401, "Неверная подпись Telegram")
    if time.time() - int(pairs.get("auth_date", "0")) > INITDATA_TTL:
        raise HTTPException(401, "Данные входа устарели, откройте приложение заново")
    try:
        return json.loads(pairs.get("user", "{}"))
    except ValueError:
        raise HTTPException(401, "Нет данных пользователя")


def check_login_widget(data: dict[str, Any], bot_token: str) -> dict:
    """Проверка данных Telegram Login Widget."""
    data = {k: str(v) for k, v in data.items() if v is not None}
    received = data.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hashlib.sha256(bot_token.encode()).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not received or not hmac.compare_digest(expected, received):
        raise HTTPException(401, "Неверная подпись Telegram")
    if time.time() - int(data.get("auth_date", "0")) > INITDATA_TTL:
        raise HTTPException(401, "Данные входа устарели")
    return {"id": int(data["id"]), "username": data.get("username"), "first_name": data.get("first_name")}


async def create_session(app: App, user_id: int, user_agent: str) -> str:
    token = new_token(32)
    await app.db.run("INSERT INTO sessions(token_hash, user_id, created_at, expires_at, user_agent) "
                     "VALUES (?, ?, ?, ?, ?)", app.vault.hash(token), user_id, now(), now() + SESSION_TTL,
                     user_agent[:300])
    return token


def session_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return request.cookies.get(SESSION_COOKIE)


async def current_user(request: Request) -> Any:
    app: App = request.app.state.baksi
    token = session_token(request)
    if not token:
        raise HTTPException(401, "Нужно войти")
    row = await app.db.one("SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
                           "WHERE s.token_hash = ? AND s.expires_at > ?", app.vault.hash(token), now())
    if not row:
        raise HTTPException(401, "Сессия истекла, войдите снова")
    if row["banned"] and not app.is_owner(row["id"]):
        raise HTTPException(403, "Доступ ограничен")
    return row


async def owner_user(request: Request) -> Any:
    user = await current_user(request)
    if not request.app.state.baksi.is_owner(user["id"]):
        raise HTTPException(403, "Только для администрации")
    return user


class RateLimiter:
    """Скользящее окно в памяти: ключ (ip, корзина)."""

    def __init__(self) -> None:
        self.hits: dict[tuple[str, str], deque] = defaultdict(deque)

    def check(self, ip: str, bucket: str, limit: int, window: float) -> None:
        q = self.hits[(ip, bucket)]
        t = time.monotonic()
        while q and t - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            raise HTTPException(429, "Слишком много запросов, подождите немного")
        q.append(t)
        if len(self.hits) > 100_000:
            self.hits.clear()
