"""Экспресс: заявки в группу исполнителей.

Заказчик пишет боту → бот пересылает сообщение форвардом в привязанную группу.
Любой исполнитель отвечает реплаем на заявку → ответ уходит заказчику в личку (форвардом или анонимно).
Заказчик может ответить реплаем на ответ исполнителя → сообщение уйдёт в группу.
"""
from __future__ import annotations

import asyncio
import html
import re
import logging
import os
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message, ReactionTypeEmoji

import baksi_kit as kit
from baksi_captcha import Captcha, menu_button
from baksi_db import ModuleDB

log = logging.getLogger("express")

MIGRATIONS = [
    """
    CREATE TABLE users (
        id          INTEGER PRIMARY KEY,
        username    TEXT,
        first_name  TEXT,
        banned      INTEGER NOT NULL DEFAULT 0,
        created_at  INTEGER NOT NULL
    );
    CREATE TABLE tickets (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL,
        status      TEXT NOT NULL DEFAULT 'open',
        control_id  INTEGER,
        created_at  INTEGER NOT NULL,
        last_at     INTEGER NOT NULL,
        closed_at   INTEGER,
        closed_by   INTEGER
    );
    CREATE INDEX tickets_user ON tickets(user_id, status);
    CREATE TABLE group_msgs (            -- сообщения бота в группе -> заказчик
        chat_id     INTEGER NOT NULL,
        message_id  INTEGER NOT NULL,
        ticket_id   INTEGER NOT NULL,
        user_id     INTEGER NOT NULL,
        created_at  INTEGER NOT NULL,
        PRIMARY KEY (chat_id, message_id)
    );
    CREATE TABLE dm_msgs (               -- ответы исполнителей у заказчика -> сообщение исполнителя в группе
        user_id     INTEGER NOT NULL,
        message_id  INTEGER NOT NULL,
        ticket_id   INTEGER NOT NULL,
        seller_id   INTEGER NOT NULL,
        group_chat  INTEGER NOT NULL,
        group_msg   INTEGER NOT NULL,
        created_at  INTEGER NOT NULL,
        PRIMARY KEY (user_id, message_id)
    );
    CREATE TABLE replies (
        ticket_id   INTEGER NOT NULL,
        seller_id   INTEGER NOT NULL,
        seller_name TEXT,
        created_at  INTEGER NOT NULL
    );
    CREATE INDEX replies_ticket ON replies(ticket_id);
    CREATE INDEX replies_time ON replies(created_at);
    """,
]

DEFAULTS = {
    "greeting": "👋 Здравствуйте! Напишите, что вам нужно: текст, фото, голосовые. Ответ придёт сюда.",
    "anonymous": False,      # ответы исполнителей копией, без имени
    "types": ["text", "photo", "video", "audio", "document"],  # что заказчик может присылать
}
# что можно разрешить; всё остальное (стикеры, GIF, кружки, опросы, геолокация) не пересылается никогда
KINDS = [("text", "Текст"), ("photo", "Фото"), ("video", "Видео"), ("audio", "Аудио и голосовые"), ("document", "Документы")]
TEXTS = [("greeting", "👋 Приветствие")]
RATE_WINDOW, RATE_LIMIT = 60, 12  # сообщений от заказчика в минуту


def now() -> int:
    return int(time.time())


def esc(text: object) -> str:
    return html.escape(str(text or ""))


