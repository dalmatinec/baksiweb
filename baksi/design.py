"""Оформление ботов: одна логика для веб-админки и для редактора в хаб-боте.

Значения хранятся в settings под ключом ui:<slug> в виде {"texts": {...}, "buttons": {...}, "layout": {...}}.
"""
from __future__ import annotations

import asyncio
import logging
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .app import App
from .hub.design import HUB_UI

log = logging.getLogger("baksi.design")

STYLES = ("primary", "success", "danger")
ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a", "code", "pre", "blockquote",
                "tg-spoiler", "tg-emoji", "span"}


class DesignError(Exception):
    pass


def check_telegram_html(text: str) -> str | None:
    """Грубая проверка HTML для Telegram: только разрешённые теги и все закрыты. None - всё хорошо."""

    class P(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.stack: list[str] = []
            self.error: str | None = None

        def handle_starttag(self, tag, attrs):
            if tag not in ALLOWED_TAGS:
                self.error = self.error or f"тег <{tag}> Telegram не поддерживает"
            self.stack.append(tag)

        def handle_endtag(self, tag):
            if not self.stack or self.stack[-1] != tag:
                self.error = self.error or f"лишний или незакрытый тег </{tag}>"
            else:
                self.stack.pop()

    parser = P()
    parser.feed(text)
    parser.close()
    if parser.error:
        return parser.error
    if parser.stack:
        return f"не закрыт тег <{parser.stack[-1]}>"
    return None


def targets(app: App) -> list[dict]:
    out = [{"slug": "hub", "title": f"Хаб-бот @{app.hub_username}" if app.hub_username else "Хаб-бот",
            "note": "Твой главный бот. Премиум-эмодзи видны, если у тебя есть Telegram Premium.",
            "schema": HUB_UI}]
    for m in sorted(app.catalog.modules.values(), key=lambda x: x.sort):
        if m.ui_schema:
            out.append({"slug": m.slug, "title": m.title, "color": m.color,
                        "note": "Применяется у всех клиентов за несколько секунд. Если клиент сам поменял текст или "
                                "кнопку в своём /admin, у него останется его вариант. Премиум-иконки видны, если "
                                "Premium есть у владельца бота (клиента).",
                        "schema": m.ui_schema})
    return out


def target(app: App, slug: str) -> dict:
    t = next((x for x in targets(app) if x["slug"] == slug), None)
    if not t:
        raise DesignError("Нет такого бота")
    return t


async def values(app: App, slug: str) -> dict:
    v = await app.db.get_setting(f"ui:{slug}", {}) or {}
    return {"texts": dict(v.get("texts") or {}), "buttons": dict(v.get("buttons") or {}),
            "layout": dict(v.get("layout") or {})}


def clean(app: App, slug: str, raw: dict) -> dict:
    """Проверить и нормализовать значения под схему. DesignError - если что-то не так."""
    schema = target(app, slug)["schema"]
    text_keys = {t["key"] for t in schema.get("texts", [])}
    button_keys = {x["key"] for x in schema.get("buttons", [])}
    layout_keys = {x["key"] for x in schema.get("layout", [])}
    texts, buttons, layout = {}, {}, {}
    for key, value in (raw.get("texts") or {}).items():
        if key not in text_keys:
            raise DesignError(f"Неизвестный текст {key}")
        value = str(value or "").strip()
        if not value:
            continue
        if len(value) > 3500:
            raise DesignError("Текст длиннее 3500 символов")
        err = check_telegram_html(value)
        if err:
            raise DesignError(f"Текст «{key}»: {err}")
        texts[key] = value
    for key, value in (raw.get("buttons") or {}).items():
        if key not in button_keys or not isinstance(value, dict):
            raise DesignError(f"Неизвестная кнопка {key}")
        item: dict[str, Any] = {}
        if str(value.get("text") or "").strip():
            item["text"] = str(value["text"]).strip()[:64]
        if "style" in value and (value["style"] in STYLES or value["style"] in ("", None)):
            item["style"] = value["style"] or None
        icon = str(value.get("icon") or "").strip()
        if icon:
            if not icon.isdigit() or len(icon) > 32:
                raise DesignError("ID премиум-эмодзи - это число. Пришлите эмодзи хаб-боту, он покажет ID")
            item["icon"] = icon
        if item:
            buttons[key] = item
    for key, value in (raw.get("layout") or {}).items():
        if key not in layout_keys:
            raise DesignError(f"Неизвестная раскладка {key}")
        if str(value) in ("1", "2", "3"):
            layout[key] = int(value)
    return {"texts": texts, "buttons": buttons, "layout": layout}


async def save(app: App, slug: str, raw: dict, actor: int | None) -> int:
    """Сохранить и сразу применить. Возвращает, у скольких ботов обновилось."""
    v = clean(app, slug, raw)
    await app.db.set_setting(f"ui:{slug}", v)
    await app.db.audit(actor, "design", slug)
    if slug == "hub":
        app.design.values = v
        return 1
    app.sup.ui[slug] = v
    return await app.sup.refresh_platform_files(slug)


# --- картинки премиум-эмодзи для превью в вебе ----------------------------------
_locks: dict[str, asyncio.Lock] = {}


async def emoji_image(app: App, emoji_id: str) -> tuple[bytes, str] | None:
    """Превью премиум-эмодзи: миниатюра стикера через Bot API, кэш на диске."""
    if not emoji_id.isdigit() or len(emoji_id) > 32:
        return None
    cache = app.cfg.data_dir / "emoji_cache"
    cache.mkdir(parents=True, exist_ok=True)
    for ext, mime in (("webp", "image/webp"), ("jpg", "image/jpeg"), ("png", "image/png")):
        path = cache / f"{emoji_id}.{ext}"
        if path.exists():
            return path.read_bytes(), mime
    miss = cache / f"{emoji_id}.none"
    if miss.exists():
        return None
    lock = _locks.setdefault(emoji_id, asyncio.Lock())
    async with lock:
        try:
            stickers = await app.bot.get_custom_emoji_stickers([emoji_id])
            if not stickers:
                miss.touch()
                return None
            st = stickers[0]
            file_id = st.thumbnail.file_id if st.thumbnail else (st.file_id if not (st.is_animated or st.is_video) else None)
            if not file_id:
                miss.touch()
                return None
            f = await app.bot.get_file(file_id)
            ext = Path(f.file_path or "x.webp").suffix.lstrip(".").lower() or "webp"
            ext = ext if ext in ("webp", "jpg", "png") else "webp"
            path = cache / f"{emoji_id}.{ext}"
            await app.bot.download(f, destination=path)
            return path.read_bytes(), {"jpg": "image/jpeg", "png": "image/png"}.get(ext, "image/webp")
        except Exception as exc:  # сеть, неизвестный ID и т.п. - просто без картинки
            log.info("Премиум-эмодзи %s: превью недоступно (%s)", emoji_id, exc)
            return None
