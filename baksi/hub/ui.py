"""Кнопки хаб-бота. Цвета: primary - синяя, success - зелёная, danger - красная."""
from __future__ import annotations

from typing import Any

from aiogram.types import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo


def b(text: str, cb: str | None = None, *, url: str | None = None, style: str | None = None,
      web_app: str | None = None, copy: str | None = None, icon: str | None = None) -> InlineKeyboardButton:
    kw: dict[str, Any] = {"text": text}
    if style:
        kw["style"] = style
    if icon:
        kw["icon_custom_emoji_id"] = icon
    if url:
        kw["url"] = url
    elif web_app:
        kw["web_app"] = WebAppInfo(url=web_app)
    elif copy:
        kw["copy_text"] = CopyTextButton(text=copy)
    else:
        kw["callback_data"] = cb or "noop"
    return InlineKeyboardButton(**kw)


def kb(*rows: list[InlineKeyboardButton | None]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[x for x in row if x] for row in rows if row and any(row)])


def bot_card_kb(instance_id: int) -> InlineKeyboardMarkup:
    return kb([b("🤖 Открыть бота", f"bot:{instance_id}", style="primary")])