class Express:
    def __init__(self, bot: Bot, db: ModuleDB) -> None:
        self.bot = bot
        self.db = db
        self.rate: dict[int, list[float]] = {}
        self.hinted: dict[int, float] = {}
        # сообщения одного заказчика обрабатываем по очереди: альбом из 5 фото приходит одновременно,
        # и без замка получилось бы 5 заявок вместо одной
        self.user_locks: dict[int, asyncio.Lock] = {}

    def opt(self, key: str):
        """Своя настройка клиента → оформление от владельца платформы → значение по умолчанию."""
        value = self.db.get(key)
        if value is not None:
            return value
        default = DEFAULTS[key]
        return kit.ui_text(key, default) if isinstance(default, str) else default

    def is_owner(self, user_id: int) -> bool:
        return user_id in kit.owner_ids()

    def lock(self, user_id: int) -> asyncio.Lock:
        if len(self.user_locks) > 5000:
            self.user_locks = {k: v for k, v in self.user_locks.items() if v.locked()}
        return self.user_locks.setdefault(user_id, asyncio.Lock())

    def flood(self, user_id: int) -> bool:
        t = time.monotonic()
        hits = [x for x in self.rate.get(user_id, []) if t - x < RATE_WINDOW]
        hits.append(t)
        self.rate[user_id] = hits
        if len(self.rate) > 20000:
            self.rate.clear()
        return len(hits) > RATE_LIMIT

    async def stats(self) -> dict:
        day, month = now() - 86400, now() - 30 * 86400
        return {
            "today": await self.db.value("SELECT COUNT(*) FROM group_msgs WHERE created_at > ?", day, default=0),
            "total": await self.db.value("SELECT COUNT(*) FROM group_msgs", default=0),
            "customers": await self.db.value("SELECT COUNT(DISTINCT user_id) FROM group_msgs", default=0),
            "replies": await self.db.value("SELECT COUNT(*) FROM replies WHERE created_at > ?", month, default=0),
            "top": await self.db.all("SELECT seller_name, COUNT(*) AS n FROM replies WHERE created_at > ? "
                                     "GROUP BY seller_id ORDER BY n DESC LIMIT 5", month),
        }


router = Router(name="express")


async def delete_later(message: Message, delay: float) -> None:
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except TelegramBadRequest:
        pass


class AdminForm(StatesGroup):
    text = State()


# --- заказчик: любое сообщение уходит в группу форвардом -------------------------------
OWNER_COMMANDS = ("/admin", "/ban", "/unban", "/bind", "/captcha")


def kind_of(message: Message) -> str | None:
    if message.photo:
        return "photo"
    if message.video:
        return "video"
    if message.audio or message.voice:
        return "audio"
    if message.document and not message.animation:
        return "document"
    if message.text:
        return "text"
    return None  # стикеры, GIF, кружки, опросы, контакты, геолокация


async def react(ex: Express, message: Message, ok: bool) -> None:
    """Заказчик видит: 👍 - доставлено исполнителям, 👎 - не доставлено."""
    try:
        await ex.bot.set_message_reaction(message.chat.id, message.message_id,
                                          [ReactionTypeEmoji(emoji="👍" if ok else "👎")])
    except TelegramBadRequest:
        pass


async def forward_to_group(message: Message, ex: Express, quiet: bool = False) -> None:
    user = message.from_user
    group = kit.bound_chat_id()
    if not group:
        if quiet:
            return
        if ex.is_owner(user.id):
            code = kit.platform().get("bind_code", "")
            await message.answer("Группа исполнителей ещё не привязана. Добавьте бота в группу и отправьте туда "
                                 f"<code>/bind {code}</code>")
        else:
            await message.answer("Бот ещё настраивается, попробуйте немного позже.")
        return
    await ex.db.run("INSERT INTO users(id, username, first_name, created_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET username = excluded.username, first_name = excluded.first_name",
                    user.id, user.username, user.first_name, now())
    if await ex.db.value("SELECT banned FROM users WHERE id = ?", user.id, default=0):
        return
    is_start = (message.text or "").startswith("/start")
    kind = kind_of(message)
    if not is_start and kind not in ex.opt("types"):
        await react(ex, message, False)
        await types_hint(ex, message)
        return
    if ex.flood(user.id):
        await react(ex, message, False)  # больше 12 сообщений в минуту - не пересылаем, чтобы не засорять группу
        return
    try:
        fwd = await ex.bot.forward_message(group, user.id, message.message_id)
    except TelegramBadRequest as exc:
        log.warning("Не удалось переслать в группу: %s", exc.message)
        await react(ex, message, False)
        return
    except TelegramForbiddenError:
        kit.notify("⚠️ Бот не может писать в группу исполнителей: проверьте, что он в группе и ему можно писать.")
        await react(ex, message, False)
        return
    if not is_start:
        await react(ex, message, True)
    await ex.db.run("INSERT OR REPLACE INTO group_msgs(chat_id, message_id, ticket_id, user_id, created_at) "
                    "VALUES (?, ?, 0, ?, ?)", group, fwd.message_id, user.id, now())


