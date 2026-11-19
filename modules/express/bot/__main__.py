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
    "greeting": "👋 Здравствуйте! Опишите, что вам нужно, одним или несколькими сообщениями: текст, фото, "
                "голосовые. Заявку увидят все исполнители, ответы придут сюда.",
    "accepted": "✅ Заявка отправлена исполнителям. Ответы придут в этот чат.",
    "closed_user": "Заявка закрыта. Если нужно что-то ещё, просто напишите снова.",
    "anonymous": False,      # ответы исполнителей копией, без имени
    "cooldown": 60,          # секунд между новыми заявками
    "auto_close_hours": 24,  # закрыть заявку без активности
}
CARD_TEXT = "📨 Заявка #{номер} от {имя}\nОтветьте реплаем на заявку, ответ уйдёт заказчику."
TEXTS = [("greeting", "👋 Приветствие"), ("accepted", "✅ Заявка принята"), ("closed_user", "🔒 Заявка закрыта")]
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

    async def open_ticket(self, user_id: int) -> tuple[int, bool]:
        """Текущая открытая заявка или новая. -> (id, создана_сейчас)."""
        row = await self.db.one("SELECT * FROM tickets WHERE user_id = ? AND status = 'open' ORDER BY id DESC LIMIT 1",
                                user_id)
        limit = int(self.opt("auto_close_hours")) * 3600
        if row and now() - row["last_at"] < limit:
            await self.db.run("UPDATE tickets SET last_at = ? WHERE id = ?", now(), row["id"])
            return row["id"], False
        if row:
            await self.db.run("UPDATE tickets SET status = 'closed', closed_at = ? WHERE id = ?", now(), row["id"])
        return await self.db.run("INSERT INTO tickets(user_id, created_at, last_at) VALUES (?, ?, ?)",
                                 user_id, now(), now()), True

    async def last_created(self, user_id: int) -> int:
        return await self.db.value("SELECT MAX(created_at) FROM tickets WHERE user_id = ?", user_id, default=0)


router = Router(name="express")


async def delete_later(message: Message, delay: float) -> None:
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except TelegramBadRequest:
        pass


class AdminForm(StatesGroup):
    text = State()
    number = State()


# --- заказчик -------------------------------------------------------------------
@router.message(CommandStart(), F.chat.type == "private")
async def start(message: Message, ex: Express, state: FSMContext) -> None:
    await state.clear()
    u = message.from_user
    await ex.db.run("INSERT INTO users(id, username, first_name, created_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET username = excluded.username, first_name = excluded.first_name",
                    u.id, u.username, u.first_name, now())
    text = ex.opt("greeting")
    if ex.is_owner(u.id):
        text += "\n\n⚙️ Вы владелец бота: /admin - настройки и статистика."
    await message.answer(text)


def not_command(message: Message) -> bool:
    return not (message.text or "").startswith("/")


@router.message(StateFilter(None), F.chat.type == "private", not_command)
async def from_customer(message: Message, ex: Express) -> None:
    async with ex.lock(message.from_user.id):
        await _from_customer(message, ex)


