"""Капча для новых пользователей, одна на все модули Baksi Cloud.

Владелец бота включает её в своей админке (кнопка «🛡 Капча» или команда /captcha) и выбирает вид:
кнопка «Я не робот», пример с числами или «найди картинку». Проверяются только личные сообщения тех,
кто пишет боту впервые: владелец, сотрудники (exempt) и уже прошедшие не проверяются. То, что человек
успел отправить до проверки (например, /start со ссылкой-приглашением), после прохождения
обрабатывается как обычно, ничего не теряется.

Подключение в модуле:
    captcha = Captcha(back="adm:home", exempt=async_fn_or_None)
    await captcha.setup(dp)          # до start_polling
    ...
    await captcha.close()            # в finally
"""
from __future__ import annotations

import inspect
import logging
import random
import secrets
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiosqlite
from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery, Update

import baksi_kit as kit

log = logging.getLogger("baksi.captcha")

MODES = {
    "off": "Выключена",
    "button": "Кнопка «Я не робот»",
    "math": "Пример с числами",
    "emoji": "Найти картинку",
}
MODE_HINTS = {
    "button": "одна кнопка, самый простой вариант",
    "math": "«Сколько будет 7 + 5?» и четыре ответа",
    "emoji": "«Нажмите на яблоко» среди шести картинок",
}
EMOJI = [("🍎", "яблоко"), ("🚗", "машину"), ("🐶", "собаку"), ("🌙", "луну"), ("⚽️", "мяч"), ("🎸", "гитару"),
         ("🍕", "пиццу"), ("✈️", "самолёт"), ("🐱", "кошку"), ("🌲", "ёлку"), ("🔑", "ключ"), ("☂️", "зонт"),
         ("🍌", "банан"), ("🚲", "велосипед"), ("🐟", "рыбу"), ("⏰", "будильник"), ("🎁", "подарок"), ("🍦", "мороженое")]
MAX_FAILS = 3
BLOCK_SECONDS = 300
CHALLENGE_TTL = 600
RESEND_AFTER = 60
MAX_PENDING_UPDATES = 5

Exempt = Callable[[int], Awaitable[bool] | bool]


def make_challenge(mode: str) -> tuple[str, list[str], int, int]:
    """Текст, варианты, номер верного, сколько кнопок в ряду."""
    if mode == "math":
        if random.random() < 0.5:
            a, b = random.randint(2, 9), random.randint(2, 9)
            question, answer = f"{a} + {b}", a + b
        else:
            a, b = random.randint(10, 19), random.randint(2, 9)
            question, answer = f"{a} − {b}", a - b
        options = {answer}
        while len(options) < 4:
            options.add(max(0, answer + random.choice([-3, -2, -1, 1, 2, 3, 10])))
        options = random.sample(sorted(options), 4)
        return (f"🛡 <b>Проверка, что вы не робот</b>\nСколько будет <b>{question}</b>?",
                [str(o) for o in options], options.index(answer), 4)
    if mode == "emoji":
        picks = random.sample(EMOJI, 6)
        target = random.randrange(6)
        return (f"🛡 <b>Проверка, что вы не робот</b>\nНажмите на <b>{picks[target][1]}</b>",
                [p[0] for p in picks], target, 3)
    return ("🛡 <b>Проверка, что вы не робот</b>\nНажмите кнопку ниже, чтобы продолжить.", ["✅ Я не робот"], 0, 1)


