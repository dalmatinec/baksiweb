"""Оформление хаб-бота: тексты, кнопки (подпись, цвет, премиум-иконка) и раскладку меняет владелец в веб-админке.

Премиум-иконки на кнопках и премиум-эмодзи в текстах Telegram показывает, если у владельца хаб-бота есть Premium.
"""
from __future__ import annotations

from typing import Any

from aiogram.types import InlineKeyboardButton

from .ui import b

HUB_UI: dict[str, Any] = {
    "screens": [
        {"title": "Главное меню", "text": "main", "layout": "main_cols",
         "buttons": ["h_my", "h_wallet", "h_web", "h_ref"], "rows_before": [["h_cat"], ["h_app"]],
         "rows": [["h_support"], ["h_admin"]]},
        {"title": "Каталог", "text": "catalog", "rows": []},
        {"title": "Кошелёк", "text": "wallet", "rows": []},
    ],
    "texts": [
        {"key": "main", "label": "Главное меню",
         "default": "<b>{бренд}</b>: боты для Telegram под ключ\n\n💰 Баланс: <b>{баланс}</b>\n🤖 Ботов: <b>{ботов}</b>\n"
                    "🆔 ID: <code>{id}</code>",
         "hint": "{бренд}, {баланс}, {ботов}, {id}, {имя}"},
        {"key": "catalog", "label": "Заголовок каталога",
         "default": "<b>Каталог ботов</b>\nСвой бот на своём токене. Запуск за пару минут."},
        {"key": "wallet", "label": "Заголовок кошелька", "default": "<b>Кошелёк</b>\n\n💰 Баланс: <b>{баланс}</b>",
         "hint": "{баланс}"},
    ],
    "buttons": [
        {"key": "h_cat", "label": "Каталог ботов", "default": "🛒 Каталог ботов", "style": "success"},
        {"key": "h_app", "label": "Открыть приложение (Mini App)", "default": "✨ Открыть приложение", "style": "primary"},
        {"key": "h_my", "label": "Мои боты", "default": "🤖 Мои боты"},
        {"key": "h_wallet", "label": "Кошелёк", "default": "💳 Кошелёк"},
        {"key": "h_web", "label": "Веб-кабинет", "default": "🌐 Веб-кабинет"},
        {"key": "h_ref", "label": "Рефералы", "default": "🎁 Рефералы"},
        {"key": "h_support", "label": "Поддержка", "default": "💬 Поддержка"},
        {"key": "h_admin", "label": "Админка (видишь только ты)", "default": "⚙️ Админка"},
        {"key": "h_back", "label": "Назад", "default": "⬅️ Назад"},
    ],
    "layout": [{"key": "main_cols", "label": "Главное меню: кнопок в ряд", "default": 2}],
}
DEFAULT_TEXTS = {t["key"]: t["default"] for t in HUB_UI["texts"]}
DEFAULT_BUTTONS = {x["key"]: x for x in HUB_UI["buttons"]}


class HubDesign:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}

    def text(self, key: str, **fields: Any) -> str:
        raw = (self.values.get("texts") or {}).get(key) or DEFAULT_TEXTS[key]
        for name, value in fields.items():
            raw = raw.replace("{" + name + "}", str(value))
        return raw

    def button(self, key: str, cb: str | None = None, **kw: Any) -> InlineKeyboardButton:
        d = DEFAULT_BUTTONS[key]
        o = (self.values.get("buttons") or {}).get(key) or {}
        style = o.get("style", d.get("style"))
        return b(o.get("text") or d["default"], cb, style=style if style in ("primary", "success", "danger") else None,
                 icon=o.get("icon") or None, **kw)

    def cols(self, key: str, default: int) -> int:
        value = (self.values.get("layout") or {}).get(key)
        return int(value) if str(value) in ("1", "2", "3") else default