async def types_hint(ex: Express, message: Message) -> None:
    """Подсказка, что принимается, - не чаще раза в 10 минут, чтобы не спамить."""
    uid = message.from_user.id
    last = ex.hinted.get(uid)
    if last is not None and time.monotonic() - last < 600:
        return
    ex.hinted[uid] = time.monotonic()
    allowed = [title.lower() for key, title in KINDS if key in ex.opt("types")]
    await message.answer("Такие сообщения не принимаются. Можно отправить: " + (", ".join(allowed) or "ничего") + ".")


@router.message(CommandStart(), F.chat.type == "private")
async def start(message: Message, ex: Express, state: FSMContext) -> None:
    await state.clear()
    text = ex.opt("greeting")
    if ex.is_owner(message.from_user.id):
        await message.answer(text + "\n\n⚙️ Вы владелец бота: /admin - настройки и статистика.")
        return
    await message.answer(text)
    async with ex.lock(message.from_user.id):
        await forward_to_group(message, ex, quiet=True)  # /start тоже уходит в группу: видно, кто пришёл


def customer_message(message: Message) -> bool:
    """Всё от заказчика, кроме служебных команд владельца (их обрабатывают свои обработчики)."""
    return not (message.text or "").split("@")[0].split(" ")[0].lower() in OWNER_COMMANDS


def not_command(message: Message) -> bool:
    return not (message.text or "").startswith("/")


@router.message(StateFilter(None), F.chat.type == "private", customer_message)
async def from_customer(message: Message, ex: Express) -> None:
    async with ex.lock(message.from_user.id):  # альбом приходит пачкой: пересылаем по порядку
        await forward_to_group(message, ex)


# --- исполнители в группе ----------------------------------------------------------
@router.message(F.chat.type.in_({"group", "supergroup"}), F.reply_to_message, not_command)
async def seller_reply(message: Message, ex: Express) -> None:
    if message.chat.id != kit.bound_chat_id() or not message.from_user or message.from_user.is_bot:
        return
    if message.text and message.text.startswith("/"):
        return
    link = await ex.db.one("SELECT * FROM group_msgs WHERE chat_id = ? AND message_id = ?",
                           message.chat.id, message.reply_to_message.message_id)
    if not link:
        return
    banned = await ex.db.value("SELECT banned FROM users WHERE id = ?", link["user_id"], default=0)
    if banned:
        await message.reply("Заказчик заблокирован, ответ не отправлен.")
        return
    try:
        if ex.opt("anonymous"):
            sent = await ex.bot.copy_message(link["user_id"], message.chat.id, message.message_id)
            sent_id = sent.message_id
        else:
            sent = await ex.bot.forward_message(link["user_id"], message.chat.id, message.message_id)
            sent_id = sent.message_id
    except TelegramForbiddenError:
        await message.reply("Заказчик заблокировал бота, ответ не доставлен.")
        return
    except TelegramBadRequest as exc:
        await message.reply(f"Ответ не доставлен: {esc(exc.message)}")
        return
    seller = message.from_user
    await ex.db.run("INSERT OR REPLACE INTO dm_msgs(user_id, message_id, ticket_id, seller_id, group_chat, group_msg, "
                    "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", link["user_id"], sent_id, link["ticket_id"],
                    seller.id, message.chat.id, message.message_id, now())
    await ex.db.run("INSERT INTO replies(ticket_id, seller_id, seller_name, created_at) VALUES (0, ?, ?, ?)",
                    seller.id, seller.full_name, now())
    try:  # отметка «доставлено» без лишних сообщений в группе
        await ex.bot.set_message_reaction(message.chat.id, message.message_id, [ReactionTypeEmoji(emoji="👍")])
    except TelegramBadRequest:
        pass


# --- бан, разбан, закрытие: команды админов группы -------------------------------------
async def target_user(message: Message, ex: Express, command: CommandObject) -> int | None:
    """Кого касается команда: реплай на сообщение заявки в группе или ID в команде."""
    arg = (command.args or "").strip()
    if arg.lstrip("-").isdigit():
        return int(arg)
    if message.reply_to_message and message.chat.type != "private":
        link = await ex.db.one("SELECT user_id FROM group_msgs WHERE chat_id = ? AND message_id = ?",
                               message.chat.id, message.reply_to_message.message_id)
        if link:
            return link["user_id"]
    return None


