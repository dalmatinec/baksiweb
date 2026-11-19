"""Отправка сообщений от хаб-бота: очередь с ограничением скорости, чтобы не упереться в лимиты Telegram."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from aiogram import Bot
from aiogram.exceptions import (TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError,
                                TelegramRetryAfter)

from .db import Database

log = logging.getLogger("baksi.notify")
RATE = 20  # сообщений в секунду (лимит Telegram ~30)


@dataclass(slots=True)
class Outgoing:
    chat_id: int
    text: str
    markup: Any = None
    attempts: int = 0


class Notifier:
    def __init__(self, bot: Bot, db: Database, owner_ids: frozenset[int]) -> None:
        self.bot = bot
        self.db = db
        self.owner_ids = owner_ids
        self.queue: asyncio.Queue[Outgoing] = asyncio.Queue(maxsize=100_000)

    def send(self, chat_id: int, text: str, markup: Any = None) -> None:
        try:
            self.queue.put_nowait(Outgoing(chat_id, text, markup))
        except asyncio.QueueFull:
            log.error("Очередь уведомлений переполнена, сообщение для %s потеряно", chat_id)

    def owners(self, text: str, markup: Any = None) -> None:
        for oid in self.owner_ids:
            self.send(oid, text, markup)

    async def run(self) -> None:
        interval = 1 / RATE
        while True:
            item = await self.queue.get()
            try:
                await self.bot.send_message(item.chat_id, item.text, reply_markup=item.markup,
                                            disable_web_page_preview=True)
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 1)
                self.queue.put_nowait(item)
            except TelegramForbiddenError:
                await self.db.run("UPDATE users SET hub_blocked = 1 WHERE id = ?", item.chat_id)
            except TelegramBadRequest as exc:
                log.warning("Сообщение для %s не отправлено: %s", item.chat_id, exc.message)
            except TelegramNetworkError:
                item.attempts += 1
                if item.attempts < 5:
                    await asyncio.sleep(2 * item.attempts)
                    self.queue.put_nowait(item)
            except Exception:
                log.exception("Ошибка отправки уведомления")
            await asyncio.sleep(interval)
