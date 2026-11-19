"""Экспресс: заявки в группу исполнителей.

Заказчик пишет боту → бот пересылает сообщение форвардом в привязанную группу.
Любой исполнитель отвечает реплаем на заявку → ответ уходит заказчику в личку (форвардом или анонимно).
Заказчик может ответить реплаем на ответ исполнителя → сообщение уйдёт в группу.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message, ReactionTypeEmoji

import baksi_kit as kit
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
    "close_by_anyone": False,
}
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
        self.admin_cache: dict[tuple[int, int], tuple[bool, float]] = {}

    def opt(self, key: str):
        return self.db.get(key, DEFAULTS[key])

    def is_owner(self, user_id: int) -> bool:
        return user_id in kit.owner_ids()

    async def can_moderate(self, chat_id: int, user_id: int) -> bool:
        if self.is_owner(user_id) or self.opt("close_by_anyone"):
            return True
        key = (chat_id, user_id)
        cached = self.admin_cache.get(key)
        if cached and time.monotonic() - cached[1] < 300:
            return cached[0]
        try:
            member = await self.bot.get_chat_member(chat_id, user_id)
            ok = member.status in ("creator", "administrator")
        except TelegramBadRequest:
            ok = False
        self.admin_cache[key] = (ok, time.monotonic())
        return ok

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
        control = await ex.bot.send_message(
            group, f"📨 Заявка #{ticket_id} от {name}" + (f" (@{esc(user.username)})" if user.username else "") +
            "\nОтветьте реплаем на заявку, ответ уйдёт заказчику.",
            reply_to_message_id=fwd.message_id,
            reply_markup=kit.kb([[kit.btn("✅ Закрыть заявку", f"ex:close:{ticket_id}", style="success"),
                                  kit.btn("⛔️ Бан", f"ex:ban:{user.id}", style="danger")]]),
            disable_notification=True)
        await ex.db.run("UPDATE tickets SET control_id = ? WHERE id = ?", control.message_id, ticket_id)
        await ex.db.run("INSERT OR REPLACE INTO group_msgs(chat_id, message_id, ticket_id, user_id, created_at) "
                        "VALUES (?, ?, ?, ?, ?)", group, control.message_id, ticket_id, user.id, now())
        await message.answer(ex.opt("accepted"))


# --- исполнители в группе ----------------------------------------------------------
@router.message(F.chat.type.in_({"group", "supergroup"}), F.reply_to_message)
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
        await ex.bot.set_message_reaction(message.chat.id, message.message_id, [ReactionTypeEmoji(emoji="👌")])
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("ex:close:"))
async def close_ticket(call: CallbackQuery, ex: Express) -> None:
    ticket_id = int(call.data.split(":")[2])
    if not await ex.can_moderate(call.message.chat.id, call.from_user.id):
        await call.answer("Закрывать заявки могут админы группы", show_alert=True)
        return
    ticket = await ex.db.one("SELECT * FROM tickets WHERE id = ?", ticket_id)
    if not ticket:
        await call.answer()
        return
    if ticket["status"] != "closed":
        await ex.db.run("UPDATE tickets SET status = 'closed', closed_at = ?, closed_by = ? WHERE id = ?",
                        now(), call.from_user.id, ticket_id)
        try:
            await ex.bot.send_message(ticket["user_id"], ex.opt("closed_user"))
        except (TelegramForbiddenError, TelegramBadRequest):
            pass
    sellers = await ex.db.value("SELECT COUNT(DISTINCT seller_id) FROM replies WHERE ticket_id = ?", ticket_id,
                                default=0)
    try:
        await call.message.edit_text(f"✅ Заявка #{ticket_id} закрыта · ответили исполнителей: {sellers}",
                                     reply_markup=kit.kb([[kit.btn("⛔️ Бан", f"ex:ban:{ticket['user_id']}")]]))
    except TelegramBadRequest:
        pass
    await call.answer("Заявка закрыта")


@router.callback_query(F.data.startswith("ex:ban:") | F.data.startswith("ex:unban:"))
async def ban_user(call: CallbackQuery, ex: Express) -> None:
    _, action, uid = call.data.split(":")
    in_group = call.message.chat.type != "private"
    if (in_group and not await ex.can_moderate(call.message.chat.id, call.from_user.id)) or \
            (not in_group and not ex.is_owner(call.from_user.id)):
        await call.answer("Банить могут админы группы", show_alert=True)
        return
    await ex.db.run("UPDATE users SET banned = ? WHERE id = ?", int(action == "ban"), int(uid))
    if action == "ban":
        await ex.db.run("UPDATE tickets SET status = 'closed', closed_at = ?, closed_by = ? WHERE user_id = ? "
                        "AND status = 'open'", now(), call.from_user.id, int(uid))
    await call.answer("Заказчик заблокирован" if action == "ban" else "Разблокирован", show_alert=True)
    if not in_group:
        await admin_bans(call, ex)


# --- админка владельца ------------------------------------------------------------
async def admin_text(ex: Express) -> str:
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
    lines = [f"⚙️ <b>Экспресс: админка</b>\n",
             f"👥 Группа исполнителей: <b>{esc(group)}</b>",
             f"📨 Заявок: сегодня <b>{today}</b>, всего <b>{total}</b>",
             f"💬 С ответом за 30 дней: <b>{answered}</b> из {month}",
             f"🙋 Заказчиков: <b>{users}</b>",
             f"🕶 Ответы исполнителей: <b>{'анонимно' if ex.opt('anonymous') else 'форвардом'}</b>"]
    if top:
        lines.append("\n<b>Активные исполнители (30 дней)</b>")
        lines += [f"{i}. {esc(r['seller_name'])} - {r['n']}" for i, r in enumerate(top, 1)]
    if not kit.bound_chat_id():
        lines.append(f"\nПривязать группу: добавьте бота и отправьте <code>/bind "
                     f"{kit.platform().get('bind_code', '')}</code>")
    return "\n".join(lines)


def admin_kb(ex: Express):
    return kit.kb([
        [kit.btn("✏️ Приветствие", "exa:txt:greeting"), kit.btn("✏️ «Заявка принята»", "exa:txt:accepted")],
        [kit.btn("✏️ «Заявка закрыта»", "exa:txt:closed_user")],
        [kit.btn(f"🕶 Анонимные ответы: {'вкл' if ex.opt('anonymous') else 'выкл'}", "exa:tog:anonymous")],
        [kit.btn(f"🔐 Закрывать может любой: {'да' if ex.opt('close_by_anyone') else 'только админы'}",
                 "exa:tog:close_by_anyone")],
        [kit.btn(f"⏱ Пауза между заявками: {ex.opt('cooldown')} с", "exa:num:cooldown"),
         kit.btn(f"🕓 Автозакрытие: {ex.opt('auto_close_hours')} ч", "exa:num:auto_close_hours")],
        [kit.btn("⛔️ Заблокированные", "exa:bans"), kit.btn("🔄 Обновить", "exa:home")],
    ])


@router.message(Command("admin"), F.chat.type == "private")
async def admin(message: Message, ex: Express, state: FSMContext) -> None:
    if not ex.is_owner(message.from_user.id):
        return
    await state.clear()
    await message.answer(await admin_text(ex), reply_markup=admin_kb(ex))


@router.callback_query(F.data == "exa:home")
async def admin_home(call: CallbackQuery, ex: Express, state: FSMContext) -> None:
    if not ex.is_owner(call.from_user.id):
        return
    await state.clear()
    try:
        await call.message.edit_text(await admin_text(ex), reply_markup=admin_kb(ex))
    except TelegramBadRequest:
        pass
    await call.answer()


@router.callback_query(F.data.startswith("exa:tog:"))
async def admin_toggle(call: CallbackQuery, ex: Express, state: FSMContext) -> None:
    if not ex.is_owner(call.from_user.id):
        return
    key = call.data.split(":")[2]
    if key in ("anonymous", "close_by_anyone"):
        await ex.db.set(key, not ex.opt(key))
    await admin_home(call, ex, state)


@router.callback_query(F.data.startswith("exa:txt:") | F.data.startswith("exa:num:"))
async def admin_edit(call: CallbackQuery, ex: Express, state: FSMContext) -> None:
    if not ex.is_owner(call.from_user.id):
        return
    _, kind, key = call.data.split(":")
    if key not in DEFAULTS:
        return
    await state.set_state(AdminForm.text if kind == "txt" else AdminForm.number)
    await state.update_data(key=key)
    current = ex.opt(key)
    prompt = (f"Сейчас:\n<blockquote>{esc(current)}</blockquote>\nОтправьте новый текст. Форматирование и "
              "премиум-эмодзи сохраняются." if kind == "txt" else f"Сейчас: {current}. Отправьте число.")
    await call.message.answer(prompt, reply_markup=kit.kb([[kit.btn("Отмена", "exa:home")]]))
    await call.answer()


@router.message(AdminForm.text, F.chat.type == "private")
async def admin_text_input(message: Message, ex: Express, state: FSMContext) -> None:
    if not ex.is_owner(message.from_user.id) or not message.text:
        return
    key = (await state.get_data()).get("key")
    await ex.db.set(key, message.html_text[:3500])
    await state.clear()
    await message.answer("✅ Сохранено")
    await message.answer(await admin_text(ex), reply_markup=admin_kb(ex))


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
        await message.answer("Нужно целое число, например 60")
        return
    if key == "auto_close_hours":
        value = max(1, value)
    await ex.db.set(key, value)
    await state.clear()
    await message.answer("✅ Сохранено")
    await message.answer(await admin_text(ex), reply_markup=admin_kb(ex))


@router.callback_query(F.data == "exa:bans")
async def admin_bans(call: CallbackQuery, ex: Express) -> None:
    if not ex.is_owner(call.from_user.id):
        return
    rows = await ex.db.all("SELECT * FROM users WHERE banned = 1 ORDER BY created_at DESC LIMIT 40")
    buttons = [[kit.btn(f"✅ Разбанить {r['first_name'] or r['id']}", f"ex:unban:{r['id']}")] for r in rows]
    buttons.append([kit.btn("⬅️ Назад", "exa:home")])
    text = "⛔️ <b>Заблокированные заказчики</b>" + ("" if rows else "\n\nСписок пуст.")
    try:
        await call.message.edit_text(text, reply_markup=kit.kb(buttons))
    except TelegramBadRequest:
        pass
    await call.answer()


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
    await bot.delete_webhook(drop_pending_updates=False)
    await kit.ready(bot)
    task = asyncio.create_task(cleanup(db))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        task.cancel()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
