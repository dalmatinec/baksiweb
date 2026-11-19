"""Связь: переписка клиента с операторами через бота (для тех, у кого спам-блок).

- Сообщения клиента получают ТОЛЬКО операторы, добавленные владельцем бота в /admin (по ссылке-приглашению).
  Владелец бота сам по себе оператором не является: хочет отвечать - добавляет себя той же ссылкой.
- Операторы видят всё о клиенте: имя, @username, ID, ссылку на профиль. Клиент видит только текст ответа,
  кто ответил - не видит.
- Ответил один оператор - остальные операторы получают его ответ с пометкой «✅ Имя ответил в 15:42».
- Нет операторов - бот напоминает владельцу их добавить. Обращения сохраняются: оператор увидит историю.
- Отделы необязательны: если их два и больше, клиент выбирает отдел, и обращение получают операторы отдела.
- Режим «темы»: бот в группе с темами, на каждого клиента своя тема, отвечает любой участник группы.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message, ReactionTypeEmoji

import baksi_kit as kit
from baksi_db import ModuleDB

log = logging.getLogger("svyaz")

MIGRATIONS = [
    """
    CREATE TABLE departments (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        title     TEXT NOT NULL,
        position  INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE staff (
        user_id   INTEGER NOT NULL,
        dept_id   INTEGER NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
        name      TEXT NOT NULL,
        added_at  INTEGER NOT NULL,
        PRIMARY KEY (user_id, dept_id)
    );
    CREATE TABLE clients (
        id         INTEGER PRIMARY KEY,
        username   TEXT,
        first_name TEXT,
        banned     INTEGER NOT NULL DEFAULT 0,
        current    INTEGER,
        created_at INTEGER NOT NULL
    );
    CREATE TABLE dialogs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id   INTEGER NOT NULL,
        dept_id     INTEGER,
        staff_id    INTEGER,
        status      TEXT NOT NULL DEFAULT 'new',
        topic_id    INTEGER,
        created_at  INTEGER NOT NULL,
        last_at     INTEGER NOT NULL,
        first_reply INTEGER,
        last_staff  INTEGER,
        closed_at   INTEGER
    );
    CREATE INDEX dialogs_client ON dialogs(client_id, status);
    CREATE INDEX dialogs_dept ON dialogs(dept_id, status);
    CREATE TABLE msgmap (
        chat_id    INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        dialog_id  INTEGER NOT NULL,
        created_at INTEGER NOT NULL,
        PRIMARY KEY (chat_id, message_id)
    );
    CREATE TABLE active (
        staff_id  INTEGER PRIMARY KEY,
        dialog_id INTEGER NOT NULL
    );
    CREATE TABLE invites (
        token     TEXT PRIMARY KEY,
        dept_id   INTEGER NOT NULL,
        created_at INTEGER NOT NULL
    );
    """,
    """
    CREATE TABLE operators (
        user_id   INTEGER PRIMARY KEY,
        name      TEXT NOT NULL,
        username  TEXT,
        added_at  INTEGER NOT NULL
    );
    INSERT OR IGNORE INTO operators(user_id, name, added_at)
        SELECT user_id, MIN(name), MIN(added_at) FROM staff GROUP BY user_id;
    CREATE TABLE history (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        dialog_id   INTEGER NOT NULL,
        side        TEXT NOT NULL,
        chat_id     INTEGER NOT NULL,
        message_id  INTEGER NOT NULL,
        operator_id INTEGER,
        created_at  INTEGER NOT NULL
    );
    CREATE INDEX history_dialog ON history(dialog_id, id);
    """,
]

DEFAULTS = {
    "greeting": "👋 Здравствуйте! Здесь можно написать нам, даже если у вашего аккаунта ограничения на сообщения. "
                "Просто напишите, что вас интересует.",
    "after_choose": "Напишите сообщение, мы ответим здесь же.",
    "received": "✅ Сообщение получено, ответим здесь.",
    "closed": "Диалог закрыт. Если появятся вопросы, просто напишите снова.",
    "mode": "personal",      # personal | topics
}
NO_OPERATORS_REMIND = 1800   # напоминать владельцу про операторов не чаще раза в 30 минут
HISTORY_LIMIT = 30


def now() -> int:
    return int(time.time())


def esc(text: Any) -> str:
    return html.escape(str(text or ""))


def hhmm(ts: int | None = None) -> str:
    return time.strftime("%H:%M", time.localtime(ts or now()))


class Svyaz:
    def __init__(self, bot: Bot, db: ModuleDB, username: str) -> None:
        self.bot = bot
        self.db = db
        self.username = username
        self.locks: dict[int, asyncio.Lock] = {}  # сообщения одного клиента по очереди (альбомы приходят пачкой)

    def lock(self, user_id: int) -> asyncio.Lock:
        if len(self.locks) > 5000:
            self.locks = {k: v for k, v in self.locks.items() if v.locked()}
        return self.locks.setdefault(user_id, asyncio.Lock())

    def opt(self, key: str) -> Any:
        """Своя настройка клиента → оформление от владельца платформы → значение по умолчанию."""
        value = self.db.get(key)
        if value is not None:
            return value
        default = DEFAULTS[key]
        return kit.ui_text(key, default) if isinstance(default, str) else default

    def is_owner(self, uid: int) -> bool:
        return uid in kit.owner_ids()

    async def is_operator(self, uid: int) -> bool:
        return bool(await self.db.one("SELECT 1 FROM operators WHERE user_id = ?", uid))

    async def operators(self) -> list[Any]:
        return await self.db.all("SELECT * FROM operators ORDER BY added_at")

    async def departments(self) -> list[Any]:
        return await self.db.all("SELECT * FROM departments ORDER BY position, id")

    async def dept_title(self, dept_id: int | None) -> str:
        if not dept_id:
            return ""
        return await self.db.value("SELECT title FROM departments WHERE id = ?", dept_id, default="")

    async def audience(self, dialog: Any) -> list[int]:
        """Кто получает обращение: операторы отдела, а если отдела нет или в нём никого - все операторы."""
        if dialog["dept_id"]:
            rows = await self.db.all("SELECT s.user_id FROM staff s JOIN operators o ON o.user_id = s.user_id "
                                     "WHERE s.dept_id = ?", dialog["dept_id"])
            if rows:
                return [r["user_id"] for r in rows]
        return [r["user_id"] for r in await self.operators()]

    async def map(self, chat_id: int, message_id: int, dialog_id: int) -> None:
        await self.db.run("INSERT OR REPLACE INTO msgmap(chat_id, message_id, dialog_id, created_at) "
                          "VALUES (?, ?, ?, ?)", chat_id, message_id, dialog_id, now())

    async def dialog_of(self, chat_id: int, message_id: int) -> Any:
        return await self.db.one("SELECT d.* FROM msgmap m JOIN dialogs d ON d.id = m.dialog_id "
                                 "WHERE m.chat_id = ? AND m.message_id = ?", chat_id, message_id)

    async def remember(self, dialog_id: int, side: str, chat_id: int, message_id: int,
                       operator_id: int | None = None) -> None:
        await self.db.run("INSERT INTO history(dialog_id, side, chat_id, message_id, operator_id, created_at) "
                          "VALUES (?, ?, ?, ?, ?, ?)", dialog_id, side, chat_id, message_id, operator_id, now())

    async def client_card(self, client_id: int) -> str:
        c = await self.db.one("SELECT * FROM clients WHERE id = ?", client_id)
        name = esc((c["first_name"] if c else None) or "Без имени")
        parts = [f'👤 <a href="tg://user?id={client_id}">{name}</a>']
        if c and c["username"]:
            parts.append(f"@{esc(c['username'])}")
        parts.append(f"ID <code>{client_id}</code>")
        return " · ".join(parts)

    def reply_kb(self, dialog_id: int):
        return kit.kb([[kit.ui_btn("sv_reply", "↩️ Ответить", f"sv:act:{dialog_id}", style="primary")]])

    async def card_kb(self, dialog_id: int):
        rows = [[kit.ui_btn("sv_reply", "↩️ Ответить", f"sv:act:{dialog_id}", style="primary"),
                 kit.ui_btn("sv_history", "📜 История", f"sv:hist:{dialog_id}")],
                [kit.ui_btn("sv_close", "✅ Закрыть", f"sv:close:{dialog_id}", style="success"),
                 kit.ui_btn("sv_ban", "⛔️ Бан", f"sv:ban:{dialog_id}", style="danger")]]
        if len(await self.departments()) > 1:
            rows.append([kit.ui_btn("sv_transfer", "🔀 Передать", f"sv:tr:{dialog_id}")])
        return kit.kb(rows)

    async def send_safe(self, make):
        """Одна отправка: заблокировавший бота оператор не должен ломать доставку остальным."""
        for _ in range(2):
            try:
                return await make()
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 0.5)
            except (TelegramForbiddenError, TelegramBadRequest) as exc:
                log.info("Не доставлено: %s", exc)
                return None
        return None

    # --- нет операторов -----------------------------------------------------------
    async def remind_no_operators(self) -> None:
        if now() - int(self.db.get("no_ops_reminded", 0) or 0) < NO_OPERATORS_REMIND:
            return
        await self.db.set("no_ops_reminded", now())
        waiting = await self.db.value("SELECT COUNT(*) FROM dialogs WHERE status != 'closed'", default=0)
        text = (f"⚠️ Связь @{self.username}: нет операторов, обращения никто не видит (ждут ответа: {waiting}).\n"
                "Откройте бота → /admin → 👥 Операторы и отправьте ссылку-приглашение тем, кто будет отвечать. "
                "Себя можно добавить той же ссылкой.")
        kit.notify(text)
        for oid in kit.owner_ids():  # и в самом боте, если владелец его уже открывал
            await self.send_safe(lambda oid=oid: self.bot.send_message(oid, text))

    # --- доставка сообщения клиента -----------------------------------------------
    async def deliver_from_client(self, dialog: Any, message: Message, fresh: bool) -> int:
        """Возвращает, скольким операторам доставлено."""
        if self.opt("mode") == "topics" and kit.bound_chat_id():
            return 1 if await self._to_topic(dialog, message) else 0
        targets = await self.audience(dialog)
        if not targets:
            await self.remind_no_operators()
            return 0
        dept = await self.dept_title(dialog["dept_id"])
        card_text = (f"📩 <b>Новое обращение #{dialog['id']}</b>" + (f" · {esc(dept)}" if dept else "")
                     + f"\n{await self.client_card(dialog['client_id'])}")
        card_kb = await self.card_kb(dialog["id"])
        delivered = 0
        for uid in targets:
            if fresh:
                card = await self.send_safe(lambda uid=uid: self.bot.send_message(uid, card_text, reply_markup=card_kb))
                if card:
                    await self.map(uid, card.message_id, dialog["id"])
            copy = await self.send_safe(lambda uid=uid: self.bot.copy_message(
                uid, message.chat.id, message.message_id, reply_markup=self.reply_kb(dialog["id"])))
            if copy:
                await self.map(uid, copy.message_id, dialog["id"])
                delivered += 1
        return delivered

    async def _to_topic(self, dialog: Any, message: Message) -> bool:
        group = kit.bound_chat_id()
        topic = dialog["topic_id"]
        if not topic:
            c = await self.db.one("SELECT * FROM clients WHERE id = ?", dialog["client_id"])
            name = (c["first_name"] if c else None) or str(dialog["client_id"])
            dept = await self.dept_title(dialog["dept_id"])
            try:
                created = await self.bot.create_forum_topic(group, name[:80] + (f" · {dept[:40]}" if dept else ""))
            except TelegramBadRequest as exc:
                kit.notify(f"⚠️ Не получилось создать тему в группе ({exc.message}). Проверьте, что в группе "
                           "включены темы и у бота есть право управлять ими.")
                return False
            topic = created.message_thread_id
            await self.db.run("UPDATE dialogs SET topic_id = ? WHERE id = ?", topic, dialog["id"])
            card = await self.bot.send_message(
                group, f"📩 <b>Обращение #{dialog['id']}</b>\n{await self.client_card(dialog['client_id'])}\n"
                       "Пишите в эту тему: всё уйдёт клиенту.", message_thread_id=topic,
                reply_markup=kit.kb([[kit.ui_btn("sv_close", "✅ Закрыть", f"sv:close:{dialog['id']}", style="success"),
                                      kit.ui_btn("sv_ban", "⛔️ Бан", f"sv:ban:{dialog['id']}", style="danger")]]))
            await self.map(group, card.message_id, dialog["id"])
        try:
            copy = await self.bot.copy_message(group, message.chat.id, message.message_id, message_thread_id=topic)
        except TelegramBadRequest:
            await self.db.run("UPDATE dialogs SET topic_id = NULL WHERE id = ?", dialog["id"])
            return False
        await self.map(group, copy.message_id, dialog["id"])
        return True

    # --- ответ оператора ------------------------------------------------------------
    async def deliver_to_client(self, dialog: Any, message: Message, operator_id: int, name: str,
                                mirror: bool = True) -> str | None:
        if dialog["status"] == "closed":
            return "Обращение закрыто. Клиент может написать снова, тогда откроется новое."
        if await self.db.value("SELECT banned FROM clients WHERE id = ?", dialog["client_id"], default=0):
            return "Клиент заблокирован."
        try:  # клиент видит только сам ответ, без имени оператора
            sent = await self.bot.copy_message(dialog["client_id"], message.chat.id, message.message_id)
        except TelegramForbiddenError:
            return "Клиент заблокировал бота, сообщение не доставлено."
        except TelegramBadRequest as exc:
            return f"Не доставлено: {exc.message}"
        await self.map(dialog["client_id"], sent.message_id, dialog["id"])
        await self.remember(dialog["id"], "operator", message.chat.id, message.message_id, operator_id)
        await self.db.run("UPDATE dialogs SET staff_id = ?, status = 'active', last_at = ?, "
                          "first_reply = COALESCE(first_reply, ?), last_staff = ? WHERE id = ?",
                          operator_id, now(), now(), operator_id, dialog["id"])
        if mirror:
            await self.mirror(dialog, message, operator_id, name)
        return None

    async def mirror(self, dialog: Any, message: Message, operator_id: int, name: str) -> None:
        """Остальные операторы видят, что и когда ответил коллега, - чтобы не отвечать повторно."""
        for uid in await self.audience(dialog):
            if uid == operator_id:
                continue
            head = await self.send_safe(lambda uid=uid: self.bot.send_message(
                uid, f"✅ <b>{esc(name)}</b> ответил в {hhmm()} · обращение #{dialog['id']}",
                reply_markup=self.reply_kb(dialog["id"]), disable_notification=True))
            if head:
                await self.map(uid, head.message_id, dialog["id"])
            copy = await self.send_safe(lambda uid=uid: self.bot.copy_message(
                uid, message.chat.id, message.message_id, disable_notification=True))
            if copy:
                await self.map(uid, copy.message_id, dialog["id"])

    async def close(self, dialog_id: int, by: int | None, by_name: str = "") -> None:
        d = await self.db.one("SELECT * FROM dialogs WHERE id = ?", dialog_id)
        if not d or d["status"] == "closed":
            return
        await self.db.run("UPDATE dialogs SET status = 'closed', closed_at = ? WHERE id = ?", now(), dialog_id)
        await self.db.run("DELETE FROM active WHERE dialog_id = ?", dialog_id)
        await self.db.run("UPDATE clients SET current = NULL WHERE id = ? AND current = ?", d["client_id"], dialog_id)
        await self.send_safe(lambda: self.bot.send_message(d["client_id"], self.opt("closed")))
        for uid in await self.audience(d):
            if uid != by:
                await self.send_safe(lambda uid=uid: self.bot.send_message(
                    uid, f"✅ Обращение #{dialog_id} закрыто" + (f": {esc(by_name)}" if by_name else ""),
                    disable_notification=True))
        if d["topic_id"] and kit.bound_chat_id():
            try:
                await self.bot.close_forum_topic(kit.bound_chat_id(), d["topic_id"])
            except TelegramBadRequest:
                pass


router = Router(name="svyaz")


class Form(StatesGroup):
    dept_title = State()
    text = State()


def not_command(message: Message) -> bool:
    return not (message.text or "").startswith("/")


async def dept_keyboard(sv: Svyaz):
    depts = await sv.departments()
    return kit.kb(kit.chunk([kit.btn(d["title"], f"sv:dept:{d['id']}") for d in depts], kit.ui_cols("dept_cols", 1)))


async def new_invite(sv: Svyaz, dept_id: int = 0) -> str:
    token = secrets.token_urlsafe(9)
    await sv.db.run("INSERT INTO invites(token, dept_id, created_at) VALUES (?, ?, ?)", token, dept_id, now())
    return f"https://t.me/{sv.username}?start=op_{token}"


# --- клиент и приглашения -----------------------------------------------------------
@router.message(CommandStart(), F.chat.type == "private")
async def start(message: Message, command: CommandObject, sv: Svyaz, state: FSMContext) -> None:
    await state.clear()
    u = message.from_user
    arg = (command.args or "").strip()
    token = arg[3:] if arg.startswith("op_") else arg[6:] if arg.startswith("staff_") else ""
    if token:
        inv = await sv.db.one("SELECT * FROM invites WHERE token = ?", token)
        if not inv:
            await message.answer("Приглашение недействительно. Попросите новое у владельца бота.")
            return
        await sv.db.run("INSERT INTO operators(user_id, name, username, added_at) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(user_id) DO UPDATE SET name = excluded.name, username = excluded.username",
                        u.id, u.full_name, u.username, now())
        dept = ""
        if inv["dept_id"]:
            await sv.db.run("INSERT OR REPLACE INTO staff(user_id, dept_id, name, added_at) VALUES (?, ?, ?, ?)",
                            u.id, inv["dept_id"], u.full_name, now())
            dept = await sv.dept_title(inv["dept_id"])
        waiting = await sv.db.value("SELECT COUNT(*) FROM dialogs WHERE status != 'closed'", default=0)
        await message.answer(
            "✅ Вы оператор" + (f" в отделе «{esc(dept)}»" if dept else "") + ". Обращения клиентов будут приходить сюда.\n\n"
            "Как отвечать: нажмите «↩️ Ответить» под сообщением клиента и просто пишите, или ответьте на него реплаем. "
            "Клиент видит только текст ответа, ваше имя ему не показывается. Другие операторы видят, что вы ответили.\n"
            "/dialogs - открытые обращения, /close - закрыть текущее."
            + (f"\n\nСейчас ждут ответа: {waiting}. Откройте /dialogs." if waiting else ""))
        for oid in kit.owner_ids():
            if oid != u.id:
                await sv.send_safe(lambda oid=oid: sv.bot.send_message(
                    oid, f"👤 {esc(u.full_name)} теперь оператор" + (f" отдела «{esc(dept)}»" if dept else "")))
        return
    await sv.db.run("INSERT INTO clients(id, username, first_name, created_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET username = excluded.username, first_name = excluded.first_name",
                    u.id, u.username, u.first_name, now())
    text = sv.opt("greeting")
    if sv.is_owner(u.id):
        ops = len(await sv.operators())
        text += "\n\n⚙️ Вы владелец: /admin - операторы, отделы, тексты." + (
            "" if ops else "\n⚠️ Операторов пока нет: обращения никто не видит. /admin → 👥 Операторы.")
    depts = await sv.departments()
    if len(depts) > 1:
        await message.answer(text, reply_markup=await dept_keyboard(sv))
        return
    await message.answer(text)
    if len(depts) == 1:
        await choose(message, sv, depts[0]["id"], u.id, silent=True)


async def choose(target: Message, sv: Svyaz, dept_id: int, user_id: int, silent: bool = False) -> None:
    d = await sv.db.one("SELECT * FROM dialogs WHERE client_id = ? AND dept_id = ? AND status != 'closed' "
                        "ORDER BY id DESC LIMIT 1", user_id, dept_id)
    dialog_id = d["id"] if d else await sv.db.run(
        "INSERT INTO dialogs(client_id, dept_id, created_at, last_at) VALUES (?, ?, ?, ?)", user_id, dept_id, now(), now())
    await sv.db.run("UPDATE clients SET current = ? WHERE id = ?", dialog_id, user_id)
    if not silent:
        await target.answer(f"<b>{esc(await sv.dept_title(dept_id))}</b>\n{sv.opt('after_choose')}")


@router.callback_query(F.data.startswith("sv:dept:"))
async def pick_dept(call: CallbackQuery, sv: Svyaz) -> None:
    dept_id = int(call.data.split(":")[2])
    if not await sv.db.one("SELECT 1 FROM departments WHERE id = ?", dept_id):
        await call.answer("Отдел больше не доступен", show_alert=True)
        return
    await choose(call.message, sv, dept_id, call.from_user.id)
    await call.answer()


@router.message(StateFilter(None), F.chat.type == "private", not_command)
async def private_message(message: Message, sv: Svyaz) -> None:
    async with sv.lock(message.from_user.id):
        await _private_message(message, sv)


async def _private_message(message: Message, sv: Svyaz) -> None:
    uid = message.from_user.id
    if await sv.is_operator(uid):
        await operator_message(message, sv)
        return
    client = await sv.db.one("SELECT * FROM clients WHERE id = ?", uid)
    if client is None:
        await sv.db.run("INSERT OR IGNORE INTO clients(id, username, first_name, created_at) VALUES (?, ?, ?, ?)",
                        uid, message.from_user.username, message.from_user.first_name, now())
        client = await sv.db.one("SELECT * FROM clients WHERE id = ?", uid)
    if client["banned"]:
        return
    dialog = None
    if message.reply_to_message:
        dialog = await sv.dialog_of(uid, message.reply_to_message.message_id)
        if dialog and dialog["status"] == "closed":
            dialog = None
    if not dialog and client["current"]:
        dialog = await sv.db.one("SELECT * FROM dialogs WHERE id = ? AND status != 'closed'", client["current"])
    if not dialog:
        depts = await sv.departments()
        if len(depts) > 1:
            await message.answer("С кем хотите связаться?", reply_markup=await dept_keyboard(sv))
            return
        dept_id = depts[0]["id"] if depts else None
        did = await sv.db.run("INSERT INTO dialogs(client_id, dept_id, created_at, last_at) VALUES (?, ?, ?, ?)",
                              uid, dept_id, now(), now())
        await sv.db.run("UPDATE clients SET current = ? WHERE id = ?", did, uid)
        dialog = await sv.db.one("SELECT * FROM dialogs WHERE id = ?", did)
    fresh = not await sv.db.one("SELECT 1 FROM history WHERE dialog_id = ? LIMIT 1", dialog["id"])
    await sv.remember(dialog["id"], "client", uid, message.message_id)
    await sv.db.run("UPDATE dialogs SET last_at = ? WHERE id = ?", now(), dialog["id"])
    await sv.deliver_from_client(dialog, message, fresh)
    if fresh:  # клиенту одно короткое подтверждение на обращение, без подробностей о том, кто его увидит
        await message.answer(sv.opt("received"))


# --- операторы ---------------------------------------------------------------------
async def operator_message(message: Message, sv: Svyaz) -> None:
    uid = message.from_user.id
    dialog = None
    if message.reply_to_message:
        dialog = await sv.dialog_of(uid, message.reply_to_message.message_id)
    if not dialog:
        active = await sv.db.one("SELECT dialog_id FROM active WHERE staff_id = ?", uid)
        if active:
            dialog = await sv.db.one("SELECT * FROM dialogs WHERE id = ?", active["dialog_id"])
    if not dialog:
        await message.answer("Кому ответить? Нажмите «↩️ Ответить» под сообщением клиента, ответьте на него реплаем "
                             "или откройте /dialogs.")
        return
    err = await sv.deliver_to_client(dialog, message, uid, message.from_user.full_name)
    if err:
        await message.answer(f"⚠️ {err}")
        return
    try:  # отметка «доставлено» без лишних сообщений
        await sv.bot.set_message_reaction(message.chat.id, message.message_id, [ReactionTypeEmoji(emoji="👌")])
    except Exception:
        pass


@router.message(F.chat.type.in_({"group", "supergroup"}), F.message_thread_id)
async def topic_message(message: Message, sv: Svyaz) -> None:
    if message.chat.id != kit.bound_chat_id() or not message.from_user or message.from_user.is_bot:
        return
    if (message.text or "").startswith("/"):
        return
    dialog = await sv.db.one("SELECT * FROM dialogs WHERE topic_id = ? ORDER BY id DESC LIMIT 1",
                             message.message_thread_id)
    if not dialog:
        return
    err = await sv.deliver_to_client(dialog, message, message.from_user.id, message.from_user.full_name, mirror=False)
    if err:
        await message.reply(f"⚠️ {err}")


async def can_handle(sv: Svyaz, call: CallbackQuery) -> bool:
    if call.message and call.message.chat.type != "private" and call.message.chat.id == kit.bound_chat_id():
        return True
    return await sv.is_operator(call.from_user.id)


@router.callback_query(F.data.startswith("sv:act:"))
async def activate(call: CallbackQuery, sv: Svyaz) -> None:
    if not await sv.is_operator(call.from_user.id):
        await call.answer("Отвечать могут только операторы", show_alert=True)
        return
    dialog_id = int(call.data.split(":")[2])
    d = await sv.db.one("SELECT * FROM dialogs WHERE id = ?", dialog_id)
    if not d or d["status"] == "closed":
        await call.answer("Обращение уже закрыто", show_alert=True)
        return
    await sv.db.run("INSERT OR REPLACE INTO active(staff_id, dialog_id) VALUES (?, ?)", call.from_user.id, dialog_id)
    await call.answer()
    extra = ""
    if d["last_staff"] and d["last_staff"] != call.from_user.id:
        who = await sv.db.value("SELECT name FROM operators WHERE user_id = ?", d["last_staff"], default="коллега")
        extra = f"\nПоследним отвечал(а): {esc(who)}."
    await call.message.answer(f"✍️ Вы отвечаете в обращение #{dialog_id}\n{await sv.client_card(d['client_id'])}{extra}\n"
                              "Просто отправляйте сообщения: клиент увидит только текст. Закончить: /close")


@router.callback_query(F.data.startswith("sv:hist:"))
async def history(call: CallbackQuery, sv: Svyaz) -> None:
    if not await can_handle(sv, call):
        await call.answer()
        return
    dialog_id = int(call.data.split(":")[2])
    rows = await sv.db.all("SELECT * FROM (SELECT * FROM history WHERE dialog_id = ? ORDER BY id DESC LIMIT ?) "
                           "ORDER BY id", dialog_id, HISTORY_LIMIT)
    await call.answer()
    chat = call.message.chat.id
    if not rows:
        await call.message.answer("История пуста.")
        return
    await call.message.answer(f"📜 История обращения #{dialog_id} (последние {len(rows)}):", disable_notification=True)
    last = None
    for r in rows:
        who = (r["side"], r["operator_id"])
        if who != last:
            if r["side"] == "client":
                label = "👤 Клиент"
            else:
                label = "🎧 " + esc(await sv.db.value("SELECT name FROM operators WHERE user_id = ?", r["operator_id"],
                                                     default="Оператор"))
            await sv.send_safe(lambda label=label, r=r: sv.bot.send_message(
                chat, f"<b>{label}</b> · {hhmm(r['created_at'])}", disable_notification=True))
            last = who
        await sv.send_safe(lambda r=r: sv.bot.copy_message(chat, r["chat_id"], r["message_id"], disable_notification=True))
        await asyncio.sleep(0.05)


@router.message(Command("dialogs"), F.chat.type == "private")
async def dialogs(message: Message, sv: Svyaz) -> None:
    uid = message.from_user.id
    if not await sv.is_operator(uid):
        if sv.is_owner(uid):
            await message.answer("Вы не оператор. Добавить себя: /admin → 👥 Операторы → ссылка-приглашение.")
        return
    rows = [d for d in await sv.db.all("SELECT * FROM dialogs WHERE status != 'closed' ORDER BY last_at DESC LIMIT 40")
            if uid in await sv.audience(d)]
    if not rows:
        await message.answer("Открытых обращений нет 🎉")
        return
    buttons = []
    for d in rows:
        c = await sv.db.one("SELECT first_name, username FROM clients WHERE id = ?", d["client_id"])
        mark = "🆕" if d["status"] == "new" else "💬"
        name = (c["first_name"] if c else None) or str(d["client_id"])
        buttons.append([kit.btn(f"{mark} #{d['id']} {name}" + (f" @{c['username']}" if c and c["username"] else ""),
                                f"sv:act:{d['id']}")])
    await message.answer("<b>Открытые обращения</b>\n🆕 без ответа · 💬 уже отвечали", reply_markup=kit.kb(buttons))


@router.message(Command("close"), F.chat.type == "private")
async def close_cmd(message: Message, sv: Svyaz) -> None:
    uid = message.from_user.id
    active = await sv.db.one("SELECT dialog_id FROM active WHERE staff_id = ?", uid)
    if not active:
        await message.answer("Нет активного обращения.")
        return
    await sv.close(active["dialog_id"], uid, message.from_user.full_name)
    await message.answer(f"✅ Обращение #{active['dialog_id']} закрыто.")


@router.callback_query(F.data.startswith("sv:close:"))
async def close_btn(call: CallbackQuery, sv: Svyaz) -> None:
    if not await can_handle(sv, call):
        await call.answer()
        return
    dialog_id = int(call.data.split(":")[2])
    await sv.close(dialog_id, call.from_user.id, call.from_user.full_name)
    await call.answer(f"Обращение #{dialog_id} закрыто")
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("sv:ban:"))
async def ban_btn(call: CallbackQuery, sv: Svyaz) -> None:
    if not await can_handle(sv, call):
        await call.answer()
        return
    dialog_id = int(call.data.split(":")[2])
    d = await sv.db.one("SELECT * FROM dialogs WHERE id = ?", dialog_id)
    if not d:
        await call.answer()
        return
    await sv.db.run("UPDATE clients SET banned = 1 WHERE id = ?", d["client_id"])
    await sv.db.run("UPDATE dialogs SET status = 'closed', closed_at = ? WHERE client_id = ? AND status != 'closed'",
                    now(), d["client_id"])
    await sv.db.run("DELETE FROM active WHERE dialog_id IN (SELECT id FROM dialogs WHERE client_id = ?)", d["client_id"])
    await call.answer("Клиент заблокирован. Разблокировать: /admin → Заблокированные", show_alert=True)


@router.callback_query(F.data.startswith("sv:tr:"))
async def transfer_menu(call: CallbackQuery, sv: Svyaz) -> None:
    if not await sv.is_operator(call.from_user.id):
        await call.answer()
        return
    dialog_id = int(call.data.split(":")[2])
    rows = [[kit.btn(d["title"], f"sv:trd:{dialog_id}:{d['id']}")] for d in await sv.departments()]
    await call.message.answer(f"Куда передать обращение #{dialog_id}?", reply_markup=kit.kb(rows))
    await call.answer()


@router.callback_query(F.data.startswith("sv:trd:"))
async def transfer(call: CallbackQuery, sv: Svyaz) -> None:
    if not await sv.is_operator(call.from_user.id):
        await call.answer()
        return
    _, _, did, dept = call.data.split(":")
    dialog_id, dept_id = int(did), int(dept)
    await sv.db.run("UPDATE dialogs SET dept_id = ?, status = 'new' WHERE id = ?", dept_id, dialog_id)
    await sv.db.run("DELETE FROM active WHERE dialog_id = ?", dialog_id)
    d = await sv.db.one("SELECT * FROM dialogs WHERE id = ?", dialog_id)
    title = await sv.dept_title(dept_id)
    text = f"🔀 Вам передано обращение #{dialog_id} · {esc(title)}\n{await sv.client_card(d['client_id'])}"
    markup = await sv.card_kb(dialog_id)
    for uid in await sv.audience(d):
        card = await sv.send_safe(lambda uid=uid: sv.bot.send_message(uid, text, reply_markup=markup))
        if card:
            await sv.map(uid, card.message_id, dialog_id)
    await call.message.edit_text(f"✅ Обращение #{dialog_id} передано в «{esc(title)}»")
    await call.answer()


# --- админка владельца ----------------------------------------------------------------
async def admin_view(sv: Svyaz) -> tuple[str, Any]:
    ops = await sv.operators()
    depts = await sv.departments()
    day = now() - 86400
    today = await sv.db.value("SELECT COUNT(*) FROM dialogs WHERE created_at > ?", day, default=0)
    open_ = await sv.db.value("SELECT COUNT(*) FROM dialogs WHERE status != 'closed'", default=0)
    unanswered = await sv.db.value("SELECT COUNT(*) FROM dialogs WHERE status = 'new'", default=0)
    avg = await sv.db.value("SELECT AVG(first_reply - created_at) FROM dialogs WHERE first_reply IS NOT NULL AND "
                            "created_at > ?", now() - 7 * 86400)
    lines = ["⚙️ <b>Связь: админка</b>\n"]
    if not ops:
        lines.append("⚠️ <b>Операторов нет: обращения никто не видит.</b> Добавьте их в разделе 👥 Операторы.\n")
    names = ", ".join(esc(o["name"]) for o in ops[:5]) + ("…" if len(ops) > 5 else "")
    lines += [f"👥 Операторов: <b>{len(ops)}</b>" + (f" ({names})" if ops else ""),
              f"📨 Обращений сегодня: <b>{today}</b> · открыто: <b>{open_}</b> · без ответа: <b>{unanswered}</b>",
              f"⏱ Средний первый ответ (7 дней): <b>{int(avg // 60)} мин</b>" if avg else "⏱ Ответов пока нет",
              f"📁 Отделов: <b>{len(depts)}</b>" + (" (клиент выбирает, кому писать)" if len(depts) > 1 else ""),
              f"🧭 Режим: <b>{'темы в группе' if sv.opt('mode') == 'topics' else 'личка операторов'}</b>"]
    if sv.opt("mode") == "topics" and not kit.bound_chat_id():
        lines.append(f"\n⚠️ Для режима тем привяжите группу с темами: <code>/bind {kit.platform().get('bind_code', '')}</code>")
    rows = [[kit.btn("👥 Операторы", "sva:ops", style=None if ops else "primary"), kit.btn("📁 Отделы", "sva:depts")],
            [kit.btn("✏️ Приветствие", "sva:txt:greeting"), kit.btn("✏️ «Сообщение получено»", "sva:txt:received")],
            [kit.btn("✏️ Текст после выбора отдела", "sva:txt:after_choose")],
            [kit.btn(f"🧭 Режим: {'темы' if sv.opt('mode') == 'topics' else 'личка'}", "sva:mode"),
             kit.btn("⛔️ Заблокированные", "sva:bans")],
            [kit.btn("🔄 Обновить", "sva:home")]]
    return "\n".join(lines), kit.kb(rows)


async def show(target: Message | CallbackQuery, text: str, markup: Any) -> None:
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            pass
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup)


async def show_admin(target: Message | CallbackQuery, sv: Svyaz) -> None:
    text, markup = await admin_view(sv)
    await show(target, text, markup)


@router.message(Command("admin"), F.chat.type == "private")
async def admin(message: Message, sv: Svyaz, state: FSMContext) -> None:
    if sv.is_owner(message.from_user.id):
        await state.clear()
        await show_admin(message, sv)


@router.callback_query(F.data == "sva:home")
async def admin_home(call: CallbackQuery, sv: Svyaz, state: FSMContext) -> None:
    if sv.is_owner(call.from_user.id):
        await state.clear()
        await show_admin(call, sv)


@router.callback_query(F.data == "sva:ops")
async def admin_operators(call: CallbackQuery, sv: Svyaz) -> None:
    if not sv.is_owner(call.from_user.id):
        await call.answer()
        return
    ops = await sv.operators()
    link = await new_invite(sv, 0)
    listing = "\n".join(f"• {esc(o['name'])}" + (f" @{esc(o['username'])}" if o["username"] else "")
                        + f" · <code>{o['user_id']}</code>" for o in ops) or "Пока никого."
    text = ("👥 <b>Операторы</b>\nТолько они получают обращения и могут отвечать. Владелец бота - не оператор, "
            "пока сам не откроет ссылку-приглашение.\n\n" + listing +
            f"\n\nСсылка-приглашение (отправьте оператору, действует 7 дней):\n<code>{link}</code>")
    rows = [[kit.btn("📋 Скопировать приглашение", copy=link)]]
    rows += [[kit.btn(f"✖️ Убрать {o['name']}", f"sva:oprm:{o['user_id']}")] for o in ops]
    rows.append([kit.btn("⬅️ Назад", "sva:home")])
    await show(call, text, kit.kb(rows))


@router.callback_query(F.data.startswith("sva:oprm:"))
async def admin_operator_remove(call: CallbackQuery, sv: Svyaz) -> None:
    if not sv.is_owner(call.from_user.id):
        await call.answer()
        return
    uid = int(call.data.split(":")[2])
    await sv.db.run("DELETE FROM operators WHERE user_id = ?", uid)
    await sv.db.run("DELETE FROM staff WHERE user_id = ?", uid)
    await sv.db.run("DELETE FROM active WHERE staff_id = ?", uid)
    await sv.send_safe(lambda: sv.bot.send_message(uid, "Вы больше не оператор этого бота."))
    await admin_operators(call, sv)


@router.callback_query(F.data == "sva:depts")
async def admin_depts(call: CallbackQuery, sv: Svyaz) -> None:
    if not sv.is_owner(call.from_user.id):
        await call.answer()
        return
    depts = await sv.departments()
    text = ("📁 <b>Отделы</b> (необязательно)\nЕсли отделов два и больше, клиент выбирает, кому написать, и обращение "
            "получают операторы этого отдела. В отделе без своих операторов обращения получают все операторы.\n\n")
    for d in depts:
        names = await sv.db.all("SELECT name FROM staff WHERE dept_id = ?", d["id"])
        text += f"• {esc(d['title'])}: " + (", ".join(esc(n["name"]) for n in names) or "все операторы") + "\n"
    rows = [[kit.btn(f"📁 {d['title']}", f"sva:dept:{d['id']}")] for d in depts]
    rows.append([kit.btn("➕ Новый отдел", "sva:newdept", style="success")])
    rows.append([kit.btn("⬅️ Назад", "sva:home")])
    await show(call, text, kit.kb(rows))


@router.callback_query(F.data == "sva:newdept")
async def new_dept(call: CallbackQuery, sv: Svyaz, state: FSMContext) -> None:
    if not sv.is_owner(call.from_user.id):
        return
    await state.set_state(Form.dept_title)
    await call.message.answer("Название отдела (так его увидят клиенты), например: «Продажи» или «Заказы»",
                              reply_markup=kit.kb([[kit.btn("Отмена", "sva:depts")]]))
    await call.answer()


@router.message(Form.dept_title, F.chat.type == "private", F.text)
async def new_dept_title(message: Message, sv: Svyaz, state: FSMContext) -> None:
    if not sv.is_owner(message.from_user.id):
        return
    title = message.text.strip()[:60]
    data = await state.get_data()
    if data.get("rename"):
        await sv.db.run("UPDATE departments SET title = ? WHERE id = ?", title, data["rename"])
    else:
        pos = await sv.db.value("SELECT MAX(position) FROM departments", default=0)
        await sv.db.run("INSERT INTO departments(title, position) VALUES (?, ?)", title, pos + 1)
    await state.clear()
    await message.answer("✅ Сохранено")
    await show_admin(message, sv)


@router.callback_query(F.data.startswith("sva:dept:"))
async def dept_card(call: CallbackQuery, sv: Svyaz) -> None:
    if not sv.is_owner(call.from_user.id):
        return
    dept_id = int(call.data.split(":")[2])
    title = await sv.dept_title(dept_id)
    staff = await sv.db.all("SELECT user_id, name FROM staff WHERE dept_id = ?", dept_id)
    link = await new_invite(sv, dept_id)
    text = (f"📁 <b>{esc(title)}</b>\n\nОператоры отдела: " +
            (", ".join(esc(s["name"]) for s in staff) or "нет своих (обращения получают все операторы)") +
            f"\n\nСсылка-приглашение в отдел (оператор попадёт сразу в этот отдел):\n<code>{link}</code>")
    rows = [[kit.btn("📋 Скопировать приглашение", copy=link)]]
    rows += [[kit.btn(f"✖️ Убрать из отдела {s['name']}", f"sva:rm:{dept_id}:{s['user_id']}")] for s in staff]
    rows.append([kit.btn("✏️ Переименовать", f"sva:ren:{dept_id}"), kit.btn("⬆️ Выше", f"sva:up:{dept_id}")])
    rows.append([kit.btn("🗑 Удалить отдел", f"sva:del:{dept_id}", style="danger"), kit.btn("⬅️ Назад", "sva:depts")])
    await show(call, text, kit.kb(rows))


@router.callback_query(F.data.startswith("sva:rm:"))
async def remove_staff(call: CallbackQuery, sv: Svyaz) -> None:
    if not sv.is_owner(call.from_user.id):
        return
    _, _, dept, uid = call.data.split(":")
    await sv.db.run("DELETE FROM staff WHERE dept_id = ? AND user_id = ?", int(dept), int(uid))
    call.data = f"sva:dept:{dept}"
    await dept_card(call, sv)


@router.callback_query(F.data.startswith("sva:ren:"))
async def rename_dept(call: CallbackQuery, sv: Svyaz, state: FSMContext) -> None:
    if not sv.is_owner(call.from_user.id):
        return
    await state.set_state(Form.dept_title)
    await state.update_data(rename=int(call.data.split(":")[2]))
    await call.message.answer("Новое название отдела:")
    await call.answer()


@router.callback_query(F.data.startswith("sva:up:"))
async def dept_up(call: CallbackQuery, sv: Svyaz) -> None:
    if not sv.is_owner(call.from_user.id):
        return
    dept_id = int(call.data.split(":")[2])
    ids = [d["id"] for d in await sv.departments()]
    i = ids.index(dept_id) if dept_id in ids else 0
    if i > 0:
        ids[i - 1], ids[i] = ids[i], ids[i - 1]
    for pos, did in enumerate(ids):
        await sv.db.run("UPDATE departments SET position = ? WHERE id = ?", pos, did)
    await admin_depts(call, sv)


@router.callback_query(F.data.startswith("sva:del:"))
async def delete_dept(call: CallbackQuery, sv: Svyaz) -> None:
    if not sv.is_owner(call.from_user.id):
        return
    dept_id = int(call.data.split(":")[2])
    await sv.db.run("DELETE FROM staff WHERE dept_id = ?", dept_id)
    await sv.db.run("DELETE FROM departments WHERE id = ?", dept_id)
    await sv.db.run("UPDATE dialogs SET dept_id = NULL WHERE dept_id = ?", dept_id)
    await admin_depts(call, sv)


@router.callback_query(F.data == "sva:mode")
async def toggle_mode(call: CallbackQuery, sv: Svyaz) -> None:
    if not sv.is_owner(call.from_user.id):
        return
    await sv.db.set("mode", "personal" if sv.opt("mode") == "topics" else "topics")
    await show_admin(call, sv)


@router.callback_query(F.data.startswith("sva:txt:"))
async def edit_text(call: CallbackQuery, sv: Svyaz, state: FSMContext) -> None:
    if not sv.is_owner(call.from_user.id):
        return
    key = call.data.split(":")[2]
    if key not in DEFAULTS:
        return
    await state.set_state(Form.text)
    await state.update_data(key=key)
    await call.message.answer(f"Сейчас:\n<blockquote>{esc(sv.opt(key))}</blockquote>\nОтправьте новый текст. "
                              "Форматирование и премиум-эмодзи сохранятся.",
                              reply_markup=kit.kb([[kit.btn("Отмена", "sva:home")]]))
    await call.answer()


@router.message(Form.text, F.chat.type == "private", F.text)
async def edit_text_input(message: Message, sv: Svyaz, state: FSMContext) -> None:
    if not sv.is_owner(message.from_user.id):
        return
    key = (await state.get_data()).get("key")
    await sv.db.set(key, message.html_text[:3500])
    await state.clear()
    await message.answer("✅ Сохранено")
    await show_admin(message, sv)


@router.callback_query(F.data == "sva:bans")
async def bans(call: CallbackQuery, sv: Svyaz) -> None:
    if not sv.is_owner(call.from_user.id):
        return
    rows = await sv.db.all("SELECT * FROM clients WHERE banned = 1 ORDER BY created_at DESC LIMIT 40")
    buttons = [[kit.btn(f"✅ Разбанить {r['first_name'] or r['id']}", f"sva:unban:{r['id']}")] for r in rows]
    buttons.append([kit.btn("⬅️ Назад", "sva:home")])
    await show(call, "⛔️ <b>Заблокированные</b>" + ("" if rows else "\n\nСписок пуст."), kit.kb(buttons))


@router.callback_query(F.data.startswith("sva:unban:"))
async def unban(call: CallbackQuery, sv: Svyaz) -> None:
    if not sv.is_owner(call.from_user.id):
        return
    await sv.db.run("UPDATE clients SET banned = 0 WHERE id = ?", int(call.data.split(":")[2]))
    await bans(call, sv)


async def cleanup(db: ModuleDB) -> None:
    while True:
        await asyncio.sleep(3600)
        await db.run("DELETE FROM msgmap WHERE created_at < ?", now() - 90 * 86400)
        await db.run("DELETE FROM history WHERE created_at < ?", now() - 90 * 86400)
        await db.run("DELETE FROM invites WHERE created_at < ?", now() - 7 * 86400)


async def main() -> None:
    kit.setup_logging()
    token = os.environ.get("BOT_TOKEN", "")
    if not token:
        raise SystemExit("BOT_TOKEN не задан")
    db = ModuleDB(Path(kit.data_dir()) / "svyaz.db", MIGRATIONS)
    await db.connect()
    bot = Bot(token, session=kit.session(), default=DefaultBotProperties(parse_mode="HTML"))
    me = await bot.get_me()
    sv = Svyaz(bot, db, me.username or "")
    dp = Dispatcher(storage=MemoryStorage(), sv=sv)
    dp.include_router(kit.bind_router())
    dp.include_router(router)
    await kit.ready(bot)
    if not await sv.operators():
        await sv.remind_no_operators()
    task = asyncio.create_task(cleanup(db))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        task.cancel()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
