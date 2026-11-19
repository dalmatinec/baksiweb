"""Связь: бот-посредник для людей со спам-блоком.

Человек пишет боту → выбирает отдел (операторы, разработчики, дизайнеры…) → переписывается с сотрудником
прямо через бота. Два режима:
- «личка»: сообщения приходят сотрудникам отдела в личку от бота, сотрудник жмёт «Ответить» и просто пишет;
- «темы»: бот в группе с темами, на каждого клиента своя тема, отвечает любой сотрудник в теме.
Сотрудники добавляются по ссылке-приглашению в отдел (без поиска ID).
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
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message

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
        current    INTEGER,                 -- текущий диалог
        created_at INTEGER NOT NULL
    );
    CREATE TABLE dialogs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id   INTEGER NOT NULL,
        dept_id     INTEGER,
        staff_id    INTEGER,
        status      TEXT NOT NULL DEFAULT 'new',   -- new|active|closed
        topic_id    INTEGER,
        created_at  INTEGER NOT NULL,
        last_at     INTEGER NOT NULL,
        first_reply INTEGER,
        last_staff  INTEGER,                       -- кто писал клиенту последним (для подписи)
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
]

DEFAULTS = {
    "greeting": "👋 Здравствуйте! Здесь можно написать нам, даже если у вашего аккаунта ограничения на сообщения. "
                "Выберите, с кем хотите связаться:",
    "after_choose": "Напишите сообщение, мы ответим здесь же.",
    "closed": "Диалог закрыт. Если появятся вопросы, просто напишите снова.",
    "show_names": True,      # показывать клиенту имя сотрудника
    "mode": "personal",      # personal | topics
}


def now() -> int:
    return int(time.time())


def esc(text: Any) -> str:
    return html.escape(str(text or ""))


class Svyaz:
    def __init__(self, bot: Bot, db: ModuleDB, username: str) -> None:
        self.bot = bot
        self.db = db
        self.username = username

    def opt(self, key: str) -> Any:
        return self.db.get(key, DEFAULTS[key])

    def is_owner(self, uid: int) -> bool:
        return uid in kit.owner_ids()

    async def is_staff(self, uid: int) -> bool:
        return bool(await self.db.one("SELECT 1 FROM staff WHERE user_id = ?", uid))

    async def departments(self) -> list[Any]:
        return await self.db.all("SELECT * FROM departments ORDER BY position, id")

    async def dept_staff(self, dept_id: int | None) -> list[Any]:
        if dept_id is None:
            return await self.db.all("SELECT user_id, MIN(name) AS name FROM staff GROUP BY user_id")
        return await self.db.all("SELECT user_id, name FROM staff WHERE dept_id = ?", dept_id)

    async def dept_title(self, dept_id: int | None) -> str:
        if dept_id is None:
            return "Поддержка"
        return await self.db.value("SELECT title FROM departments WHERE id = ?", dept_id, default="Отдел")

    async def map(self, chat_id: int, message_id: int, dialog_id: int) -> None:
        await self.db.run("INSERT OR REPLACE INTO msgmap(chat_id, message_id, dialog_id, created_at) "
                          "VALUES (?, ?, ?, ?)", chat_id, message_id, dialog_id, now())

    async def dialog_of(self, chat_id: int, message_id: int) -> Any:
        return await self.db.one("SELECT d.* FROM msgmap m JOIN dialogs d ON d.id = m.dialog_id "
                                 "WHERE m.chat_id = ? AND m.message_id = ?", chat_id, message_id)

    def card_kb(self, dialog_id: int, assigned: bool):
        return kit.kb([
            [kit.btn("↩️ Ответить" if assigned else "🙋 Взять и ответить", f"sv:act:{dialog_id}", style="primary"),
             kit.btn("🔀 Передать", f"sv:tr:{dialog_id}")],
            [kit.btn("✅ Закрыть", f"sv:close:{dialog_id}", style="success"),
             kit.btn("⛔️ Бан", f"sv:ban:{dialog_id}", style="danger")],
        ])

    async def client_line(self, client_id: int) -> str:
        c = await self.db.one("SELECT * FROM clients WHERE id = ?", client_id)
        if not c:
            return f"ID {client_id}"
        name = esc(c["first_name"] or "Без имени")
        return f"{name}" + (f" @{esc(c['username'])}" if c["username"] else "") + f" · <code>{client_id}</code>"

    # --- доставка сообщения клиента сотрудникам ---------------------------------
    async def deliver_from_client(self, dialog: Any, message: Message) -> bool:
        title = await self.dept_title(dialog["dept_id"])
        if self.opt("mode") == "topics" and kit.bound_chat_id():
            return await self._to_topic(dialog, message, title)
        targets = [dialog["staff_id"]] if dialog["staff_id"] else [s["user_id"] for s in
                                                                  await self.dept_staff(dialog["dept_id"])]
        if not targets:
            targets = sorted(kit.owner_ids())
        delivered = False
        fresh = dialog["status"] == "new" and await self.db.value(
            "SELECT COUNT(*) FROM msgmap WHERE dialog_id = ?", dialog["id"], default=0) == 0
        for uid in targets:
            try:
                if fresh:
                    card = await self.bot.send_message(
                        uid, f"📩 Новый диалог #{dialog['id']} · {esc(title)}\n{await self.client_line(dialog['client_id'])}",
                        reply_markup=self.card_kb(dialog["id"], bool(dialog["staff_id"])))
                    await self.map(uid, card.message_id, dialog["id"])
                copy = await self.bot.copy_message(uid, message.chat.id, message.message_id,
                                                   reply_markup=kit.kb([[kit.btn(f"↩️ Ответить #{dialog['id']}",
                                                                                 f"sv:act:{dialog['id']}")]]))
                await self.map(uid, copy.message_id, dialog["id"])
                delivered = True
            except (TelegramForbiddenError, TelegramBadRequest) as exc:
                log.info("Сотруднику %s не доставлено: %s", uid, exc)
        return delivered

    async def _to_topic(self, dialog: Any, message: Message, title: str) -> bool:
        group = kit.bound_chat_id()
        topic = dialog["topic_id"]
        if not topic:
            c = await self.db.one("SELECT * FROM clients WHERE id = ?", dialog["client_id"])
            name = (c["first_name"] if c else None) or str(dialog["client_id"])
            try:
                created = await self.bot.create_forum_topic(group, f"{name[:80]} · {title[:40]}")
            except TelegramBadRequest as exc:
                kit.notify(f"⚠️ Не получилось создать тему в группе ({exc.message}). Проверьте, что в группе "
                           "включены темы и у бота есть право управлять ими.")
                return False
            topic = created.message_thread_id
            await self.db.run("UPDATE dialogs SET topic_id = ? WHERE id = ?", topic, dialog["id"])
            card = await self.bot.send_message(group, f"📩 Диалог #{dialog['id']} · {esc(title)}\n"
                                                      f"{await self.client_line(dialog['client_id'])}\n"
                                                      "Пишите в эту тему: всё уйдёт клиенту.",
                                               message_thread_id=topic,
                                               reply_markup=kit.kb([[kit.btn("✅ Закрыть", f"sv:close:{dialog['id']}"),
                                                                     kit.btn("⛔️ Бан", f"sv:ban:{dialog['id']}")]]))
            await self.map(group, card.message_id, dialog["id"])
        try:
            copy = await self.bot.copy_message(group, message.chat.id, message.message_id, message_thread_id=topic)
        except TelegramBadRequest:
            await self.db.run("UPDATE dialogs SET topic_id = NULL WHERE id = ?", dialog["id"])
            return False
        await self.map(group, copy.message_id, dialog["id"])
        return True

    # --- ответ сотрудника клиенту -----------------------------------------------
    async def deliver_to_client(self, dialog: Any, message: Message, staff_id: int, staff_name: str) -> str | None:
        if dialog["status"] == "closed":
            return "Диалог закрыт. Клиент может написать снова, тогда откроется новый."
        banned = await self.db.value("SELECT banned FROM clients WHERE id = ?", dialog["client_id"], default=0)
        if banned:
            return "Клиент заблокирован."
        try:
            if self.opt("show_names") and dialog["last_staff"] != staff_id:
                dept = await self.dept_title(dialog["dept_id"])
                await self.bot.send_message(dialog["client_id"], f"💬 Вам отвечает {esc(staff_name)} · {esc(dept)}")
            sent = await self.bot.copy_message(dialog["client_id"], message.chat.id, message.message_id)
        except TelegramForbiddenError:
            return "Клиент заблокировал бота, сообщение не доставлено."
        except TelegramBadRequest as exc:
            return f"Не доставлено: {exc.message}"
        await self.map(dialog["client_id"], sent.message_id, dialog["id"])
        await self.db.run("UPDATE dialogs SET staff_id = COALESCE(staff_id, ?), status = 'active', last_at = ?, "
                          "first_reply = COALESCE(first_reply, ?), last_staff = ? WHERE id = ?",
                          staff_id, now(), now(), staff_id, dialog["id"])
        if dialog["staff_id"] != staff_id and self.opt("mode") == "topics":
            await self.db.run("UPDATE dialogs SET staff_id = ? WHERE id = ?", staff_id, dialog["id"])
        return None

    async def close(self, dialog_id: int, by: int | None) -> None:
        d = await self.db.one("SELECT * FROM dialogs WHERE id = ?", dialog_id)
        if not d or d["status"] == "closed":
            return
        await self.db.run("UPDATE dialogs SET status = 'closed', closed_at = ? WHERE id = ?", now(), dialog_id)
        await self.db.run("DELETE FROM active WHERE dialog_id = ?", dialog_id)
        await self.db.run("UPDATE clients SET current = NULL WHERE id = ? AND current = ?", d["client_id"], dialog_id)
        try:
            await self.bot.send_message(d["client_id"], self.opt("closed"))
        except (TelegramForbiddenError, TelegramBadRequest):
            pass
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
    return kit.kb([[kit.btn(d["title"], f"sv:dept:{d['id']}")] for d in depts])


# --- клиент ---------------------------------------------------------------------
@router.message(CommandStart(), F.chat.type == "private")
async def start(message: Message, command: CommandObject, sv: Svyaz, state: FSMContext) -> None:
    await state.clear()
    u = message.from_user
    arg = (command.args or "").strip()
    if arg.startswith("staff_"):
        inv = await sv.db.one("SELECT * FROM invites WHERE token = ?", arg[6:])
        if not inv:
            await message.answer("Приглашение недействительно. Попросите новое у владельца бота.")
            return
        await sv.db.run("INSERT OR REPLACE INTO staff(user_id, dept_id, name, added_at) VALUES (?, ?, ?, ?)",
                        u.id, inv["dept_id"], u.full_name, now())
        title = await sv.dept_title(inv["dept_id"])
        await message.answer(f"✅ Вы сотрудник отдела «{esc(title)}». Новые обращения будут приходить сюда.\n\n"
                             "Как отвечать: нажмите «Ответить» под сообщением клиента и просто пишите. "
                             "/dialogs - открытые диалоги, /close - закрыть текущий.")
        for oid in kit.owner_ids():
            try:
                await sv.bot.send_message(oid, f"👤 {esc(u.full_name)} присоединился к отделу «{esc(title)}»")
            except (TelegramForbiddenError, TelegramBadRequest):
                pass
        return
    await sv.db.run("INSERT INTO clients(id, username, first_name, created_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET username = excluded.username, first_name = excluded.first_name",
                    u.id, u.username, u.first_name, now())
    depts = await sv.departments()
    text = sv.opt("greeting")
    if sv.is_owner(u.id):
        text += "\n\n⚙️ Вы владелец: /admin - отделы, сотрудники, настройки."
    if not depts:
        await message.answer(text + "\n\nБот ещё настраивается." if not sv.is_owner(u.id) else text)
        return
    if len(depts) == 1:
        await choose(message, sv, depts[0]["id"])
        return
    await message.answer(text, reply_markup=await dept_keyboard(sv))


async def choose(target: Message, sv: Svyaz, dept_id: int, user_id: int | None = None) -> None:
    uid = user_id or target.from_user.id
    d = await sv.db.one("SELECT * FROM dialogs WHERE client_id = ? AND dept_id = ? AND status != 'closed' "
                        "ORDER BY id DESC LIMIT 1", uid, dept_id)
    dialog_id = d["id"] if d else await sv.db.run(
        "INSERT INTO dialogs(client_id, dept_id, created_at, last_at) VALUES (?, ?, ?, ?)", uid, dept_id, now(), now())
    await sv.db.run("UPDATE clients SET current = ? WHERE id = ?", dialog_id, uid)
    title = await sv.dept_title(dept_id)
    await target.answer(f"<b>{esc(title)}</b>\n{sv.opt('after_choose')}")


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
    uid = message.from_user.id
    if await sv.is_staff(uid) or (sv.is_owner(uid) and await sv.db.one("SELECT 1 FROM active WHERE staff_id = ?", uid)):
        await staff_message(message, sv)
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
    ok = await sv.deliver_from_client(dialog, message)
    await sv.db.run("UPDATE dialogs SET last_at = ? WHERE id = ?", now(), dialog["id"])
    if not ok:
        await message.answer("Сейчас не получается доставить сообщение. Попробуйте чуть позже.")


# --- сотрудники -----------------------------------------------------------------
async def staff_message(message: Message, sv: Svyaz) -> None:
    uid = message.from_user.id
    dialog = None
    if message.reply_to_message:
        dialog = await sv.dialog_of(uid, message.reply_to_message.message_id)
    if not dialog:
        active = await sv.db.one("SELECT dialog_id FROM active WHERE staff_id = ?", uid)
        if active:
            dialog = await sv.db.one("SELECT * FROM dialogs WHERE id = ?", active["dialog_id"])
    if not dialog:
        await message.answer("Выберите диалог: нажмите «Ответить» под сообщением клиента или откройте /dialogs.")
        return
    err = await sv.deliver_to_client(dialog, message, uid, message.from_user.full_name)
    if err:
        await message.answer(f"⚠️ {err}")


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
    err = await sv.deliver_to_client(dialog, message, message.from_user.id, message.from_user.full_name)
    if err:
        await message.reply(f"⚠️ {err}")


async def staff_allowed(sv: Svyaz, uid: int) -> bool:
    return sv.is_owner(uid) or await sv.is_staff(uid)


@router.callback_query(F.data.startswith("sv:act:"))
async def activate(call: CallbackQuery, sv: Svyaz) -> None:
    uid = call.from_user.id
    if not await staff_allowed(sv, uid):
        await call.answer()
        return
    dialog_id = int(call.data.split(":")[2])
    d = await sv.db.one("SELECT * FROM dialogs WHERE id = ?", dialog_id)
    if not d or d["status"] == "closed":
        await call.answer("Диалог уже закрыт", show_alert=True)
        return
    if d["staff_id"] and d["staff_id"] != uid:
        who = await sv.db.value("SELECT name FROM staff WHERE user_id = ?", d["staff_id"], default="другой сотрудник")
        await call.answer(f"Диалог уже ведёт {who}. Можно попросить передать его вам.", show_alert=True)
        return
    if not d["staff_id"]:
        await sv.db.run("UPDATE dialogs SET staff_id = ?, status = 'active' WHERE id = ?", uid, dialog_id)
        for s in await sv.dept_staff(d["dept_id"]):
            if s["user_id"] != uid:
                try:
                    await sv.bot.send_message(s["user_id"], f"Диалог #{dialog_id} взял {esc(call.from_user.full_name)}",
                                              disable_notification=True)
                except (TelegramForbiddenError, TelegramBadRequest):
                    pass
    await sv.db.run("INSERT OR REPLACE INTO active(staff_id, dialog_id) VALUES (?, ?)", uid, dialog_id)
    await call.answer(f"Диалог #{dialog_id}: всё, что вы пишете, уйдёт клиенту", show_alert=False)
    await call.message.answer(f"✍️ Сейчас вы пишете в диалог #{dialog_id} ({await sv.client_line(d['client_id'])}). "
                              "Просто отправляйте сообщения. Закончить: /close")


@router.message(Command("dialogs"), F.chat.type == "private")
async def dialogs(message: Message, sv: Svyaz) -> None:
    uid = message.from_user.id
    if not await staff_allowed(sv, uid):
        return
    if sv.is_owner(uid):
        rows = await sv.db.all("SELECT * FROM dialogs WHERE status != 'closed' ORDER BY last_at DESC LIMIT 30")
    else:
        rows = await sv.db.all("SELECT d.* FROM dialogs d WHERE d.status != 'closed' AND (d.staff_id = ? OR "
                               "(d.staff_id IS NULL AND d.dept_id IN (SELECT dept_id FROM staff WHERE user_id = ?))) "
                               "ORDER BY d.last_at DESC LIMIT 30", uid, uid)
    if not rows:
        await message.answer("Открытых диалогов нет 🎉")
        return
    buttons = []
    for d in rows:
        c = await sv.db.one("SELECT first_name FROM clients WHERE id = ?", d["client_id"])
        mark = "🆕" if d["status"] == "new" else ("👤" if d["staff_id"] == uid else "💬")
        buttons.append([kit.btn(f"{mark} #{d['id']} {(c['first_name'] if c else '') or d['client_id']} · "
                                f"{await sv.dept_title(d['dept_id'])}", f"sv:act:{d['id']}")])
    await message.answer("<b>Открытые диалоги</b>\n🆕 новый · 👤 ваш · 💬 у коллеги", reply_markup=kit.kb(buttons))


@router.message(Command("close"), F.chat.type == "private")
async def close_cmd(message: Message, sv: Svyaz) -> None:
    uid = message.from_user.id
    active = await sv.db.one("SELECT dialog_id FROM active WHERE staff_id = ?", uid)
    if not active:
        await message.answer("Нет активного диалога.")
        return
    await sv.close(active["dialog_id"], uid)
    await message.answer(f"✅ Диалог #{active['dialog_id']} закрыт.")


@router.callback_query(F.data.startswith("sv:close:"))
async def close_btn(call: CallbackQuery, sv: Svyaz) -> None:
    if not await staff_allowed(sv, call.from_user.id) and call.message.chat.id != kit.bound_chat_id():
        await call.answer()
        return
    dialog_id = int(call.data.split(":")[2])
    await sv.close(dialog_id, call.from_user.id)
    await call.answer(f"Диалог #{dialog_id} закрыт")
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("sv:ban:"))
async def ban_btn(call: CallbackQuery, sv: Svyaz) -> None:
    if not await staff_allowed(sv, call.from_user.id) and call.message.chat.id != kit.bound_chat_id():
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
    if not await staff_allowed(sv, call.from_user.id):
        await call.answer()
        return
    dialog_id = int(call.data.split(":")[2])
    depts = await sv.departments()
    rows = [[kit.btn(d["title"], f"sv:trd:{dialog_id}:{d['id']}")] for d in depts]
    await call.message.answer(f"Куда передать диалог #{dialog_id}?", reply_markup=kit.kb(rows))
    await call.answer()


@router.callback_query(F.data.startswith("sv:trd:"))
async def transfer(call: CallbackQuery, sv: Svyaz) -> None:
    if not await staff_allowed(sv, call.from_user.id):
        await call.answer()
        return
    _, _, did, dept = call.data.split(":")
    dialog_id, dept_id = int(did), int(dept)
    await sv.db.run("UPDATE dialogs SET dept_id = ?, staff_id = NULL, status = 'new' WHERE id = ?", dept_id, dialog_id)
    await sv.db.run("DELETE FROM active WHERE dialog_id = ?", dialog_id)
    d = await sv.db.one("SELECT * FROM dialogs WHERE id = ?", dialog_id)
    title = await sv.dept_title(dept_id)
    for s in await sv.dept_staff(dept_id):
        try:
            card = await sv.bot.send_message(s["user_id"], f"🔀 Вам передан диалог #{dialog_id} · {esc(title)}\n"
                                                           f"{await sv.client_line(d['client_id'])}",
                                             reply_markup=sv.card_kb(dialog_id, False))
            await sv.map(s["user_id"], card.message_id, dialog_id)
        except (TelegramForbiddenError, TelegramBadRequest):
            pass
    try:
        await sv.bot.send_message(d["client_id"], f"Ваш диалог передан: {esc(title)}. Скоро ответят.")
    except (TelegramForbiddenError, TelegramBadRequest):
        pass
    await call.message.edit_text(f"✅ Диалог #{dialog_id} передан в «{esc(title)}»")
    await call.answer()


# --- админка владельца ------------------------------------------------------------
async def admin_view(sv: Svyaz) -> tuple[str, Any]:
    depts = await sv.departments()
    day = now() - 86400
    today = await sv.db.value("SELECT COUNT(*) FROM dialogs WHERE created_at > ?", day, default=0)
    open_ = await sv.db.value("SELECT COUNT(*) FROM dialogs WHERE status != 'closed'", default=0)
    avg = await sv.db.value("SELECT AVG(first_reply - created_at) FROM dialogs WHERE first_reply IS NOT NULL AND "
                            "created_at > ?", now() - 7 * 86400)
    lines = ["⚙️ <b>Связь: админка</b>\n",
             f"📨 Диалогов сегодня: <b>{today}</b>, открыто: <b>{open_}</b>",
             f"⏱ Средний первый ответ (7 дней): <b>{int(avg // 60)} мин</b>" if avg else "⏱ Ответов пока нет",
             f"🧭 Режим: <b>{'темы в группе' if sv.opt('mode') == 'topics' else 'личка сотрудников'}</b>",
             f"👤 Имена сотрудников клиенту: <b>{'показывать' if sv.opt('show_names') else 'скрывать'}</b>", "",
             "<b>Отделы</b>"]
    for d in depts:
        staff = await sv.dept_staff(d["id"])
        lines.append(f"• {esc(d['title'])}: " + (", ".join(esc(s["name"]) for s in staff) or "нет сотрудников"))
    if not depts:
        lines.append("Пока нет. Добавьте первый отдел, например «Оператор» или «Дизайнеры».")
    if sv.opt("mode") == "topics" and not kit.bound_chat_id():
        lines.append(f"\n⚠️ Для режима тем привяжите группу с темами: <code>/bind {kit.platform().get('bind_code', '')}</code>")
    rows = [[kit.btn(f"📁 {d['title']}", f"sva:dept:{d['id']}")] for d in depts]
    rows.append([kit.btn("➕ Новый отдел", "sva:newdept", style="success")])
    rows.append([kit.btn("✏️ Приветствие", "sva:txt:greeting"), kit.btn("✏️ Текст после выбора", "sva:txt:after_choose")])
    rows.append([kit.btn(f"👤 Имена: {'вкл' if sv.opt('show_names') else 'выкл'}", "sva:tog:show_names"),
                 kit.btn(f"🧭 Режим: {'темы' if sv.opt('mode') == 'topics' else 'личка'}", "sva:mode")])
    rows.append([kit.btn("⛔️ Заблокированные", "sva:bans"), kit.btn("🔄 Обновить", "sva:home")])
    return "\n".join(lines), kit.kb(rows)


async def show_admin(target: Message | CallbackQuery, sv: Svyaz) -> None:
    text, markup = await admin_view(sv)
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            pass
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup)


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


@router.callback_query(F.data == "sva:newdept")
async def new_dept(call: CallbackQuery, sv: Svyaz, state: FSMContext) -> None:
    if not sv.is_owner(call.from_user.id):
        return
    await state.set_state(Form.dept_title)
    await call.message.answer("Название отдела (так его увидят клиенты), например: «Оператор RU»",
                              reply_markup=kit.kb([[kit.btn("Отмена", "sva:home")]]))
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
    await show_admin(message, sv)


@router.callback_query(F.data.startswith("sva:dept:"))
async def dept_card(call: CallbackQuery, sv: Svyaz) -> None:
    if not sv.is_owner(call.from_user.id):
        return
    dept_id = int(call.data.split(":")[2])
    title = await sv.dept_title(dept_id)
    staff = await sv.dept_staff(dept_id)
    token = secrets.token_urlsafe(9)
    await sv.db.run("INSERT INTO invites(token, dept_id, created_at) VALUES (?, ?, ?)", token, dept_id, now())
    link = f"https://t.me/{sv.username}?start=staff_{token}"
    text = (f"📁 <b>{esc(title)}</b>\n\nСотрудники: " + (", ".join(esc(s["name"]) for s in staff) or "нет") +
            f"\n\nСсылка-приглашение в отдел (отправьте сотруднику):\n<code>{link}</code>")
    rows = [[kit.btn("📋 Скопировать приглашение", copy=link)]]
    rows += [[kit.btn(f"✖️ Убрать {s['name']}", f"sva:rm:{dept_id}:{s['user_id']}")] for s in staff]
    rows.append([kit.btn("✏️ Переименовать", f"sva:ren:{dept_id}"), kit.btn("⬆️ Выше", f"sva:up:{dept_id}")])
    rows.append([kit.btn("🗑 Удалить отдел", f"sva:del:{dept_id}", style="danger"), kit.btn("⬅️ Назад", "sva:home")])
    try:
        await call.message.edit_text(text, reply_markup=kit.kb(rows))
    except TelegramBadRequest:
        pass
    await call.answer()


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
    await call.answer("Готово")


@router.callback_query(F.data.startswith("sva:del:"))
async def delete_dept(call: CallbackQuery, sv: Svyaz) -> None:
    if not sv.is_owner(call.from_user.id):
        return
    dept_id = int(call.data.split(":")[2])
    await sv.db.run("DELETE FROM departments WHERE id = ?", dept_id)
    await sv.db.run("UPDATE dialogs SET dept_id = NULL WHERE dept_id = ?", dept_id)
    await show_admin(call, sv)


@router.callback_query(F.data.startswith("sva:tog:"))
async def toggle(call: CallbackQuery, sv: Svyaz) -> None:
    if not sv.is_owner(call.from_user.id):
        return
    key = call.data.split(":")[2]
    if key == "show_names":
        await sv.db.set(key, not sv.opt(key))
    await show_admin(call, sv)


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
    await call.message.answer(f"Сейчас:\n<blockquote>{esc(sv.opt(key))}</blockquote>\nОтправьте новый текст.",
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
    try:
        await call.message.edit_text("⛔️ <b>Заблокированные</b>" + ("" if rows else "\n\nСписок пуст."),
                                     reply_markup=kit.kb(buttons))
    except TelegramBadRequest:
        pass
    await call.answer()


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
    task = asyncio.create_task(cleanup(db))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        task.cancel()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