class Captcha(BaseMiddleware):
    def __init__(self, back: str | None = None, exempt: Exempt | None = None,
                 db_path: Path | str | None = None) -> None:
        self.back = back              # callback «Назад» в админку модуля; None - кнопка «Закрыть»
        self.exempt = exempt          # сотрудники модуля (операторы, админы) - без проверки
        self.db_path = Path(db_path) if db_path else kit.data_dir() / "captcha.db"
        self.conn: aiosqlite.Connection | None = None
        self.dp = None
        self.mode = "off"
        self.passed: set[int] = set()
        self.fails_total = 0
        self.pending: dict[int, dict] = {}
        self.blocked: dict[int, float] = {}

    # --- хранение ---------------------------------------------------------------------
    async def setup(self, dp) -> "Captcha":
        self.dp = dp
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.db_path)
        await self.conn.executescript(
            "PRAGMA journal_mode=WAL;"
            "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);"
            "CREATE TABLE IF NOT EXISTS passed (user_id INTEGER PRIMARY KEY, at INTEGER NOT NULL);")
        await self.conn.commit()
        async with self.conn.execute("SELECT key, value FROM kv") as cur:
            kv = {k: v for k, v in await cur.fetchall()}
        self.mode = kv.get("mode", "off") if kv.get("mode") in MODES else "off"
        self.fails_total = int(kv.get("fails", 0) or 0)
        async with self.conn.execute("SELECT user_id FROM passed") as cur:
            self.passed = {r[0] for r in await cur.fetchall()}
        dp.update.outer_middleware(self)
        return self

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()
            self.conn = None

    async def _kv(self, key: str, value: Any) -> None:
        await self.conn.execute("INSERT INTO kv(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
                                "value = excluded.value", (key, str(value)))
        await self.conn.commit()

    async def set_mode(self, mode: str) -> None:
        self.mode = mode if mode in MODES else "off"
        await self._kv("mode", self.mode)
        if self.mode == "off":
            self.pending.clear()
            self.blocked.clear()

    async def _pass(self, uid: int) -> None:
        self.passed.add(uid)
        await self.conn.execute("INSERT OR IGNORE INTO passed(user_id, at) VALUES (?, ?)", (uid, int(time.time())))
        await self.conn.commit()

    async def _is_exempt(self, uid: int) -> bool:
        if uid in kit.owner_ids():
            return True
        if not self.exempt:
            return False
        result = self.exempt(uid)
        return bool(await result if inspect.isawaitable(result) else result)

    # --- middleware -------------------------------------------------------------------
    async def __call__(self, handler, event: Update, data: dict) -> Any:
        message, call = event.message, event.callback_query
        bot = data.get("bot")
        kit.touch()  # любое входящее событие = бот живой (капча подключена во всех модулях)
        if call and (call.data or "").startswith("kcap:"):
            await self.on_callback(call, bot)
            return None
        if message and message.chat.type == "private" and message.from_user \
                and (message.text or "").split("@")[0].strip() == "/captcha":
            if message.from_user.id in kit.owner_ids():
                text, markup = self.menu()
                await message.answer(text, reply_markup=markup, parse_mode="HTML")
                return None
        if self.mode == "off":
            return await handler(event, data)
        user = message.from_user if message else call.from_user if call else None
        chat_type = message.chat.type if message else (call.message.chat.type if call and call.message else "private")
        if not user or user.is_bot or chat_type != "private" or user.id in self.passed:
            return await handler(event, data)
        if await self._is_exempt(user.id):
            return await handler(event, data)
        await self.hold(event, user.id, bot, call)
        return None

    async def hold(self, event: Update, uid: int, bot, call: CallbackQuery | None) -> None:
        """Новый пользователь: запоминаем, что он прислал, и показываем проверку."""
        now = time.time()
        if self.blocked.get(uid, 0) > now:
            if call:
                await self._answer(call, "Слишком много ошибок. Попробуйте через несколько минут.", True)
            return
        p = self.pending.get(uid)
        if p and now - p["created"] > CHALLENGE_TTL:
            p = None
        if call:
            await self._answer(call, "Сначала пройдите проверку ниже 👇", True)
        if p is None:
            p = self.pending[uid] = {"created": now, "sent": 0.0, "fails": 0, "updates": [], "msg": None}
        if event.message and len(p["updates"]) < MAX_PENDING_UPDATES:
            p["updates"].append(event)
        if p["msg"] is None or now - p["sent"] >= RESEND_AFTER:
            p["sent"] = now
            p["msg"] = -1  # уже отправляем: параллельные сообщения (альбом) не шлют вторую проверку
            await self.send_challenge(bot, uid, p)

    async def send_challenge(self, bot, uid: int, p: dict, prefix: str = "", edit: int | None = None) -> None:
        text, options, answer, cols = make_challenge(self.mode)
        token = secrets.token_hex(3)
        p.update(answer=answer, token=token)
        buttons = [kit.btn(o, f"kcap:a:{token}:{i}") for i, o in enumerate(options)]
        markup = kit.kb(kit.chunk(buttons, cols))
        try:
            if edit:
                await bot.edit_message_text(prefix + text, chat_id=uid, message_id=edit, reply_markup=markup,
                                            parse_mode="HTML")
                p["msg"] = edit
            else:
                sent = await bot.send_message(uid, prefix + text, reply_markup=markup, parse_mode="HTML")
                p["msg"] = sent.message_id
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            log.info("Капча не отправлена %s: %s", uid, exc)

    # --- кнопки -----------------------------------------------------------------------
    async def on_callback(self, call: CallbackQuery, bot) -> None:
        parts = (call.data or "").split(":")
        uid = call.from_user.id
        kind = parts[1] if len(parts) > 1 else ""
        if kind == "a" and len(parts) == 4:
            await self.on_answer(call, bot, parts[2], parts[3])
            return
        if kind == "x":
            await self._answer(call, "Так выглядит проверка для новых пользователей")
            return
        if uid not in kit.owner_ids():
            await self._answer(call)
            return
        if kind == "m" and len(parts) == 3:
            await self.set_mode(parts[2])
            await self._answer(call, f"Капча: {MODES[self.mode].lower()}")
        elif kind == "p":
            text, options, _, cols = make_challenge(self.mode if self.mode != "off" else "button")
            await call.message.answer("👁 <b>Так увидит проверку новый пользователь</b> (кнопки здесь не работают):\n\n"
                                      + text, reply_markup=kit.kb(kit.chunk([kit.btn(o, "kcap:x") for o in options],
                                                                            cols)), parse_mode="HTML")
            await self._answer(call)
            return
        elif kind == "close":
            try:
                await call.message.delete()
            except TelegramBadRequest:
                pass
            await self._answer(call)
            return
        else:
            await self._answer(call)
        text, markup = self.menu()
        try:
            await call.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        except TelegramBadRequest:
            pass

    async def on_answer(self, call: CallbackQuery, bot, token: str, choice: str) -> None:
        uid = call.from_user.id
        p = self.pending.get(uid)
        if uid in self.passed:
            await self._answer(call, "Проверка уже пройдена ✅")
            return
        if not p or p.get("token") != token or not choice.isdigit():
            await self._answer(call, "Эта проверка устарела, вот новая")
            if self.mode != "off":
                p = self.pending.setdefault(uid, {"created": time.time(), "sent": 0.0, "fails": 0, "updates": [],
                                                  "msg": None})
                p["sent"] = time.time()
                await self.send_challenge(bot, uid, p)
            return
        if int(choice) == p["answer"]:
            self.pending.pop(uid, None)
            await self._pass(uid)
            await self._answer(call, "Готово ✅")
            try:
                await call.message.edit_text("✅ Проверка пройдена")
            except TelegramBadRequest:
                pass
            for upd in p["updates"]:  # то, что человек прислал до проверки
                try:
                    await self.dp.feed_update(bot, upd)
                except Exception:
                    log.exception("Не удалось обработать отложенное сообщение")
            return
        p["fails"] += 1
        self.fails_total += 1
        await self._kv("fails", self.fails_total)
        if p["fails"] >= MAX_FAILS:
            self.pending.pop(uid, None)
            self.blocked[uid] = time.time() + BLOCK_SECONDS
            await self._answer(call, "Неверно")
            try:
                await call.message.edit_text(f"❌ Неверно {MAX_FAILS} раза. Попробуйте через {BLOCK_SECONDS // 60} минут: "
                                             "просто напишите боту снова.")
            except TelegramBadRequest:
                pass
            return
        await self._answer(call, "Неверно, попробуйте ещё раз")
        await self.send_challenge(bot, uid, p, prefix="❌ Неверно, ещё попытка.\n\n", edit=call.message.message_id)

    def menu(self) -> tuple[str, Any]:
        lines = ["🛡 <b>Капча</b>",
                 "Проверка для тех, кто пишет боту впервые: отсекает ботов-спамеров. Владелец, сотрудники и те, кто "
                 "уже прошёл проверку, её не видят. То, что человек отправил до проверки, не теряется.\n",
                 f"Сейчас: <b>{MODES[self.mode]}</b>" + (f" ({MODE_HINTS[self.mode]})" if self.mode in MODE_HINTS else ""),
                 f"Прошли проверку: <b>{len(self.passed)}</b> · неверных ответов: <b>{self.fails_total}</b>"]
        rows = [[kit.btn(("✅ " if self.mode == key else "") + title, f"kcap:m:{key}",
                         style="primary" if self.mode == key and key != "off" else None)] for key, title in MODES.items()]
        if self.mode != "off":
            rows.append([kit.btn("👁 Как это выглядит", "kcap:p")])
        rows.append([kit.btn("⬅️ Назад", self.back)] if self.back else [kit.btn("✖️ Закрыть", "kcap:close")])
        return "\n".join(lines), kit.kb(rows)

    @staticmethod
    async def _answer(call: CallbackQuery, text: str | None = None, alert: bool = False) -> None:
        try:
            await call.answer(text, show_alert=alert)
        except TelegramBadRequest:
            pass


def menu_button(text: str = "🛡 Капча"):
    """Кнопка для админки модуля: открывает экран настройки капчи."""
    return kit.btn(text, "kcap:menu")