async def may_ban(message: Message, ex: Express) -> bool:
    """В группе - админы с правом банить (и владелец бота), в личке бота - только владелец."""
    if not message.from_user:
        return False
    if message.chat.type == "private":
        return ex.is_owner(message.from_user.id)
    if message.chat.id != kit.bound_chat_id():
        return False
    return await kit.is_group_admin(ex.bot, message.chat.id, message.from_user.id, need_ban=True,
                                    sender_chat_id=message.sender_chat.id if message.sender_chat else None)


async def reply_note(message: Message, text: str, keep: bool = True) -> None:
    note = await message.reply(text)
    if not keep:
        asyncio.create_task(delete_later(note, 10))


@router.message(Command("ban"))
async def ban_cmd(message: Message, command: CommandObject, ex: Express) -> None:
    if not await may_ban(message, ex):
        if message.chat.type != "private" and message.chat.id == kit.bound_chat_id():
            await reply_note(message, "Банить могут админы группы с правом блокировки.", keep=False)
        return
    uid = await target_user(message, ex, command)
    if not uid:
        await reply_note(message, "Ответьте /ban на сообщение заказчика или напишите /ban ID.", keep=False)
        return
    await ex.db.run("INSERT INTO users(id, created_at, banned) VALUES (?, ?, 1) ON CONFLICT(id) DO UPDATE SET banned = 1",
                    uid, now())
    await reply_note(message, f"⛔️ Заказчик <code>{uid}</code> заблокирован: его сообщения больше не приходят.\n"
                              f"Разблокировать: <code>/unban {uid}</code>")


@router.message(Command("unban"))
async def unban_cmd(message: Message, command: CommandObject, ex: Express) -> None:
    if not await may_ban(message, ex):
        return
    uid = await target_user(message, ex, command)
    if not uid:
        await reply_note(message, "Ответьте /unban на сообщение заказчика или напишите /unban ID.", keep=False)
        return
    if not await ex.db.value("SELECT banned FROM users WHERE id = ?", uid, default=0):
        await reply_note(message, f"Заказчик <code>{uid}</code> не заблокирован.", keep=False)
        return
    await ex.db.run("UPDATE users SET banned = 0 WHERE id = ?", uid)
    await reply_note(message, f"✅ Заказчик <code>{uid}</code> разблокирован.")


@router.callback_query(F.data.startswith("ex:"))
async def old_buttons(call: CallbackQuery) -> None:  # кнопки из прежней версии в старых сообщениях
    await call.answer("Кнопок больше нет: админы группы банят командой /ban реплаем на сообщение.", show_alert=True)


# --- админка владельца: по разделам ------------------------------------------------------
async def stats_text(ex: Express) -> str:
    st = await ex.stats()
    group = kit.state().get("title") or "не привязана"
    lines = ["⚙️ <b>Экспресс</b>\n",
             f"👥 Группа: <b>{esc(group)}</b>",
             f"📨 Сообщений сегодня: <b>{st['today']}</b> · всего: <b>{st['total']}</b>",
             f"🙋 Заказчиков: <b>{st['customers']}</b> · ответов за 30 дней: <b>{st['replies']}</b>"]
    if st["top"]:
        lines.append("\n<b>Активные исполнители, 30 дней</b>")
        lines += [f"{i}. {esc(r['seller_name'])} - {r['n']}" for i, r in enumerate(st["top"], 1)]
    if not kit.bound_chat_id():
        lines.append(f"\nПривязать группу: добавьте бота и отправьте <code>/bind {kit.platform().get('bind_code', '')}</code>")
    return "\n".join(lines)


def home_kb():
    return kit.kb([[kit.btn("✏️ Тексты", "exa:texts"), kit.btn("⚙️ Настройки", "exa:set")],
                   [kit.btn("⛔️ Баны", "exa:bans"), menu_button()],
                   [kit.btn("🔄 Обновить", "exa:home")]])


BACK = [kit.btn("⬅️ Назад", "exa:home")]


async def show(target: Message | CallbackQuery, text: str, markup) -> None:
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            pass
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup)


@router.message(Command("admin"), F.chat.type == "private")
async def admin(message: Message, ex: Express, state: FSMContext) -> None:
    if not ex.is_owner(message.from_user.id):
        return
    await state.clear()
    await show(message, await stats_text(ex), home_kb())


@router.callback_query(F.data == "exa:home")
async def admin_home(call: CallbackQuery, ex: Express, state: FSMContext) -> None:
    if not ex.is_owner(call.from_user.id):
        return
    await state.clear()
    await show(call, await stats_text(ex), home_kb())