async def _from_customer(message: Message, ex: Express) -> None:
    user = message.from_user
    group = kit.bound_chat_id()
    if not group:
        if ex.is_owner(user.id):
            code = kit.platform().get("bind_code", "")
            await message.answer("Группа исполнителей ещё не привязана. Добавьте бота в группу и отправьте туда "
                                 f"<code>/bind {code}</code>")
        else:
            await message.answer("Бот ещё настраивается, попробуйте немного позже.")
        return
    row = await ex.db.one("SELECT banned FROM users WHERE id = ?", user.id)
    if row is None:
        await ex.db.run("INSERT OR IGNORE INTO users(id, username, first_name, created_at) VALUES (?, ?, ?, ?)",
                        user.id, user.username, user.first_name, now())
    elif row["banned"]:
        return
    if ex.flood(user.id):
        await message.answer("Слишком много сообщений подряд. Подождите минуту.")
        return
    # ответ на сообщение конкретного исполнителя
    target = None
    if message.reply_to_message:
        target = await ex.db.one("SELECT * FROM dm_msgs WHERE user_id = ? AND message_id = ?",
                                 user.id, message.reply_to_message.message_id)
    has_open = await ex.db.one("SELECT 1 FROM tickets WHERE user_id = ? AND status = 'open'", user.id)
    if not has_open and not target:
        cooldown = int(ex.opt("cooldown"))
        wait = await ex.last_created(user.id) + cooldown - now()
        if wait > 0:
            await message.answer(f"Новую заявку можно отправить через {wait} с.")
            return
    ticket_id, created = await ex.open_ticket(user.id)
    try:
        fwd = await ex.bot.forward_message(group, user.id, message.message_id)
    except TelegramBadRequest as exc:
        log.warning("Не удалось переслать в группу: %s", exc.message)
        await message.answer("Не получилось отправить заявку. Попробуйте ещё раз чуть позже.")
        return
    except TelegramForbiddenError:
        kit.notify("⚠️ Бот не может писать в группу исполнителей: проверьте, что он в группе и ему можно писать.")
        await message.answer("Не получилось отправить заявку. Попробуйте позже.")
        return
    await ex.db.run("INSERT OR REPLACE INTO group_msgs(chat_id, message_id, ticket_id, user_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?)", group, fwd.message_id, ticket_id, user.id, now())
    if target:
        # подсказать исполнителю, что это ответ ему
        try:
            hint = await ex.bot.send_message(group, "↩️ Ответ заказчика на это сообщение ⬆️",
                                             reply_to_message_id=target["group_msg"], allow_sending_without_reply=True,
                                             disable_notification=True)
            await ex.db.run("INSERT OR REPLACE INTO group_msgs(chat_id, message_id, ticket_id, user_id, created_at) "
                            "VALUES (?, ?, ?, ?, ?)", group, hint.message_id, ticket_id, user.id, now())
        except TelegramBadRequest:
            pass
    if created:
        name = esc(user.full_name)
        who = name + (f" (@{esc(user.username)})" if user.username else "") + f" · ID <code>{user.id}</code>"
        card = kit.ui_text("card", CARD_TEXT).replace("{номер}", str(ticket_id)).replace("{имя}", who)
        control = await ex.bot.send_message(group, card, reply_to_message_id=fwd.message_id, disable_notification=True)
        await ex.db.run("UPDATE tickets SET control_id = ? WHERE id = ?", control.message_id, ticket_id)
        await ex.db.run("INSERT OR REPLACE INTO group_msgs(chat_id, message_id, ticket_id, user_id, created_at) "
                        "VALUES (?, ?, ?, ?, ?)", group, control.message_id, ticket_id, user.id, now())
        await message.answer(ex.opt("accepted"))


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
    ticket = await ex.db.one("SELECT * FROM tickets WHERE id = ?", link["ticket_id"])
    if ticket and ticket["status"] == "closed":
        note = await message.reply("Эта заявка уже закрыта, ответ не отправлен.")
        asyncio.create_task(delete_later(note, 8))
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
    await ex.db.run("INSERT INTO replies(ticket_id, seller_id, seller_name, created_at) VALUES (?, ?, ?, ?)",
                    link["ticket_id"], seller.id, seller.full_name, now())
    await ex.db.run("UPDATE tickets SET last_at = ? WHERE id = ?", now(), link["ticket_id"])
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
        await reply_note(message, "Ответьте /ban на сообщение заявки или напишите /ban ID.", keep=False)
        return
    await ex.db.run("INSERT INTO users(id, created_at, banned) VALUES (?, ?, 1) ON CONFLICT(id) DO UPDATE SET banned = 1",
                    uid, now())
    await ex.db.run("UPDATE tickets SET status = 'closed', closed_at = ?, closed_by = ? WHERE user_id = ? AND status = 'open'",
                    now(), message.from_user.id, uid)
    await reply_note(message, f"⛔️ Заказчик <code>{uid}</code> заблокирован: его заявки больше не приходят.\n"
                              f"Разблокировать: <code>/unban {uid}</code>")


