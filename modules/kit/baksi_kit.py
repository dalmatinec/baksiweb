"""Общий набор для модулей Baksi Cloud.

Модуль работает и сам по себе (как обычный бот с .env), и под платформой. Под платформой:
- `platform()` - данные копии: владелец, код привязки, настройки из кабинета;
- `emit()` - сообщить платформе о событии (готов, привязана группа, нужна помощь);
- `bind_router()` - команда /bind <код> в группе: привязывает группу без правки файлов;
- `ready()` - сообщить платформе юзернейм бота после запуска;
- `btn()` / `kb()` - кнопки с цветом и премиум-иконкой; без Premium у владельца Telegram просто не покажет иконку.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Awaitable

log = logging.getLogger("baksi_kit")

EVENT_PREFIX = "@@baksi "
STYLES = ("primary", "success", "danger")  # синяя, зелёная, красная; None - обычная
_platform_cache: tuple[float, dict] | None = None


def in_platform() -> bool:
    return bool(os.environ.get("BAKSI_PLATFORM_FILE"))


def data_dir() -> Path:
    path = Path(os.environ.get("DATA_DIR", "data"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def platform() -> dict:
    """Содержимое platform.json (перечитывается не чаще раза в 5 секунд)."""
    global _platform_cache
    path = os.environ.get("BAKSI_PLATFORM_FILE")
    if not path:
        return {}
    if _platform_cache and time.monotonic() - _platform_cache[0] < 5:
        return _platform_cache[1]
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = _platform_cache[1] if _platform_cache else {}
    _platform_cache = (time.monotonic(), data)
    return data


def setting(key: str, default: Any = None) -> Any:
    return platform().get("settings", {}).get(key, default)


def owner_ids() -> set[int]:
    ids = set(platform().get("owner_ids", []))
    raw = os.environ.get("OWNER_IDS", "")
    ids.update(int(x) for x in raw.replace(" ", "").split(",") if x.strip().lstrip("-").isdigit())
    return ids


def emit(event: str, **data: Any) -> None:
    if not in_platform():
        return
    sys.stdout.write(EVENT_PREFIX + json.dumps({"event": event, **data}, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def notify(text: str) -> None:
    """Попросить платформу написать владельцу бота в хаб-бот (например, «бота удалили из группы»)."""
    emit("notify", text=text[:1500])


# --- состояние привязки ------------------------------------------------------
def _state_path() -> Path:
    return data_dir() / "kit_state.json"


def state() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(platform().get("state", {}))


def save_state(**changes: Any) -> dict:
    current = state()
    current.update(changes)
    current = {k: v for k, v in current.items() if v is not None}
    tmp = _state_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_state_path())
    return current


def bound_chat_id() -> int | None:
    value = state().get("chat_id")
    return int(value) if value else None


def bind_router(on_bound: Callable[[int, str], Awaitable[None]] | None = None,
                on_unbound: Callable[[int], Awaitable[None]] | None = None):
    """Router aiogram: /bind <код> в группе и отслеживание удаления бота из привязанной группы."""
    from aiogram import F, Router
    from aiogram.filters import Command, CommandObject
    from aiogram.types import ChatMemberUpdated, Message

    router = Router(name="baksi_bind")

    @router.message(Command("bind"), F.chat.type.in_({"group", "supergroup"}))
    async def bind(message: Message, command: CommandObject) -> None:
        code = (command.args or "").strip()
        expected = str(platform().get("bind_code", ""))
        if not expected:
            return
        if code != expected:
            await message.reply("Неверный код. Код привязки - в кабинете, в карточке бота.")
            return
        if message.from_user and owner_ids() and message.from_user.id not in owner_ids():
            await message.reply("Привязать группу может только владелец бота.")
            return
        title = message.chat.title or str(message.chat.id)
        old = bound_chat_id()
        save_state(chat_id=message.chat.id, title=title)
        emit("bound", chat_id=message.chat.id, title=title)
        try:
            await message.delete()  # код не должен висеть в группе
        except Exception:
            pass
        await message.answer(f"✅ Группа «{title}» привязана к боту." +
                             ("\nПрежняя группа отвязана." if old and old != message.chat.id else ""))
        if on_bound:
            await on_bound(message.chat.id, title)

    @router.my_chat_member()
    async def membership(update: ChatMemberUpdated) -> None:
        if update.chat.id != bound_chat_id():
            return
        if update.new_chat_member.status in ("left", "kicked"):
            save_state(chat_id=None, title=None)
            emit("unbound", chat_id=update.chat.id)
            notify(f"⚠️ Бота удалили из группы «{update.chat.title}». Добавьте его обратно и отправьте /bind с кодом.")
            if on_unbound:
                await on_unbound(update.chat.id)

    return router


async def ready(bot) -> None:
    """Вызвать после запуска: платформа узнает юзернейм и отметит, что бот работает."""
    me = await bot.get_me()
    emit("ready", bot_id=me.id, username=me.username or "", name=me.first_name or "",
         chat_id=bound_chat_id())


# --- оформление от владельца платформы -----------------------------------------
# Владелец Baksi Cloud задаёт в веб-админке тексты, кнопки (подпись, цвет, премиум-иконка) и раскладку
# для всех ботов этого типа. Они приходят в platform.json → "ui" и применяются без перезапуска (кэш 5 с).
# Собственные правки клиента в /admin бота важнее: настройки владельца - это значения по умолчанию.
def ui() -> dict:
    return platform().get("ui") or {}


def ui_text(key: str, default: str) -> str:
    value = (ui().get("texts") or {}).get(key)
    return value if value else default


def ui_button(key: str, default_text: str, default_style: str | None = None,
              default_icon: str | None = None) -> tuple[str, str | None, str | None]:
    """(подпись, цвет, премиум-иконка) с учётом настроек владельца."""
    o = (ui().get("buttons") or {}).get(key) or {}
    style = o.get("style", default_style)
    return (o.get("text") or default_text, style if style in STYLES else None, o.get("icon") or default_icon)


def ui_btn(key: str, default_text: str, cb: str | None = None, *, style: str | None = None,
           icon: str | None = None, **kw):
    text, st, ic = ui_button(key, default_text, style, icon)
    return btn(text, cb, style=st, icon=ic, **kw)


def ui_cols(key: str, default: int) -> int:
    value = (ui().get("layout") or {}).get(key)
    return int(value) if str(value) in ("1", "2", "3") else default


def chunk(items: list, cols: int) -> list[list]:
    items = [x for x in items if x]
    return [items[i:i + cols] for i in range(0, len(items), max(1, cols))]


# --- кнопки -------------------------------------------------------------------
def btn(text: str, cb: str | None = None, url: str | None = None, style: str | None = None,
        icon: str | None = None, web_app: str | None = None, copy: str | None = None):
    """Кнопка с цветом (primary/success/danger) и премиум-иконкой (id премиум-эмодзи)."""
    from aiogram.types import CopyTextButton, InlineKeyboardButton, WebAppInfo
    kwargs: dict[str, Any] = {"text": text or "·"}
    if style in STYLES:
        kwargs["style"] = style
    if icon:
        kwargs["icon_custom_emoji_id"] = icon
    if url:
        kwargs["url"] = url
    elif web_app:
        kwargs["web_app"] = WebAppInfo(url=web_app)
    elif copy:
        kwargs["copy_text"] = CopyTextButton(text=copy)
    else:
        kwargs["callback_data"] = cb or "noop"
    return InlineKeyboardButton(**kwargs)


def kb(rows: list[list[Any]]):
    from aiogram.types import InlineKeyboardMarkup
    return InlineKeyboardMarkup(inline_keyboard=[[b for b in row if b] for row in rows if any(row)])


def session():
    """Сессия aiogram с прокси из окружения, если он задан."""
    from aiogram.client.session.aiohttp import AiohttpSession
    proxy = os.environ.get("PROXY_URL", "").strip()
    return AiohttpSession(proxy=proxy) if proxy else AiohttpSession()


def setup_logging() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                        format="%(levelname)s %(name)s: %(message)s", stream=sys.stdout)