def preview(text: str, size: int = 70) -> str:
    plain = " ".join(html.unescape(re.sub(r"<[^>]+>", "", str(text or ""))).split())
    return plain if len(plain) <= size else plain[:size - 1] + "…"


@router.callback_query(F.data == "exa:texts")
async def admin_texts(call: CallbackQuery, ex: Express, state: FSMContext) -> None:
    if not ex.is_owner(call.from_user.id):
        return
    await state.clear()
    lines = ["✏️ <b>Тексты</b>\nЧто видит заказчик. Нажмите, чтобы изменить.\n"]
    for key, label in TEXTS:
        lines.append(f"<b>{label}</b>" + (" ✎" if ex.db.get(key) is not None else "") + f"\n<i>{esc(preview(ex.opt(key)))}</i>\n")
    rows = [[kit.btn(label, f"exa:txt:{key}")] for key, label in TEXTS]
    await show(call, "\n".join(lines), kit.kb(rows + [BACK]))


def settings_view(ex: Express):
    allowed = [title.lower() for key, title in KINDS if key in ex.opt("types")]
    text = ("⚙️ <b>Настройки</b>\n\n"
            f"1. 🕶 Ответы заказчику: <b>{'копией, без имени исполнителя' if ex.opt('anonymous') else 'форвардом, с именем'}</b>\n"
            f"2. 📎 Заказчик может присылать: <b>{', '.join(allowed) or 'ничего'}</b>\n\n"
            "Заказчик пишет боту - сообщение уходит в группу форвардом, у него появляется 👍. Ответить можно реплаем "
            "в любое время.")
    return text, kit.kb([[kit.btn("🕶 Анонимно: " + ("вкл" if ex.opt("anonymous") else "выкл"), "exa:tog:anonymous")],
                         [kit.btn("📎 Типы сообщений", "exa:types")], BACK])


def types_view(ex: Express):
    on = ex.opt("types")
    text = ("📎 <b>Типы сообщений</b>\nЧто заказчик может присылать. Остальное (стикеры, GIF, кружки, опросы, "
            "геолокацию) бот не пересылает никогда, а заказчик видит 👎 и подсказку.")
    rows = [[kit.btn(("✅ " if key in on else "▫️ ") + title, f"exa:type:{key}")] for key, title in KINDS]
    return text, kit.kb(rows + [[kit.btn("⬅️ Назад", "exa:set")]])


@router.callback_query(F.data == "exa:types")
async def admin_types(call: CallbackQuery, ex: Express) -> None:
    if ex.is_owner(call.from_user.id):
        await show(call, *types_view(ex))


@router.callback_query(F.data.startswith("exa:type:"))
async def admin_type_toggle(call: CallbackQuery, ex: Express) -> None:
    if not ex.is_owner(call.from_user.id):
        return
    key = call.data.split(":")[2]
    if key not in dict(KINDS):
        return
    on = list(ex.opt("types"))
    if key in on:
        if len(on) == 1:
            await call.answer("Должен остаться хотя бы один тип", show_alert=True)
            return
        on.remove(key)
    else:
        on.append(key)
    await ex.db.set("types", [k for k, _ in KINDS if k in on])
    await show(call, *types_view(ex))


@router.callback_query(F.data == "exa:set")
async def admin_settings(call: CallbackQuery, ex: Express, state: FSMContext) -> None:
    if not ex.is_owner(call.from_user.id):
        return
    await state.clear()
    await show(call, *settings_view(ex))


@router.callback_query(F.data.startswith("exa:tog:"))
async def admin_toggle(call: CallbackQuery, ex: Express, state: FSMContext) -> None:
    if not ex.is_owner(call.from_user.id):
        return
    if call.data.split(":")[2] == "anonymous":
        await ex.db.set("anonymous", not ex.opt("anonymous"))
    await admin_settings(call, ex, state)