@router.message(Command("unban"))
async def unban_cmd(message: Message, command: CommandObject, ex: Express) -> None:
    if not await may_ban(message, ex):
        return
    uid = await target_user(message, ex, command)
    if not uid:
        await reply_note(message, "Ответьте /unban на сообщение заявки или напишите /unban ID.", keep=False)
        return
    if not await ex.db.value("SELECT banned FROM users WHERE id = ?", uid, default=0):
        await reply_note(message, f"Заказчик <code>{uid}</code> не заблокирован.", keep=False)
        return
    await ex.db.run("UPDATE users SET banned = 0 WHERE id = ?", uid)
    await reply_note(message, f"✅ Заказчик <code>{uid}</code> разблокирован.")


@router.message(Command("close"), F.chat.type.in_({"group", "supergroup"}))
async def close_cmd(message: Message, command: CommandObject, ex: Express) -> None:
    """Закрыть заявку раньше автозакрытия: реплаем на её сообщение, любой админ группы."""
    if message.chat.id != kit.bound_chat_id() or not message.from_user or not message.reply_to_message:
        return
    if not await kit.is_group_admin(ex.bot, message.chat.id, message.from_user.id, need_ban=False,
                                    sender_chat_id=message.sender_chat.id if message.sender_chat else None):
        return
    link = await ex.db.one("SELECT ticket_id FROM group_msgs WHERE chat_id = ? AND message_id = ?",
                           message.chat.id, message.reply_to_message.message_id)
    ticket = await ex.db.one("SELECT * FROM tickets WHERE id = ?", link["ticket_id"]) if link else None
    if not ticket:
        return
    if ticket["status"] != "closed":
        await ex.db.run("UPDATE tickets SET status = 'closed', closed_at = ?, closed_by = ? WHERE id = ?",
                        now(), message.from_user.id, ticket["id"])
        try:
            await ex.bot.send_message(ticket["user_id"], ex.opt("closed_user"))
        except (TelegramForbiddenError, TelegramBadRequest):
            pass
    await reply_note(message, f"✅ Заявка #{ticket['id']} закрыта.", keep=False)


@router.callback_query(F.data.startswith("ex:"))
async def old_buttons(call: CallbackQuery) -> None:  # кнопки из прежней версии в старых сообщениях
    await call.answer("Кнопок больше нет: админы группы банят командой /ban реплаем на заявку.", show_alert=True)


# --- админка владельца: по разделам ------------------------------------------------------
async def stats_text(ex: Express) -> str:
    day = now() - 86400
    total = await ex.db.value("SELECT COUNT(*) FROM tickets", default=0)
    today = await ex.db.value("SELECT COUNT(*) FROM tickets WHERE created_at > ?", day, default=0)
    answered = await ex.db.value("SELECT COUNT(DISTINCT ticket_id) FROM replies r JOIN tickets t ON t.id = r.ticket_id "
                                 "WHERE t.created_at > ?", now() - 30 * 86400, default=0)
    month = await ex.db.value("SELECT COUNT(*) FROM tickets WHERE created_at > ?", now() - 30 * 86400, default=0)
    users = await ex.db.value("SELECT COUNT(*) FROM users", default=0)
    top = await ex.db.all("SELECT seller_name, COUNT(*) AS n FROM replies WHERE created_at > ? GROUP BY seller_id "
                          "ORDER BY n DESC LIMIT 5", now() - 30 * 86400)
    group = kit.state().get("title") or "не привязана"
    lines = ["⚙️ <b>Экспресс</b>\n",
             f"👥 Группа: <b>{esc(group)}</b>",
             f"📨 Заявок сегодня: <b>{today}</b> · всего: <b>{total}</b>",
             f"💬 С ответом за 30 дней: <b>{answered}</b> из {month}",
             f"🙋 Заказчиков: <b>{users}</b>"]
    if top:
        lines.append("\n<b>Активные исполнители, 30 дней</b>")
        lines += [f"{i}. {esc(r['seller_name'])} - {r['n']}" for i, r in enumerate(top, 1)]
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
    text = ("⚙️ <b>Настройки</b>\n\n"
            f"🕶 Ответы заказчику: <b>{'копией, без имени исполнителя' if ex.opt('anonymous') else 'форвардом, с именем'}</b>\n"
            f"⏱ Пауза между новыми заявками: <b>{ex.opt('cooldown')} с</b>\n"
            f"🕓 Заявка закрывается сама через <b>{ex.opt('auto_close_hours')} ч</b> тишины\n\n"
            "Закрыть раньше: админ группы отвечает /close на заявку.")
    rows = [[kit.btn("🕶 Анонимно: " + ("вкл" if ex.opt("anonymous") else "выкл"), "exa:tog:anonymous")],
            [kit.btn(f"⏱ Пауза: {ex.opt('cooldown')} с", "exa:num:cooldown"),
             kit.btn(f"🕓 Закрытие: {ex.opt('auto_close_hours')} ч", "exa:num:auto_close_hours")],
            BACK]
    return text, kit.kb(rows)


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


