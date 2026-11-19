"""Проверка токенов через Bot API без запуска бота."""
from __future__ import annotations

import re
from dataclasses import dataclass

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError, TelegramNotFound

TOKEN_RE = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{30,50}$")


@dataclass(slots=True)
class BotInfo:
    id: int
    username: str
    name: str


class TokenError(Exception):
    pass


class NetworkError(Exception):
    pass


def make_session(proxy: str = "") -> AiohttpSession:
    return AiohttpSession(proxy=proxy) if proxy else AiohttpSession()


async def check_token(token: str, proxy: str = "") -> BotInfo:
    token = token.strip()
    if not TOKEN_RE.match(token):
        raise TokenError("Это не похоже на токен. Скопируйте его целиком из @BotFather: 123456789:AA…")
    bot = Bot(token, session=make_session(proxy))
    try:
        me = await bot.get_me()
    except (TelegramUnauthorizedError, TelegramNotFound):
        raise TokenError("Telegram не принял токен: проверьте, что он скопирован целиком и не сброшен в @BotFather")
    except TelegramNetworkError as exc:
        raise NetworkError(f"Нет связи с Telegram: {exc}") from exc
    finally:
        await bot.session.close()
    return BotInfo(id=me.id, username=me.username or "", name=me.first_name or "")
