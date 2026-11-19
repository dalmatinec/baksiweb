"""Хаб: надёжность для владельца платформы (бэкапы, канал бэкапов, запасной хаб) и «оставить бота работать»."""
from __future__ import annotations

import time

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from ..app import App
from ..resilience import reserve_username, set_backup_chat, touch
from .ui import b, kb

router = Router(name="hub_ops")


class OpsForm(StatesGroup):
    channel = State()


def _h():
    from . import handlers
    return handlers


async def ops_view(app: App) -> tuple[str, object]:
    h = _h()
    backups = app.backups.list()
    chat = await app.db.get_setting("backup_chat", None)
    lines = ["🛟 <b>Надёжность</b>\n", "💾 <b>Бэкапы</b>: каждую ночь в 04:00, на сервере хранится только последний."]
    if backups:
        last = backups[0]
        lines.append(f"Последний: <code>{h.esc(last['name'])}</code>, {last['size'] / 1048576:.1f} МБ")
    else:
        lines.append("Бэкапов ещё не было.")
    lines.append("Копия вне сервера: " + (f"канал «{h.esc(chat.get('title') or chat['id'])}»" if chat
                                         else "вам в личку. Надёжнее - приватный канал (история всех бэкапов)."))
    reserve = await reserve_username(app)
    lines += ["", "🤖 <b>Хаб-бот</b>: " + ("работает <b>запасной</b>" if app.hub_mode == "reserve" else "работает основной")
              + f" @{h.esc(app.hub_username)}"]
    if app.hub_mode == "reserve":
        lines.append("Создайте новый запасной бот и пропишите токены в .env (см. сообщение после переключения).")
    elif reserve:
        lines.append(f"Запасной @{h.esc(reserve)} готов: если Telegram заблокирует основной, платформа переключится "
                     "сама и разошлёт всем новую ссылку.")
    else:
        lines.append("Запасной не задан. Создайте второго бота в @BotFather и впишите токен в .env: "
                     "<code>HUB_BOT_TOKEN_RESERVE=...</code>, затем перезапустите платформу.")
    lines += ["", "🖥 <b>Переезд на другой сервер</b>: последний бэкап из канала → новый сервер → "
              "<code>bash deploy/restore.sh</code> → сменить IP у домена. Подробно: docs/MIGRATION.md."]
    rows = [[b("📡 Канал для бэкапов" if not chat else "📡 Сменить канал бэкапов", "ops:ch", style=None if chat else "primary")],
            [b("💾 Сделать бэкап сейчас", "admbackup")],
            [b("⬅️ Назад", "adm")]]
    return "\n".join(lines), kb(*rows)


@router.callback_query(F.data == "ops")
async def ops(call: CallbackQuery, app: App, state: FSMContext) -> None:
    if not app.is_owner(call.from_user.id):
        await call.answer()
        return
    await state.clear()
    text, markup = await ops_view(app)
    await _h().render(call, text, markup)


@router.callback_query(F.data == "ops:ch")
async def channel_ask(call: CallbackQuery, app: App, state: FSMContext) -> None:
    if not app.is_owner(call.from_user.id):
        await call.answer()
        return
    await state.set_state(OpsForm.channel)
    await _h().render(call, (
        "📡 <b>Канал для бэкапов</b>\n\n"
        "1. Создайте приватный канал (только для себя).\n"
        f"2. Добавьте @{app.hub_username} администратором с правом публиковать сообщения.\n"
        "3. Перешлите сюда любой пост из этого канала.\n\n"
        "Бэкапы будут приходить туда каждую ночь, большие - частями по 45 МБ."),
        kb([b("Отмена", "ops")]))


@router.message(OpsForm.channel)
async def channel_set(message: Message, app: App, state: FSMContext) -> None:
    if not app.is_owner(message.from_user.id):
        return
    origin = message.forward_origin
    chat = getattr(origin, "chat", None) if origin else None
    if not chat and (message.text or "").strip().lstrip("-").isdigit():
        chat_id, title = int(message.text.strip()), message.text.strip()
    elif chat and chat.type == "channel":
        chat_id, title = chat.id, chat.title or str(chat.id)
    else:
        await message.answer("⚠️ <b>Не сохранено.</b> Перешлите пост именно из канала (или пришлите его ID вида -100…).")
        return
    try:
        await set_backup_chat(app, chat_id, title)
    except Exception as exc:
        await message.answer(f"⚠️ <b>Не сохранено.</b> Бот не может писать в канал: {exc}. Проверьте, что он "
                             "администратор с правом публиковать.")
        return
    await state.clear()
    text, markup = await ops_view(app)
    await message.answer(f"✅ <b>Канал «{_h().esc(title)}» подключён.</b> Там уже есть проверочное сообщение.\n\n" + text,
                         reply_markup=markup)


@router.callback_query(F.data.startswith("keep:"))
async def keep_running(call: CallbackQuery, app: App) -> None:
    """«Оставить работать» из предупреждения о молчащем боте."""
    iid = int(call.data.split(":")[1])
    row = await app.db.one("SELECT user_id, bot_username FROM instances WHERE id = ?", iid)
    if not row or row["user_id"] != call.from_user.id:
        await call.answer()
        return
    await touch(app, iid)
    await call.answer("Хорошо, бот работает дальше")
    try:
        await call.message.edit_text(f"✅ Бот @{row['bot_username'] or iid} продолжает работать. "
                                     f"Отсчёт тишины начат заново ({time.strftime('%d.%m.%Y')}).")
    except Exception:
        pass