@router.callback_query(F.data.startswith("exa:txt:") | F.data.startswith("exa:num:"))
async def admin_edit(call: CallbackQuery, ex: Express, state: FSMContext) -> None:
    if not ex.is_owner(call.from_user.id):
        return
    _, kind, key = call.data.split(":")
    if key not in DEFAULTS:
        return
    await state.set_state(AdminForm.text if kind == "txt" else AdminForm.number)
    await state.update_data(key=key)
    if kind == "txt":
        rows = [[kit.btn("↩️ Вернуть стандартный", f"exa:rst:{key}")]] if ex.db.get(key) is not None else []
        await show(call, f"<b>{dict(TEXTS).get(key, key)}</b>\n\nСейчас:\n<blockquote>{ex.opt(key)}</blockquote>\n"
                         "Отправьте новый текст. Форматирование и премиум-эмодзи сохранятся.",
                   kit.kb(rows + [[kit.btn("⬅️ Назад", "exa:texts")]]))
    else:
        hint = "в секундах, например 60" if key == "cooldown" else "в часах, например 24"
        await show(call, f"Сейчас: <b>{ex.opt(key)}</b>. Отправьте число {hint}.", kit.kb([[kit.btn("⬅️ Назад", "exa:set")]]))


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


@router.message(AdminForm.number, F.chat.type == "private")
async def admin_number_input(message: Message, ex: Express, state: FSMContext) -> None:
    if not ex.is_owner(message.from_user.id):
        return
    key = (await state.get_data()).get("key")
    try:
        value = int((message.text or "").strip())
        if value < 0 or value > 10 ** 6:
            raise ValueError
    except ValueError:
        await message.answer("⚠️ <b>Не сохранено.</b> Нужно целое число, например 60")
        return
    if key == "auto_close_hours":
        value = max(1, value)
    await ex.db.set(key, value)
    await state.clear()
    text, markup = settings_view(ex)
    await message.answer("✅ <b>Сохранено.</b>\n\n" + text, reply_markup=markup)


@router.callback_query(F.data == "exa:bans")
async def admin_bans(call: CallbackQuery, ex: Express) -> None:
    if not ex.is_owner(call.from_user.id):
        return
    rows = await ex.db.all("SELECT * FROM users WHERE banned = 1 ORDER BY created_at DESC LIMIT 30")
    text = ("⛔️ <b>Баны</b>\n\nВ группе: админ с правом блокировки отвечает <code>/ban</code> на сообщение заявки. "
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
        cutoff = now() - 60 * 86400  # связи для ответов храним 60 дней
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
        return bool(await db.one("SELECT 1 FROM tickets WHERE user_id = ? LIMIT 1", uid))
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
