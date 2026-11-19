"""Защита хаб-бота: бан, антифлуд, обновление «последний раз был»."""
from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from ..app import App

FLOOD_INTERVAL = 0.35  # не чаще ~3 действий в секунду от одного человека


class Guard(BaseMiddleware):
    def __init__(self, app: App) -> None:
        self.app = app
        self.last: dict[int, float] = {}
        self.banned: dict[int, tuple[bool, float]] = {}

    async def _is_banned(self, user_id: int) -> bool:
        cached = self.banned.get(user_id)
        if cached and time.monotonic() - cached[1] < 60:
            return cached[0]
        value = bool(await self.app.db.value("SELECT banned FROM users WHERE id = ?", user_id, default=0))
        self.banned[user_id] = (value, time.monotonic())
        return value

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        user = getattr(event, "from_user", None)
        if user is None:
            return await handler(event, data)
        if isinstance(event, Message) and event.chat.type != "private":
            return None  # хаб-бот работает только в личке
        now = time.monotonic()
        if now - self.last.get(user.id, 0) < FLOOD_INTERVAL and not self.app.is_owner(user.id):
            if isinstance(event, CallbackQuery):
                await event.answer()
            return None
        self.last[user.id] = now
        if len(self.last) > 50_000:
            self.last.clear()
            self.banned.clear()
        if not self.app.is_owner(user.id) and await self._is_banned(user.id):
            if isinstance(event, CallbackQuery):
                await event.answer("Доступ ограничен", show_alert=True)
            return None
        return await handler(event, data)