@router.callback_query(F.data.startswith("exa:txt:"))
async def admin_edit(call: CallbackQuery, ex: Express, state: FSMContext) -> None:
    if not ex.is_owner(call.from_user.id):
        return
    _, kind, key = call.data.split(":")
    if key not in DEFAULTS:
        return
    await state.set_state(AdminForm.text)
    await state.update_data(key=key)
    rows = [[kit.btn("↩️ Вернуть стандартный", f"exa:rst:{key}")]] if ex.db.get(key) is not None else []
    await show(call, f"<b>{dict(TEXTS).get(key, key)}</b>\n\nСейчас:\n<blockquote>{ex.opt(key)}</blockquote>\n"
                     "Отправьте новый текст. Форматирование и премиум-эмодзи сохранятся.",
               kit.kb(rows + [[kit.btn("⬅️ Назад", "exa:texts")]]))


@router.callback_query(F.data.startswith("exa:rst:"))
async def admin_reset(call: CallbackQuery, ex: Express, state: FSMContext) -> None:
    if not ex.is_owner(call.from_user.id):
        return
    key = call.data.split(":")[2]
    if key in dict(TEXTS):
        await ex.db.set(key, None)
    await call.answer("Вернули стандартный текст")
    await admin_texts(call, ex, state)


@router.message(AdminForm.text, F.chat.type == "private")
async def admin_text_input(message: Message, ex: Express, state: FSMContext) -> None:
    if not ex.is_owner(message.from_user.id) or not message.text:
        return
    key = (await state.get_data()).get("key")
    await ex.db.set(key, message.html_text[:3500])
    await state.clear()
    await message.answer(f"✅ <b>Сохранено.</b> Теперь заказчики видят так:\n\n{message.html_text[:3500]}",
                         reply_markup=kit.kb([[kit.btn("✏️ Тексты", "exa:texts"), kit.btn("⚙️ Админка", "exa:home")]]))


@router.callback_query(F.data == "exa:bans")
async def admin_bans(call: CallbackQuery, ex: Express) -> None:
    if not ex.is_owner(call.from_user.id):
        return
    rows = await ex.db.all("SELECT * FROM users WHERE banned = 1 ORDER BY created_at DESC LIMIT 30")
    text = ("⛔️ <b>Баны</b>\n\nВ группе: админ с правом блокировки отвечает <code>/ban</code> на сообщение заказчика. "
            "Разбан: <code>/unban</code> тем же способом или <code>/unban ID</code> (в группе или здесь).\n\n"
            + ("\n".join(f"• {esc(r['first_name'] or 'без имени')} · <code>{r['id']}</code>" for r in rows) if rows
               else "Сейчас никто не заблокирован."))
    buttons = [[kit.btn(f"✅ {preview(r['first_name'] or r['id'], 20)}", f"exa:unban:{r['id']}")] for r in rows]
    await show(call, text, kit.kb(buttons + [BACK]))


@router.callback_query(F.data.startswith("exa:unban:"))
async def admin_unban(call: CallbackQuery, ex: Express) -> None:
    if not ex.is_owner(call.from_user.id):
        return
    await ex.db.run("UPDATE users SET banned = 0 WHERE id = ?", int(call.data.split(":")[2]))
    await call.answer("Разблокирован")
    await admin_bans(call, ex)


async def cleanup(db: ModuleDB) -> None:
    while True:
        await asyncio.sleep(3600)
        cutoff = now() - 365 * 86400  # ответить реплаем можно в любое время: связи храним год
        await db.run("DELETE FROM group_msgs WHERE created_at < ?", cutoff)
        await db.run("DELETE FROM dm_msgs WHERE created_at < ?", cutoff)


async def main() -> None:
    kit.setup_logging()
    token = os.environ.get("BOT_TOKEN", "")
    if not token:
        raise SystemExit("BOT_TOKEN не задан")
    db = ModuleDB(Path(kit.data_dir()) / "express.db", MIGRATIONS)
    await db.connect()
    bot = Bot(token, session=kit.session(), default=DefaultBotProperties(parse_mode="HTML"))
    ex = Express(bot, db)
    dp = Dispatcher(storage=MemoryStorage(), ex=ex)
    dp.include_router(kit.bind_router())
    dp.include_router(router)

    async def known_customer(uid: int) -> bool:  # уже писавшие заказчики проверку не проходят
        return bool(await db.one("SELECT 1 FROM group_msgs WHERE user_id = ? LIMIT 1", uid))
    captcha = await Captcha(back="exa:home", exempt=known_customer).setup(dp)
    await bot.delete_webhook(drop_pending_updates=False)
    await kit.ready(bot)
    task = asyncio.create_task(cleanup(db))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        task.cancel()
        await captcha.close()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
