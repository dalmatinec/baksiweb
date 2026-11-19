"""Связь: переписка клиента с операторами через бота (для тех, у кого спам-блок).

- Сообщения клиента получают операторы, добавленные владельцем в /admin (по ссылке-приглашению).
  Владелец бота сам по себе оператором не является: хочет отвечать - добавляет себя той же ссылкой.
- По желанию подключается группа (/bind): обращения приходят и туда, отвечает любой участник.
  В группе с темами у каждого клиента своя тема, без тем - ответ реплаем на сообщение клиента.
- Операторы видят имя, @username, ID и ссылку на профиль клиента. Клиент видит только текст ответа.
- Ответил один - остальные операторы (и группа) получают его ответ с пометкой «✅ Имя ответил в 15:42».
- Отвечают только реплаем на сообщение клиента (в личке и в группе). Под карточкой клиента одна кнопка «Бан».
- /ban реплаем на сообщение клиента и /unban ID - операторы, владелец и участники подключённой группы.
- Клиент написал после 12 часов тишины - это новое обращение (новая карточка, снова «сообщение получено»).
- Фильтры: сообщения со стоп-словами до операторов не доходят, бот сам отвечает заданным текстом.
- Никто не получает обращения - бот напоминает владельцу. Обращения сохраняются, их видно в истории.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
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
from aiogram.types import (BotCommand, BotCommandScopeChat, CallbackQuery, Message, ReactionTypeEmoji,
                           ReplyParameters)

import baksi_kit as kit
from baksi_captcha import Captcha, menu_button
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
    """
    ALTER TABLE dialogs ADD COLUMN group_card INTEGER;
    ALTER TABLE history ADD COLUMN operator_name TEXT;
    """,
    """
    CREATE TABLE filters (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        words      TEXT NOT NULL,
        reply      TEXT,
        hits       INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL
    );
    """,
]


NEW_DIALOG_AFTER = 12 * 3600  # пауза, после которой сообщение клиента считается новым обращением
BACKLOG_DIALOGS = 10            # сколько ожидающих обращений прислать новому оператору
BACKLOG_MESSAGES = 10

DEFAULTS = {
    "greeting": "👋 Здравствуйте! Здесь можно написать нам, даже если у вашего аккаунта ограничения на сообщения. "
                "Просто напишите, что вас интересует.",
    "received": "✅ Сообщение получено, ответим здесь.",
}
TEXTS = [("greeting", "👋 Приветствие"), ("received", "✅ «Сообщение получено»")]
NO_OPERATORS_REMIND = 1800   # напоминать владельцу про операторов не чаще раза в 30 минут


def now() -> int:
    return int(time.time())


def esc(text: Any) -> str:
    return html.escape(str(text or ""))


def hhmm(ts: int | None = None) -> str:
    return time.strftime("%H:%M", time.localtime(ts or now()))


def preview(text: str, size: int = 70) -> str:
    plain = " ".join(html.unescape(re.sub(r"<[^>]+>", "", text or "")).split())
    return plain if len(plain) <= size else plain[:size - 1] + "…"


def norm(text: str) -> str:
    return (text or "").lower().replace("ё", "е")


def parse_words(raw: str) -> list[str]:
    words = [norm(w).strip() for w in re.split(r"[,\n;]+", raw or "")]
    return list(dict.fromkeys(w for w in words if w and w != "*"))[:100]


def word_pattern(word: str) -> str:
    """«цена» - только это слово целиком, «цен*» - всё, что начинается на «цен» (цена, цены, ценник)."""
    if word.endswith("*"):
        return r"(?<!\w)" + re.escape(word[:-1].strip())
    return r"(?<!\w)" + re.escape(word) + r"(?!\w)"


def find_filter(rules: list[Any], text: str) -> Any:
    text = norm(text)
    if not text:
        return None
    for rule in rules:
        for word in json.loads(rule["words"]):
            if re.search(word_pattern(word), text):
                return rule
    return None


class Svyaz:
    def __init__(self, bot: Bot, db: ModuleDB, username: str) -> None:
        self.bot = bot
        self.db = db
        self.username = username
        self.locks: dict[int, asyncio.Lock] = {}  # сообщения одного клиента по очереди (альбомы приходят пачкой)
        self.forum_cache: dict[int, tuple[bool, float]] = {}

    def lock(self, user_id: int) -> asyncio.Lock:
        if len(self.locks) > 5000:
            self.locks = {k: v for k, v in self.locks.items() if v.locked()}
        return self.locks.setdefault(user_id, asyncio.Lock())

    def opt(self, key: str) -> Any:
        """Своя настройка клиента → оформление от владельца платформы → значение по умолчанию."""
        value = self.db.get(key)
        if value is not None:
            return value
        return kit.ui_text(key, DEFAULTS[key])

    def is_owner(self, uid: int) -> bool:
        return uid in kit.owner_ids()

    async def is_operator(self, uid: int) -> bool:
        return bool(await self.db.one("SELECT 1 FROM operators WHERE user_id = ?", uid))

    async def operators(self) -> list[Any]:
        return await self.db.all("SELECT * FROM operators ORDER BY added_at")

    async def operator_ids(self) -> list[int]:
        return [r["user_id"] for r in await self.operators()]

    async def dialog(self, dialog_id: int) -> Any:
        return await self.db.one("SELECT * FROM dialogs WHERE id = ?", dialog_id)

    async def is_forum(self, group: int) -> bool:
        cached = self.forum_cache.get(group)
        if cached and time.monotonic() - cached[1] < 300:
            return cached[0]
        try:
            value = bool((await self.bot.get_chat(group)).is_forum)
        except Exception:
            value = False
        self.forum_cache[group] = (value, time.monotonic())
        return value

    async def map(self, chat_id: int, message_id: int, dialog_id: int) -> None:
        await self.db.run("INSERT OR REPLACE INTO msgmap(chat_id, message_id, dialog_id, created_at) "
                          "VALUES (?, ?, ?, ?)", chat_id, message_id, dialog_id, now())

    async def seen(self, chat_id: int, dialog_id: int) -> bool:
        """Этот чат уже получал сообщения обращения (значит, карточка клиента у него есть)."""
        return bool(await self.db.one("SELECT 1 FROM msgmap WHERE chat_id = ? AND dialog_id = ? LIMIT 1",
                                      chat_id, dialog_id))

    async def dialog_of(self, chat_id: int, message_id: int) -> Any:
        return await self.db.one("SELECT d.* FROM msgmap m JOIN dialogs d ON d.id = m.dialog_id "
                                 "WHERE m.chat_id = ? AND m.message_id = ?", chat_id, message_id)

    async def remember(self, dialog_id: int, side: str, chat_id: int, message_id: int,
                       operator_id: int | None = None, operator_name: str | None = None) -> None:
        await self.db.run("INSERT INTO history(dialog_id, side, chat_id, message_id, operator_id, operator_name, "
                          "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", dialog_id, side, chat_id, message_id,
                          operator_id, operator_name, now())

    async def client_card(self, client_id: int) -> str:
        c = await self.db.one("SELECT * FROM clients WHERE id = ?", client_id)
        name = esc((c["first_name"] if c else None) or "Без имени")
        parts = [f'👤 <a href="tg://user?id={client_id}">{name}</a>']
        if c and c["username"]:
            parts.append(f"@{esc(c['username'])}")
        parts.append(f"ID <code>{client_id}</code>")
        return " · ".join(parts)

    def card_kb(self, dialog_id: int):
        return kit.kb([[kit.ui_btn("sv_ban", "⛔️ Бан", f"sv:ban:{dialog_id}", style="danger")]])

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

    def group_place(self, dialog: Any) -> dict:
        """Куда писать в группе по этому обращению: в тему клиента или ответом на его карточку."""
        if dialog["topic_id"]:
            return {"message_thread_id": dialog["topic_id"]}
        if dialog["group_card"]:
            return {"reply_parameters": ReplyParameters(message_id=dialog["group_card"],
                                                        allow_sending_without_reply=True)}
        return {}

    # --- нет получателей ----------------------------------------------------------
    async def remind_no_operators(self) -> None:
        if now() - int(self.db.get("no_ops_reminded", 0) or 0) < NO_OPERATORS_REMIND:
            return
        await self.db.set("no_ops_reminded", now())
        waiting = await self.db.value("SELECT COUNT(*) FROM dialogs WHERE status != 'closed'", default=0)
        text = (f"⚠️ Связь @{self.username}: обращения никто не видит (ждут ответа: {waiting}).\n"
                "Откройте бота → /admin → 👥 Операторы и отправьте ссылку-приглашение тем, кто будет отвечать "
                "(себя можно добавить той же ссылкой), или подключите группу: /admin → 💬 Группа.")
        kit.notify(text)
        for oid in kit.owner_ids():  # и в самом боте, если владелец его уже открывал
            await self.send_safe(lambda oid=oid: self.bot.send_message(oid, text))

    # --- сообщение клиента → операторам и в группу --------------------------------
    async def deliver_from_client(self, dialog: Any, message: Message) -> int:
        """Возвращает, скольким получателям доставлено."""
        group = kit.bound_chat_id()
        delivered = 1 if group and await self._to_group(dialog, message, group) else 0
        targets = await self.operator_ids()
        if not targets and not group:
            await self.remind_no_operators()
            return 0
        card_text = None
        for uid in targets:
            if not await self.seen(uid, dialog["id"]):
                card_text = card_text or (f"📩 <b>Новое обращение #{dialog['id']}</b>\n"
                                          f"{await self.client_card(dialog['client_id'])}")
                card = await self.send_safe(lambda uid=uid: self.bot.send_message(
                    uid, card_text + "\nОтвечайте реплаем на сообщения клиента.",
                    reply_markup=self.card_kb(dialog["id"])))
                if card:
                    await self.map(uid, card.message_id, dialog["id"])
            copy = await self.send_safe(lambda uid=uid: self.bot.copy_message(
                uid, message.chat.id, message.message_id))
            if copy:
                await self.map(uid, copy.message_id, dialog["id"])
                delivered += 1
        return delivered

    async def _to_group(self, dialog: Any, message: Message, group: int, retry: bool = True) -> bool:
        if not await self.seen(group, dialog["id"]):  # впервые в этой группе: новая тема или карточка
            await self.db.run("UPDATE dialogs SET topic_id = NULL, group_card = NULL WHERE id = ?", dialog["id"])
            topic = None
            if await self.is_forum(group):
                c = await self.db.one("SELECT * FROM clients WHERE id = ?", dialog["client_id"])
                name = (c["first_name"] if c else None) or str(dialog["client_id"])
                try:
                    topic = (await self.bot.create_forum_topic(group, f"{name[:90]} #{dialog['id']}")).message_thread_id
                except TelegramBadRequest as exc:
                    if now() - int(self.db.get("topic_fail_reminded", 0) or 0) > 3600:
                        await self.db.set("topic_fail_reminded", now())
                        kit.notify(f"⚠️ Связь: не получилось создать тему в группе ({exc.message}). Сделайте бота "
                                   "админом с правом управлять темами. Пока обращения идут в общий чат группы.")
            hint = "Пишите в эту тему: всё уйдёт клиенту." if topic else "Отвечайте реплаем на сообщения клиента."
            card_text = (f"📩 <b>Новое обращение #{dialog['id']}</b>\n{await self.client_card(dialog['client_id'])}\n"
                         f"{hint}")
            card = await self.send_safe(lambda: self.bot.send_message(
                group, card_text, message_thread_id=topic, reply_markup=self.card_kb(dialog["id"])))
            if not card:
                return False
            await self.map(group, card.message_id, dialog["id"])
            await self.db.run("UPDATE dialogs SET topic_id = ?, group_card = ? WHERE id = ?",
                              topic, None if topic else card.message_id, dialog["id"])
            dialog = await self.dialog(dialog["id"])
        try:
            copy = await self.bot.copy_message(group, message.chat.id, message.message_id, **self.group_place(dialog))
        except TelegramBadRequest:
            # тему удалили вручную: начнём заново с новой карточкой
            await self.db.run("DELETE FROM msgmap WHERE chat_id = ? AND dialog_id = ?", group, dialog["id"])
            return retry and await self._to_group(await self.dialog(dialog["id"]), message, group, retry=False)
        except TelegramForbiddenError:
            return False
        await self.map(group, copy.message_id, dialog["id"])
        return True

    # --- ответ оператора ------------------------------------------------------------
    async def deliver_to_client(self, dialog: Any, message: Message, operator_id: int, name: str,
                                from_group: bool = False) -> str | None:
        if await self.db.value("SELECT banned FROM clients WHERE id = ?", dialog["client_id"], default=0):
            return "Клиент заблокирован."
        try:  # клиент видит только сам ответ, без имени оператора
            sent = await self.bot.copy_message(dialog["client_id"], message.chat.id, message.message_id)
        except TelegramForbiddenError:
            return "Клиент заблокировал бота, сообщение не доставлено."
        except TelegramBadRequest as exc:
            return f"Не доставлено: {exc.message}"
        await self.map(dialog["client_id"], sent.message_id, dialog["id"])
        await self.remember(dialog["id"], "operator", message.chat.id, message.message_id, operator_id, name)
        await self.db.run("UPDATE dialogs SET staff_id = ?, status = 'active', last_at = ?, closed_at = NULL, "
                          "first_reply = COALESCE(first_reply, ?), last_staff = ? WHERE id = ?",
                          operator_id, now(), now(), operator_id, dialog["id"])
        if dialog["status"] == "closed":  # ответили на старое сообщение - переписка продолжается в этом обращении
            await self.db.run("UPDATE dialogs SET status = 'closed', closed_at = ? WHERE client_id = ? AND id != ? "
                              "AND status != 'closed'", now(), dialog["client_id"], dialog["id"])
            await self.db.run("UPDATE clients SET current = ? WHERE id = ?", dialog["id"], dialog["client_id"])
        await self.mirror(dialog, message, operator_id, name, from_group)
        return None

    async def mirror(self, dialog: Any, message: Message, operator_id: int, name: str, from_group: bool) -> None:
        """Остальные видят, что и когда ответил коллега, - чтобы не отвечать повторно."""
        head_text = f"✅ <b>{esc(name)}</b> ответил в {hhmm()} · обращение #{dialog['id']}"
        for uid in await self.operator_ids():
            if uid == operator_id:
                continue
            head = await self.send_safe(lambda uid=uid: self.bot.send_message(
                uid, head_text, disable_notification=True))
            if head:
                await self.map(uid, head.message_id, dialog["id"])
            copy = await self.send_safe(lambda uid=uid: self.bot.copy_message(
                uid, message.chat.id, message.message_id, disable_notification=True))
            if copy:
                await self.map(uid, copy.message_id, dialog["id"])
        group = kit.bound_chat_id()
        if group and not from_group and await self.seen(group, dialog["id"]):
            place = self.group_place(await self.dialog(dialog["id"]))
            head = await self.send_safe(lambda: self.bot.send_message(group, head_text, disable_notification=True,
                                                                      **place))
            if head:
                await self.map(group, head.message_id, dialog["id"])
            copy = await self.send_safe(lambda: self.bot.copy_message(group, message.chat.id, message.message_id,
                                                                      disable_notification=True, **place))
            if copy:
                await self.map(group, copy.message_id, dialog["id"])

    async def finish(self, dialog: Any) -> None:
        """Тихо завершить обращение: клиенту ничего не пишем, тему в группе закрываем."""
        await self.db.run("UPDATE dialogs SET status = 'closed', closed_at = ? WHERE id = ? AND status != 'closed'",
                          now(), dialog["id"])
        await self.db.run("UPDATE clients SET current = NULL WHERE id = ? AND current = ?", dialog["client_id"],
                          dialog["id"])
        group = kit.bound_chat_id()
        if group and dialog["topic_id"] and await self.seen(group, dialog["id"]):
            try:
                await self.bot.close_forum_topic(group, dialog["topic_id"])
            except TelegramBadRequest:
                pass

    async def ban(self, client_id: int) -> None:
        await self.db.run("INSERT INTO clients(id, created_at) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET banned = 1",
                          client_id, now())
        await self.db.run("UPDATE clients SET banned = 1 WHERE id = ?", client_id)
        for d in await self.db.all("SELECT * FROM dialogs WHERE client_id = ? AND status != 'closed'", client_id):
            await self.finish(d)

    async def send_backlog(self, uid: int) -> int:
        """Новому оператору - обращения, которые ждут ответа, чтобы ничего не потерялось."""
        rows = await self.db.all("SELECT * FROM dialogs WHERE status = 'new' AND last_at > ? ORDER BY last_at DESC "
                                 "LIMIT ?", now() - NEW_DIALOG_AFTER, BACKLOG_DIALOGS)
        for d in reversed(rows):
            card = await self.send_safe(lambda d=d: self._backlog_card(uid, d))
            if not card:
                continue
            await self.map(uid, card.message_id, d["id"])
            msgs = await self.db.all("SELECT * FROM (SELECT * FROM history WHERE dialog_id = ? AND side = 'client' "
                                     "ORDER BY id DESC LIMIT ?) ORDER BY id", d["id"], BACKLOG_MESSAGES)
            for m in msgs:
                copy = await self.send_safe(lambda m=m: self.bot.copy_message(uid, m["chat_id"], m["message_id"],
                                                                              disable_notification=True))
                if copy:
                    await self.map(uid, copy.message_id, d["id"])
                await asyncio.sleep(0.05)
        return len(rows)

    async def _backlog_card(self, uid: int, d: Any):
        return await self.bot.send_message(uid, f"📩 <b>Ждёт ответа: обращение #{d['id']}</b>\n"
                                                f"{await self.client_card(d['client_id'])}\n"
                                                "Отвечайте реплаем на сообщения клиента.",
                                           reply_markup=self.card_kb(d["id"]), disable_notification=True)


router = Router(name="svyaz")


class Form(StatesGroup):
    text = State()
    filter_words = State()
    filter_reply = State()


def not_command(message: Message) -> bool:
    return not (message.text or "").startswith("/")


async def new_invite(sv: Svyaz) -> str:
    token = secrets.token_urlsafe(9)
    await sv.db.run("INSERT INTO invites(token, dept_id, created_at) VALUES (?, 0, ?)", token, now())
    return f"https://t.me/{sv.username}?start=op_{token}"


async def set_commands(sv: Svyaz, uid: int) -> None:
    """Меню команд по ролям: клиенты видят только /start, операторы - /dialogs, владелец - /admin."""
    commands = [BotCommand(command="start", description="Начать")]
    if await sv.is_operator(uid):
        commands += [BotCommand(command="ban", description="Заблокировать клиента (реплаем на его сообщение)"),
                     BotCommand(command="unban", description="Разблокировать: /unban ID")]
    if sv.is_owner(uid):
        commands.append(BotCommand(command="admin", description="Настройки бота"))
    try:
        await sv.bot.set_my_commands(commands, scope=BotCommandScopeChat(chat_id=uid))
    except Exception:  # владелец ещё не открывал бота - выставим при его /start
        pass


async def mark_delivered(sv: Svyaz, message: Message) -> None:
    try:  # 👍 на сообщении оператора: ответ ушёл клиенту (видят и оператор, и участники группы)
        await sv.bot.set_message_reaction(message.chat.id, message.message_id, [ReactionTypeEmoji(emoji="👍")])
    except Exception:
        pass


# --- клиент и приглашения -----------------------------------------------------------
@router.message(CommandStart(), F.chat.type == "private")
async def start(message: Message, command: CommandObject, sv: Svyaz, state: FSMContext) -> None:
    await state.clear()
    u = message.from_user
    arg = (command.args or "").strip()
    token = arg[3:] if arg.startswith("op_") else arg[6:] if arg.startswith("staff_") else ""
    if token:
        if not await sv.db.one("SELECT 1 FROM invites WHERE token = ?", token):
            await message.answer("Приглашение недействительно. Попросите новое у владельца бота.")
            return
        await sv.db.run("INSERT INTO operators(user_id, name, username, added_at) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(user_id) DO UPDATE SET name = excluded.name, username = excluded.username",
                        u.id, u.full_name, u.username, now())
        await set_commands(sv, u.id)
        await message.answer(
            "✅ Вы оператор. Обращения клиентов будут приходить сюда.\n\n"
            "Как отвечать: ответьте реплаем на сообщение клиента. Клиент видит только текст ответа, ваше имя ему "
            "не показывается. Другие операторы видят, что вы ответили, а на вашем ответе появится 👍.\n"
            "Заблокировать: /ban реплаем на сообщение клиента или кнопка «Бан». Разблокировать: /unban ID.")
        await sv.send_backlog(u.id)
        for oid in kit.owner_ids():
            if oid != u.id:
                await sv.send_safe(lambda oid=oid: sv.bot.send_message(oid, f"👤 {esc(u.full_name)} теперь оператор"))
        return
    await sv.db.run("INSERT INTO clients(id, username, first_name, created_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET username = excluded.username, first_name = excluded.first_name",
                    u.id, u.username, u.first_name, now())
    text = sv.opt("greeting")
    if sv.is_owner(u.id) or await sv.is_operator(u.id):
        await set_commands(sv, u.id)
    if sv.is_owner(u.id):
        text += "\n\n⚙️ Вы владелец: /admin - операторы, группа, тексты."
        if not await sv.operators() and not kit.bound_chat_id():
            text += "\n⚠️ Обращения пока никто не видит: добавьте операторов или подключите группу в /admin."
    await message.answer(text)


@router.message(StateFilter(None), F.chat.type == "private", not_command)
async def private_message(message: Message, sv: Svyaz) -> None:
    async with sv.lock(message.from_user.id):
        await _private_message(message, sv)


async def _private_message(message: Message, sv: Svyaz) -> None:
    uid = message.from_user.id
    if await sv.is_operator(uid):
        await operator_message(message, sv)
        return
    # имя и @username клиента обновляем с каждым сообщением: операторы видят актуальные данные
    await sv.db.run("INSERT INTO clients(id, username, first_name, created_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET username = excluded.username, first_name = excluded.first_name",
                    uid, message.from_user.username, message.from_user.first_name, now())
    client = await sv.db.one("SELECT * FROM clients WHERE id = ?", uid)
    if client["banned"]:
        return
    rule = find_filter(await sv.db.all("SELECT * FROM filters ORDER BY id"), message.text or message.caption or "")
    if rule:  # стоп-слово: операторам не пересылаем, бот отвечает сам
        await sv.db.run("UPDATE filters SET hits = hits + 1 WHERE id = ?", rule["id"])
        if rule["reply"]:
            await message.answer(rule["reply"])
        return
    dialog = None
    if client["current"]:
        dialog = await sv.db.one("SELECT * FROM dialogs WHERE id = ? AND status != 'closed'", client["current"])
        if dialog and now() - dialog["last_at"] > NEW_DIALOG_AFTER:
            await sv.finish(dialog)  # давно молчали - это уже новое обращение
            dialog = None
    if not dialog:
        did = await sv.db.run("INSERT INTO dialogs(client_id, created_at, last_at) VALUES (?, ?, ?)", uid, now(), now())
        await sv.db.run("UPDATE clients SET current = ? WHERE id = ?", did, uid)
        dialog = await sv.dialog(did)
    fresh = not await sv.db.one("SELECT 1 FROM history WHERE dialog_id = ? LIMIT 1", dialog["id"])
    await sv.remember(dialog["id"], "client", uid, message.message_id)
    await sv.db.run("UPDATE dialogs SET last_at = ? WHERE id = ?", now(), dialog["id"])
    await sv.deliver_from_client(dialog, message)
    if fresh:  # клиенту одно короткое подтверждение на обращение, без подробностей о том, кто его увидит
        await message.answer(sv.opt("received"))


# --- операторы в личке ---------------------------------------------------------------
async def operator_message(message: Message, sv: Svyaz) -> None:
    uid = message.from_user.id
    dialog = await sv.dialog_of(uid, message.reply_to_message.message_id) if message.reply_to_message else None
    if not dialog:
        await message.answer("Чтобы ответить клиенту, ответьте реплаем на его сообщение.")
        return
    err = await sv.deliver_to_client(dialog, message, uid, message.from_user.full_name)
    if err:
        await message.answer(f"⚠️ {err}")
    else:
        await mark_delivered(sv, message)


# --- ответы из подключённой группы ----------------------------------------------------
@router.message(F.chat.type.in_({"group", "supergroup"}), not_command)
async def group_message(message: Message, sv: Svyaz) -> None:
    if message.chat.id != kit.bound_chat_id() or not message.from_user or message.from_user.is_bot:
        return
    if (message.text or "").startswith("/"):
        return
    dialog = None
    if message.message_thread_id and message.is_topic_message:
        dialog = await sv.db.one("SELECT * FROM dialogs WHERE topic_id = ? ORDER BY id DESC LIMIT 1",
                                 message.message_thread_id)
    if not dialog and message.reply_to_message:
        dialog = await sv.dialog_of(message.chat.id, message.reply_to_message.message_id)
    if not dialog:
        return
    err = await sv.deliver_to_client(dialog, message, message.from_user.id, message.from_user.full_name,
                                     from_group=True)
    if err:
        await message.reply(f"⚠️ {err}")
    else:
        await mark_delivered(sv, message)


@router.callback_query(F.data.regexp(r"^sv:(act|hist|close):"))
async def old_buttons(call: CallbackQuery) -> None:  # кнопки из прежних версий в старых сообщениях
    await call.answer("Отвечайте реплаем на сообщение клиента. Заблокировать: /ban реплаем.", show_alert=True)


async def may_moderate(sv: Svyaz, message: Message) -> bool:
    """Банить могут операторы, владелец и участники подключённой группы (они же отвечают клиентам)."""
    if message.chat.type == "private":
        return sv.is_owner(message.from_user.id) or await sv.is_operator(message.from_user.id)
    return message.chat.id == kit.bound_chat_id()


def ban_text(client_id: int) -> str:
    return (f"⛔️ Клиент <code>{client_id}</code> заблокирован: его сообщения больше не доходят.\n"
            f"Разблокировать: <code>/unban {client_id}</code>")


@router.message(Command("ban"))
async def ban_cmd(message: Message, sv: Svyaz) -> None:
    if not message.from_user or not await may_moderate(sv, message):
        return
    d = None
    if message.reply_to_message:
        d = await sv.dialog_of(message.chat.id, message.reply_to_message.message_id)
    if not d and message.message_thread_id and message.is_topic_message and message.chat.type != "private":
        d = await sv.db.one("SELECT * FROM dialogs WHERE topic_id = ? ORDER BY id DESC LIMIT 1", message.message_thread_id)
    if not d:
        await message.reply("Отправьте /ban реплаем на сообщение клиента.")
        return
    await sv.ban(d["client_id"])
    await message.reply(ban_text(d["client_id"]))


@router.message(Command("unban"))
async def unban_cmd(message: Message, command: CommandObject, sv: Svyaz) -> None:
    if not message.from_user or not await may_moderate(sv, message):
        return
    arg = (command.args or "").strip()
    if not arg.lstrip("-").isdigit():
        await message.reply("Напишите ID клиента: <code>/unban 123456789</code>. ID есть в карточке клиента и в "
                            "сообщении о блокировке.")
        return
    client_id = int(arg)
    if not await sv.db.value("SELECT banned FROM clients WHERE id = ?", client_id, default=0):
        await message.reply(f"Клиент <code>{client_id}</code> не заблокирован.")
        return
    await sv.db.run("UPDATE clients SET banned = 0 WHERE id = ?", client_id)
    await message.reply(f"✅ Клиент <code>{client_id}</code> разблокирован.")


@router.callback_query(F.data.startswith("sv:ban:"))
async def ban_btn(call: CallbackQuery, sv: Svyaz) -> None:
    allowed = (call.message and call.message.chat.type != "private" and call.message.chat.id == kit.bound_chat_id()) \
        or sv.is_owner(call.from_user.id) or await sv.is_operator(call.from_user.id)
    d = await sv.dialog(int(call.data.split(":")[2])) if allowed else None
    if not d:
        await call.answer()
        return
    await sv.ban(d["client_id"])
    await call.answer(f"Клиент {d['client_id']} заблокирован. Разблокировать: /unban {d['client_id']}", show_alert=True)
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass


# --- админка владельца ----------------------------------------------------------------
BACK = [kit.btn("⬅️ Назад", "sva:home")]


async def admin_view(sv: Svyaz) -> tuple[str, Any]:
    ops = await sv.operators()
    group = kit.bound_chat_id()
    day = now() - 86400
    today = await sv.db.value("SELECT COUNT(*) FROM dialogs WHERE created_at > ?", day, default=0)
    open_ = await sv.db.value("SELECT COUNT(*) FROM dialogs WHERE status != 'closed'", default=0)
    unanswered = await sv.db.value("SELECT COUNT(*) FROM dialogs WHERE status = 'new'", default=0)
    avg = await sv.db.value("SELECT AVG(first_reply - created_at) FROM dialogs WHERE first_reply IS NOT NULL AND "
                            "created_at > ?", now() - 7 * 86400)
    lines = ["⚙️ <b>Связь: админка</b>\n"]
    if not ops and not group:
        lines.append("⚠️ <b>Обращения никто не видит.</b> Добавьте операторов или подключите группу.\n")
    names = ", ".join(esc(o["name"]) for o in ops[:5]) + ("…" if len(ops) > 5 else "")
    lines += [f"👥 Операторов: <b>{len(ops)}</b>" + (f" ({names})" if ops else ""),
              f"💬 Группа: <b>{esc(kit.state().get('title') or 'подключена')}</b>" if group else "💬 Группа: не подключена",
              f"📨 Обращений сегодня: <b>{today}</b> · открыто: <b>{open_}</b> · без ответа: <b>{unanswered}</b>",
              f"⏱ Средний первый ответ (7 дней): <b>{int(avg // 60)} мин</b>" if avg else "⏱ Ответов пока нет"]
    filters_count, filtered = await sv.db.one("SELECT COUNT(*), COALESCE(SUM(hits), 0) FROM filters")
    if filters_count:
        lines.append(f"🚫 Фильтров: <b>{filters_count}</b> · отсеяно сообщений: <b>{filtered}</b>")
    empty = not ops and not group
    rows = [[kit.btn("👥 Операторы", "sva:ops", style="primary" if empty else None), kit.btn("💬 Группа", "sva:group")],
            [kit.btn("✏️ Тексты", "sva:texts"), kit.btn("🚫 Фильтры", "sva:flt")],
            [kit.btn("⛔️ Заблокированные", "sva:bans"), menu_button()],
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
    link = await new_invite(sv)
    listing = "\n".join(f"• {esc(o['name'])}" + (f" @{esc(o['username'])}" if o["username"] else "")
                        + f" · <code>{o['user_id']}</code>" for o in ops) or "Пока никого."
    text = ("👥 <b>Операторы</b>\nПолучают обращения в личку бота и отвечают там. Владелец бота - не оператор, "
            "пока сам не откроет ссылку-приглашение.\n\n" + listing +
            f"\n\nСсылка-приглашение (отправьте оператору, действует 7 дней):\n<code>{link}</code>")
    rows = [[kit.btn("📋 Скопировать приглашение", copy=link)]]
    rows += [[kit.btn(f"✖️ Убрать {o['name']}", f"sva:oprm:{o['user_id']}")] for o in ops]
    rows.append(BACK)
    await show(call, text, kit.kb(rows))


@router.callback_query(F.data.startswith("sva:oprm:"))
async def admin_operator_remove(call: CallbackQuery, sv: Svyaz) -> None:
    if not sv.is_owner(call.from_user.id):
        await call.answer()
        return
    uid = int(call.data.split(":")[2])
    await sv.db.run("DELETE FROM operators WHERE user_id = ?", uid)
    await sv.send_safe(lambda: sv.bot.send_message(uid, "Вы больше не оператор этого бота."))
    await set_commands(sv, uid)
    await admin_operators(call, sv)


@router.callback_query(F.data == "sva:group")
async def admin_group(call: CallbackQuery, sv: Svyaz) -> None:
    if not sv.is_owner(call.from_user.id):
        await call.answer()
        return
    group = kit.bound_chat_id()
    if group:
        forum = await sv.is_forum(group)
        text = (f"💬 <b>Группа «{esc(kit.state().get('title') or group)}» подключена</b>\n\n"
                "Обращения приходят в группу" + (" и операторам в личку" if await sv.operators() else "") + ". "
                + ("В группе включены темы: у каждого клиента своя тема, просто пишите в неё."
                   if forum else "Отвечайте реплаем на сообщение клиента.")
                + "\n\nОтвечать из группы может любой её участник, поэтому добавляйте туда только своих. "
                "Клиент имени отвечающего не видит.")
        rows = [[kit.btn("🔌 Отключить группу", "sva:unbind", style="danger")], BACK]
    else:
        code = kit.platform().get("bind_code", "")
        text = ("💬 <b>Группа</b> (необязательно)\n\nМожно отвечать клиентам из общей группы: обращения будут "
                "приходить туда, ответить может любой участник.\n\n"
                "1. Добавьте бота в группу и сделайте админом.\n"
                "2. Если в группе включены темы, у каждого клиента будет своя тема (боту нужно право управлять темами). "
                "Без тем отвечайте реплаем на сообщение клиента.\n"
                f"3. Отправьте в группу: <code>/bind {esc(code)}</code>")
        rows = [[kit.btn(f"📋 Скопировать /bind {code}", copy=f"/bind {code}")]] if code else []
        rows.append(BACK)
    await show(call, text, kit.kb(rows))


@router.callback_query(F.data == "sva:unbind")
async def admin_unbind(call: CallbackQuery, sv: Svyaz) -> None:
    if not sv.is_owner(call.from_user.id):
        await call.answer()
        return
    group = kit.bound_chat_id()
    if group:
        kit.save_state(chat_id=None, title=None)
        kit.emit("unbound", chat_id=group)
        await sv.send_safe(lambda: sv.bot.send_message(group, "Группа отключена от бота: обращения сюда больше "
                                                              "не приходят."))
    await admin_group(call, sv)


@router.callback_query(F.data == "sva:texts")
async def admin_texts(call: CallbackQuery, sv: Svyaz, state: FSMContext) -> None:
    if not sv.is_owner(call.from_user.id):
        await call.answer()
        return
    await state.clear()
    lines = ["✏️ <b>Тексты</b>\nЧто видит клиент. Нажмите, чтобы изменить.\n"]
    for key, label in TEXTS:
        lines.append(f"<b>{label}</b>" + (" ✎" if sv.db.get(key) is not None else "") +
                     f"\n<i>{esc(preview(sv.opt(key)))}</i>\n")
    rows = [[kit.btn(label, f"sva:txt:{key}")] for key, label in TEXTS]
    rows.append(BACK)
    await show(call, "\n".join(lines), kit.kb(rows))


@router.callback_query(F.data.startswith("sva:txt:"))
async def edit_text(call: CallbackQuery, sv: Svyaz, state: FSMContext) -> None:
    if not sv.is_owner(call.from_user.id):
        return
    key = call.data.split(":")[2]
    if key not in DEFAULTS:
        await call.answer()
        return
    await state.set_state(Form.text)
    await state.update_data(key=key)
    rows = [[kit.btn("↩️ Вернуть стандартный", f"sva:txr:{key}")]] if sv.db.get(key) is not None else []
    rows.append([kit.btn("⬅️ Назад", "sva:texts")])
    await show(call, f"<b>{dict(TEXTS)[key]}</b>\n\nСейчас:\n<blockquote>{sv.opt(key)}</blockquote>\n"
                     "Отправьте новый текст. Форматирование и премиум-эмодзи сохранятся.", kit.kb(rows))


@router.callback_query(F.data.startswith("sva:txr:"))
async def reset_text(call: CallbackQuery, sv: Svyaz, state: FSMContext) -> None:
    if not sv.is_owner(call.from_user.id):
        return
    key = call.data.split(":")[2]
    if key in DEFAULTS:
        await sv.db.set(key, None)
    await call.answer("Вернули стандартный текст")
    await admin_texts(call, sv, state)


@router.message(Form.text, F.chat.type == "private", F.text)
async def edit_text_input(message: Message, sv: Svyaz, state: FSMContext) -> None:
    if not sv.is_owner(message.from_user.id):
        return
    key = (await state.get_data()).get("key")
    if key not in DEFAULTS:
        await state.clear()
        return
    await sv.db.set(key, message.html_text[:3500])
    await state.clear()
    lines = ["✅ <b>Сохранено.</b> Теперь клиенты видят так:\n", message.html_text[:3500]]
    rows = [[kit.btn("✏️ Тексты", "sva:texts"), kit.btn("⚙️ Админка", "sva:home")]]
    await message.answer("\n".join(lines), reply_markup=kit.kb(rows))


@router.callback_query(F.data == "sva:bans")
async def bans(call: CallbackQuery, sv: Svyaz) -> None:
    if not sv.is_owner(call.from_user.id):
        return
    rows = await sv.db.all("SELECT * FROM clients WHERE banned = 1 ORDER BY created_at DESC LIMIT 40")
    buttons = [[kit.btn(f"✅ Разбанить {r['first_name'] or r['id']}", f"sva:unban:{r['id']}")] for r in rows]
    buttons.append(BACK)
    await show(call, "⛔️ <b>Заблокированные</b>" + ("" if rows else "\n\nСписок пуст."), kit.kb(buttons))


@router.callback_query(F.data.startswith("sva:unban:"))
async def unban(call: CallbackQuery, sv: Svyaz) -> None:
    if not sv.is_owner(call.from_user.id):
        return
    await sv.db.run("UPDATE clients SET banned = 0 WHERE id = ?", int(call.data.split(":")[2]))
    await bans(call, sv)


FILTER_HELP = ("Стоп-слова через запятую или с новой строки. Регистр не важен.\n"
               "• <code>цена</code> - только слово целиком\n"
               "• <code>цен*</code> - всё, что начинается на «цен»: цена, цены, ценник\n"
               "• <code>как купить</code> - фраза целиком")


def filter_line(rule: Any) -> str:
    words = ", ".join(json.loads(rule["words"]))
    answer = f"«{esc(preview(rule['reply'], 50))}»" if rule["reply"] else "без ответа"
    return f"<b>{esc(preview(words, 60))}</b> → {answer} · сработал: {rule['hits']}"


async def filter_card(sv: Svyaz, rule_id: int) -> tuple[str, Any]:
    rule = await sv.db.one("SELECT * FROM filters WHERE id = ?", rule_id)
    if not rule:
        return "Фильтр удалён.", kit.kb([[kit.btn("⬅️ К фильтрам", "sva:flt")]])
    text = (f"🚫 <b>Фильтр #{rule['id']}</b>\n\nСтоп-слова: <code>{esc(', '.join(json.loads(rule['words'])))}</code>\n\n"
            + (f"Бот ответит:\n<blockquote>{rule['reply']}</blockquote>" if rule["reply"]
               else "Бот ничего не ответит, сообщение просто не дойдёт до операторов.")
            + f"\n\nСработал: {rule['hits']} раз")
    rows = [[kit.btn("✏️ Стоп-слова", f"sva:flw:{rule_id}"), kit.btn("✏️ Ответ", f"sva:flr:{rule_id}")],
            [kit.btn("🗑 Удалить", f"sva:fldel:{rule_id}", style="danger"), kit.btn("⬅️ К фильтрам", "sva:flt")]]
    return text, kit.kb(rows)


@router.callback_query(F.data == "sva:flt")
async def admin_filters(call: CallbackQuery, sv: Svyaz, state: FSMContext) -> None:
    if not sv.is_owner(call.from_user.id):
        await call.answer()
        return
    await state.clear()
    rules = await sv.db.all("SELECT * FROM filters ORDER BY id")
    text = ("🚫 <b>Фильтры</b>\nСообщение клиента со стоп-словом не дойдёт до операторов и группы, а бот сам ответит "
            "заданным текстом. Удобно для частых вопросов (прайс, адрес, график) и для отсева мусора.\n\n"
            + ("\n".join(f"{i}. {filter_line(r)}" for i, r in enumerate(rules, 1)) or "Фильтров пока нет."))
    rows = [[kit.btn(f"{i}. {preview(', '.join(json.loads(r['words'])), 40)}", f"sva:fl:{r['id']}")]
            for i, r in enumerate(rules, 1)]
    rows.append([kit.btn("➕ Добавить фильтр", "sva:fladd", style="success")])
    rows.append(BACK)
    await show(call, text, kit.kb(rows))


@router.callback_query(F.data.startswith("sva:fl:"))
async def admin_filter(call: CallbackQuery, sv: Svyaz, state: FSMContext) -> None:
    if not sv.is_owner(call.from_user.id):
        await call.answer()
        return
    await state.clear()
    text, markup = await filter_card(sv, int(call.data.split(":")[2]))
    await show(call, text, markup)


@router.callback_query(F.data == "sva:fladd")
@router.callback_query(F.data.startswith("sva:flw:"))
async def filter_words_ask(call: CallbackQuery, sv: Svyaz, state: FSMContext) -> None:
    if not sv.is_owner(call.from_user.id):
        await call.answer()
        return
    edit = int(call.data.split(":")[2]) if call.data.startswith("sva:flw:") else None
    await state.set_state(Form.filter_words)
    await state.update_data(edit=edit)
    back = f"sva:fl:{edit}" if edit else "sva:flt"
    await show(call, ("✏️ <b>Новые стоп-слова</b>" if edit else "➕ <b>Новый фильтр</b>, шаг 1 из 2") + "\n\n" + FILTER_HELP,
               kit.kb([[kit.btn("Отмена", back)]]))


@router.message(Form.filter_words, F.chat.type == "private", F.text)
async def filter_words_input(message: Message, sv: Svyaz, state: FSMContext) -> None:
    if not sv.is_owner(message.from_user.id):
        return
    words = parse_words(message.text)
    if not words:
        await message.answer("⚠️ <b>Не сохранено.</b> Не нашёл ни одного слова, отправьте ещё раз.")
        return
    edit = (await state.get_data()).get("edit")
    if edit:
        await sv.db.run("UPDATE filters SET words = ? WHERE id = ?", json.dumps(words, ensure_ascii=False), edit)
        await state.clear()
        text, markup = await filter_card(sv, edit)
        await message.answer("✅ <b>Сохранено.</b>\n\n" + text, reply_markup=markup)
        return
    await state.set_state(Form.filter_reply)
    await state.update_data(words=words)
    await message.answer(f"✅ Стоп-слова: <code>{esc(', '.join(words))}</code>\n\n<b>Шаг 2 из 2.</b> Что бот ответит "
                         "автоматически? Отправьте текст: форматирование и премиум-эмодзи сохранятся.",
                         reply_markup=kit.kb([[kit.btn("🔇 Без ответа, просто не пересылать", "sva:flnone")],
                                              [kit.btn("Отмена", "sva:flt")]]))


@router.callback_query(F.data.startswith("sva:flr:"))
async def filter_reply_ask(call: CallbackQuery, sv: Svyaz, state: FSMContext) -> None:
    if not sv.is_owner(call.from_user.id):
        await call.answer()
        return
    rule_id = int(call.data.split(":")[2])
    await state.set_state(Form.filter_reply)
    await state.update_data(edit=rule_id)
    await show(call, "✏️ <b>Новый автоответ</b>\nОтправьте текст: форматирование и премиум-эмодзи сохранятся.",
               kit.kb([[kit.btn("🔇 Без ответа", "sva:flnone")], [kit.btn("Отмена", f"sva:fl:{rule_id}")]]))


async def save_filter_reply(sv: Svyaz, state: FSMContext, reply: str | None) -> int | None:
    data = await state.get_data()
    await state.clear()
    if data.get("edit"):
        await sv.db.run("UPDATE filters SET reply = ? WHERE id = ?", reply, data["edit"])
        return data["edit"]
    if data.get("words"):
        return await sv.db.run("INSERT INTO filters(words, reply, created_at) VALUES (?, ?, ?)",
                               json.dumps(data["words"], ensure_ascii=False), reply, now())
    return None


@router.message(Form.filter_reply, F.chat.type == "private", F.text)
async def filter_reply_input(message: Message, sv: Svyaz, state: FSMContext) -> None:
    if not sv.is_owner(message.from_user.id):
        return
    rule_id = await save_filter_reply(sv, state, message.html_text[:3500])
    if rule_id:
        text, markup = await filter_card(sv, rule_id)
        await message.answer("✅ <b>Фильтр сохранён.</b>\n\n" + text, reply_markup=markup)


@router.callback_query(F.data == "sva:flnone", Form.filter_reply)
async def filter_no_reply(call: CallbackQuery, sv: Svyaz, state: FSMContext) -> None:
    if not sv.is_owner(call.from_user.id):
        await call.answer()
        return
    rule_id = await save_filter_reply(sv, state, None)
    if rule_id:
        text, markup = await filter_card(sv, rule_id)
        await show(call, "✅ <b>Фильтр сохранён.</b>\n\n" + text, markup)
    else:
        await call.answer()


@router.callback_query(F.data.startswith("sva:fldel:"))
async def filter_delete(call: CallbackQuery, sv: Svyaz, state: FSMContext) -> None:
    if not sv.is_owner(call.from_user.id):
        await call.answer()
        return
    await sv.db.run("DELETE FROM filters WHERE id = ?", int(call.data.split(":")[2]))
    await call.answer("Фильтр удалён")
    await admin_filters(call, sv, state)


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

    async def known(uid: int) -> bool:  # операторы и клиенты, которые уже писали, проверку не проходят
        return await sv.is_operator(uid) or bool(await db.one("SELECT 1 FROM history h JOIN dialogs d ON "
                                                               "d.id = h.dialog_id WHERE d.client_id = ? LIMIT 1", uid))
    captcha = await Captcha(back="sva:home", exempt=known).setup(dp)
    await kit.ready(bot)
    try:
        await bot.set_my_commands([BotCommand(command="start", description="Начать")])
    except Exception:
        pass
    for uid in set(kit.owner_ids()) | set(await sv.operator_ids()):
        await set_commands(sv, uid)
    if not await sv.operators() and not kit.bound_chat_id():
        await sv.remind_no_operators()
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
