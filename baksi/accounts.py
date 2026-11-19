"""Аккаунты: вход по логину и паролю, ключ восстановления, запасной Telegram, перенос на новый Telegram.

Аккаунт = Telegram ID (на нём держатся боты, оплаты, баланс, права в /admin ботов). Логин и пароль - второй
способ входа на сайт, а не отдельный аккаунт: при регистрации на сайте Telegram привязывается сразу,
одной кнопкой. Если Telegram потерян, все данные переносятся со старого ID на новый (rekey):
- ключом восстановления или с запасного Telegram - через 24 часа, прежний аккаунт и запасной получают
  уведомление с кнопкой «Это не я, отменить»;
- владельцем платформы вручную из супер-админки - сразу, с записью в журнал.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
from typing import TYPE_CHECKING, Any

from aiogram.types import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup

from .db import now

if TYPE_CHECKING:
    from .app import App

TRANSFER_DELAY = 24 * 3600
TOKEN_TTL = {"su": 1800, "rs": 86400, "mv": 3600, "pw": 1800}  # регистрация, запасной, перенос, пароль
LOGIN_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,32}$")
LOGIN_FAILS = 5          # неверных паролей подряд до паузы
LOGIN_LOCK = 15 * 60
KEY_ATTEMPTS = 5         # попыток ввести ключ в час с одного Telegram
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}

MIGRATION = """
CREATE TABLE IF NOT EXISTS credentials (
    user_id      INTEGER PRIMARY KEY,
    login        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    pass_hash    TEXT NOT NULL,
    fails        INTEGER NOT NULL DEFAULT 0,
    locked_until INTEGER NOT NULL DEFAULT 0,
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS security (
    user_id       INTEGER PRIMARY KEY,
    recovery_hash TEXT UNIQUE,
    recovery_at   INTEGER,
    reserve_id    INTEGER,
    reserve_name  TEXT,
    reserve_at    INTEGER,
    nudged_at     INTEGER,
    reminded_at   INTEGER
);
CREATE INDEX IF NOT EXISTS security_reserve ON security(reserve_id);
CREATE TABLE IF NOT EXISTS auth_tokens (
    token_hash  TEXT PRIMARY KEY,
    purpose     TEXT NOT NULL,
    user_id     INTEGER,
    data        TEXT NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL,
    result_user INTEGER
);
CREATE TABLE IF NOT EXISTS transfers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    old_id      INTEGER NOT NULL,
    new_id      INTEGER NOT NULL,
    method      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  INTEGER NOT NULL,
    execute_at  INTEGER NOT NULL,
    done_at     INTEGER,
    note        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS transfers_due ON transfers(status, execute_at);
CREATE TABLE IF NOT EXISTS key_attempts (
    user_id INTEGER NOT NULL,
    at      INTEGER NOT NULL
);
"""

METHODS = {"key": "ключ восстановления", "reserve": "запасной Telegram", "password": "вход по паролю на сайте",
           "admin": "поддержка (вручную)"}


class AccountError(Exception):
    pass


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), **SCRYPT)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def check_password_strength(password: str) -> None:
    if len(password) < 8:
        raise AccountError("Пароль должен быть не короче 8 символов")
    if len(password) > 128:
        raise AccountError("Слишком длинный пароль")
    if password.isdigit() or password.lower() in ("password", "qwertyui", "12345678", "baksicloud"):
        raise AccountError("Слишком простой пароль: добавьте буквы и цифры")


def new_recovery_key() -> str:
    raw = base64.b32encode(secrets.token_bytes(10)).decode().rstrip("=")  # 16 символов, 80 бит
    return "BAKSI-" + "-".join(raw[i:i + 4] for i in range(0, 16, 4))


def normalize_key(text: str) -> str:
    clean = re.sub(r"[^A-Z0-9]", "", (text or "").upper())
    if clean.startswith("BAKSI"):
        clean = clean[5:]
    return clean


def kb(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows if r])


def fmt_time(ts: int) -> str:
    return time.strftime("%d.%m.%Y %H:%M", time.localtime(ts))


class Accounts:
    def __init__(self, app: "App") -> None:
        self.app = app
        self.db = app.db

    async def migrate(self) -> None:
        assert self.db.conn
        await self.db.conn.executescript(MIGRATION)
        await self.db.conn.commit()

    # --- одноразовые ссылки ------------------------------------------------------------
    async def new_token(self, purpose: str, user_id: int | None, data: str = "") -> str:
        token = secrets.token_urlsafe(16)
        await self.db.run("INSERT INTO auth_tokens(token_hash, purpose, user_id, data, created_at, expires_at) "
                          "VALUES (?, ?, ?, ?, ?, ?)", self.app.vault.hash(token), purpose, user_id, data, now(),
                          now() + TOKEN_TTL[purpose])
        await self.db.run("DELETE FROM auth_tokens WHERE expires_at < ?", now() - 86400)
        return token

    async def get_token(self, purpose: str, token: str) -> Any:
        return await self.db.one("SELECT * FROM auth_tokens WHERE token_hash = ? AND purpose = ? AND expires_at > ?",
                                 self.app.vault.hash(token), purpose, now())

    def deep_link(self, prefix: str, token: str) -> str:
        return f"https://t.me/{self.app.hub_username}?start={prefix}_{token}" if self.app.hub_username else ""

    # --- логин и пароль ---------------------------------------------------------------
    async def credentials(self, user_id: int) -> Any:
        return await self.db.one("SELECT * FROM credentials WHERE user_id = ?", user_id)

    async def check_login_free(self, login: str, user_id: int | None = None) -> str:
        login = (login or "").strip()
        if not LOGIN_RE.match(login):
            raise AccountError("Логин: 3–32 символа, латиница, цифры, точка, дефис или подчёркивание")
        row = await self.db.one("SELECT user_id FROM credentials WHERE login = ?", login)
        if row and row["user_id"] != user_id:
            raise AccountError("Этот логин уже занят")
        return login

    async def signup_start(self, login: str, password: str) -> tuple[str, str]:
        """Шаг 1 регистрации на сайте. Возвращает (токен для проверки статуса, ссылку в Telegram)."""
        login = await self.check_login_free(login)
        check_password_strength(password)
        if not self.app.hub_username:
            raise AccountError("Регистрация временно недоступна: бот не запущен")
        token = await self.new_token("su", None, login + "\n" + hash_password(password))
        return token, self.deep_link("su", token)

    async def signup_confirm(self, token: str, tg_id: int) -> str:
        """Шаг 2: человек открыл ссылку в Telegram. Логин и пароль привязываются к его аккаунту."""
        row = await self.get_token("su", token)
        if not row or row["result_user"]:
            raise AccountError("Ссылка регистрации устарела. Начните регистрацию на сайте заново.")
        login, pass_hash = row["data"].split("\n", 1)
        existing = await self.credentials(tg_id)
        if existing:
            raise AccountError(f"У этого Telegram уже есть вход на сайт с логином <b>{existing['login']}</b>. "
                               "Войдите с ним или смените пароль в разделе «🔐 Безопасность».")
        await self.check_login_free(login, tg_id)
        await self.db.run("INSERT INTO credentials(user_id, login, pass_hash, created_at, updated_at) "
                          "VALUES (?, ?, ?, ?, ?)", tg_id, login, pass_hash, now(), now())
        await self.db.run("UPDATE auth_tokens SET result_user = ? WHERE token_hash = ?", tg_id, row["token_hash"])
        await self.db.audit(tg_id, "account.signup", login)
        return login

    async def signup_status(self, token: str) -> int | None:
        """Сайт ждёт подтверждения в Telegram; вход выдаётся один раз."""
        row = await self.get_token("su", token)
        if not row or not row["result_user"]:
            return None
        await self.db.run("DELETE FROM auth_tokens WHERE token_hash = ?", row["token_hash"])
        return row["result_user"]

    async def login(self, login: str, password: str) -> int:
        row = await self.db.one("SELECT * FROM credentials WHERE login = ?", (login or "").strip())
        if not row:
            verify_password(password, hash_password("x"))  # одинаковое время ответа для существующих и нет
            raise AccountError("Неверный логин или пароль")
        if row["locked_until"] > now():
            minutes = max(1, (row["locked_until"] - now()) // 60)
            raise AccountError(f"Слишком много попыток. Попробуйте через {minutes} мин.")
        if not verify_password(password, row["pass_hash"]):
            fails = row["fails"] + 1
            locked = now() + LOGIN_LOCK if fails >= LOGIN_FAILS else 0
            await self.db.run("UPDATE credentials SET fails = ?, locked_until = ? WHERE user_id = ?",
                              0 if locked else fails, locked, row["user_id"])
            if locked:
                self.app.notifier.send(row["user_id"], "⚠️ Кто-то много раз неверно ввёл пароль от вашего аккаунта "
                                                       "на сайте. Вход по паролю закрыт на 15 минут. Если это были не вы, "
                                                       "смените пароль: 🔐 Безопасность → Пароль.")
            raise AccountError("Неверный логин или пароль")
        await self.db.run("UPDATE credentials SET fails = 0, locked_until = 0 WHERE user_id = ?", row["user_id"])
        return row["user_id"]

    async def set_password(self, user_id: int, login: str | None, password: str) -> str:
        """Задать или сменить логин и пароль (человек уже вошёл через Telegram или сайт)."""
        current = await self.credentials(user_id)
        login = await self.check_login_free(login or (current["login"] if current else ""), user_id)
        check_password_strength(password)
        if current:
            await self.db.run("UPDATE credentials SET login = ?, pass_hash = ?, fails = 0, locked_until = 0, "
                              "updated_at = ? WHERE user_id = ?", login, hash_password(password), now(), user_id)
        else:
            await self.db.run("INSERT INTO credentials(user_id, login, pass_hash, created_at, updated_at) "
                              "VALUES (?, ?, ?, ?, ?)", user_id, login, hash_password(password), now(), now())
        await self.db.audit(user_id, "account.password", login)
        self.app.notifier.send(user_id, f"🔐 Пароль для входа на сайт {'изменён' if current else 'задан'}. "
                                        f"Логин: <b>{login}</b>. Если это были не вы, срочно смените пароль.")
        return login

    # --- ключ восстановления и запасной Telegram ----------------------------------------
    async def security(self, user_id: int) -> dict:
        row = await self.db.one("SELECT * FROM security WHERE user_id = ?", user_id)
        cred = await self.credentials(user_id)
        pending = await self.pending_transfer(user_id)
        return {"has_key": bool(row and row["recovery_hash"]), "key_at": row["recovery_at"] if row else None,
                "reserve_id": row["reserve_id"] if row else None,
                "reserve_name": row["reserve_name"] if row else None,
                "login": cred["login"] if cred else None,
                "transfer": dict(pending) if pending else None}

    async def issue_recovery_key(self, user_id: int) -> str:
        key = new_recovery_key()
        await self.db.run("INSERT INTO security(user_id, recovery_hash, recovery_at) VALUES (?, ?, ?) "
                          "ON CONFLICT(user_id) DO UPDATE SET recovery_hash = excluded.recovery_hash, "
                          "recovery_at = excluded.recovery_at", user_id, self.key_hash(key), now())
        await self.db.audit(user_id, "account.recovery_key")
        return key

    def key_hash(self, key: str) -> str:
        return self.app.vault.hash("recovery:" + normalize_key(key))

    async def reserve_link(self, user_id: int) -> str:
        return self.deep_link("rs", await self.new_token("rs", user_id))

    async def reserve_confirm(self, token: str, tg_id: int, name: str) -> int:
        row = await self.get_token("rs", token)
        if not row:
            raise AccountError("Ссылка устарела. Попросите владельца аккаунта создать новую.")
        owner = row["user_id"]
        if owner == tg_id:
            raise AccountError("Это тот же аккаунт. Откройте ссылку с другого, запасного Telegram.")
        await self.db.run("INSERT INTO security(user_id, reserve_id, reserve_name, reserve_at) VALUES (?, ?, ?, ?) "
                          "ON CONFLICT(user_id) DO UPDATE SET reserve_id = excluded.reserve_id, "
                          "reserve_name = excluded.reserve_name, reserve_at = excluded.reserve_at",
                          owner, tg_id, name[:64], now())
        await self.db.run("DELETE FROM auth_tokens WHERE token_hash = ?", row["token_hash"])
        await self.db.audit(owner, "account.reserve", str(tg_id))
        self.app.notifier.send(owner, f"🛡 Запасной Telegram подключён: <b>{name}</b>. Если потеряете этот аккаунт, "
                                      "с запасного можно будет вернуть доступ к ботам и балансу.")
        return owner

    async def remove_reserve(self, user_id: int) -> None:
        await self.db.run("UPDATE security SET reserve_id = NULL, reserve_name = NULL, reserve_at = NULL "
                          "WHERE user_id = ?", user_id)

    async def accounts_reserved_by(self, tg_id: int) -> list[Any]:
        return await self.db.all("SELECT s.user_id, u.username, u.first_name FROM security s "
                                 "JOIN users u ON u.id = s.user_id WHERE s.reserve_id = ?", tg_id)

    # --- перенос на новый Telegram -------------------------------------------------------
    async def pending_transfer(self, user_id: int) -> Any:
        return await self.db.one("SELECT * FROM transfers WHERE status = 'pending' AND (old_id = ? OR new_id = ?) "
                                 "ORDER BY id DESC LIMIT 1", user_id, user_id)

    async def check_target_free(self, new_id: int) -> None:
        """Новый Telegram не должен владеть чем-то своим: иначе данные смешаются."""
        user = await self.db.one("SELECT * FROM users WHERE id = ?", new_id)
        if not user:
            return
        bots = await self.db.value("SELECT COUNT(*) FROM instances WHERE user_id = ? AND deleted_at IS NULL", new_id,
                                   default=0)
        if bots or user["balance"]:
            raise AccountError("На этом Telegram уже есть свои боты или баланс. Перенести аккаунт можно только на "
                               "Telegram без своих ботов. Напишите в поддержку, если нужно объединить аккаунты.")

    async def recover_with_key(self, new_id: int, key: str) -> Any:
        recent = await self.db.value("SELECT COUNT(*) FROM key_attempts WHERE user_id = ? AND at > ?", new_id,
                                     now() - 3600, default=0)
        if recent >= KEY_ATTEMPTS:
            raise AccountError("Слишком много попыток. Попробуйте через час.")
        await self.db.run("INSERT INTO key_attempts(user_id, at) VALUES (?, ?)", new_id, now())
        await self.db.run("DELETE FROM key_attempts WHERE at < ?", now() - 86400)
        if len(normalize_key(key)) != 16:
            raise AccountError("Это не похоже на ключ восстановления. Он выглядит так: BAKSI-XXXX-XXXX-XXXX-XXXX")
        row = await self.db.one("SELECT user_id FROM security WHERE recovery_hash = ?", self.key_hash(key))
        if not row:
            raise AccountError("Ключ не подошёл. Проверьте, что ввели его полностью.")
        return await self.request_transfer(row["user_id"], new_id, "key")

    async def request_transfer(self, old_id: int, new_id: int, method: str) -> Any:
        if old_id == new_id:
            raise AccountError("Аккаунт уже на этом Telegram.")
        if await self.pending_transfer(old_id):
            raise AccountError("Перенос этого аккаунта уже запрошен. Дождитесь его или отмены.")
        await self.check_target_free(new_id)
        tid = await self.db.run("INSERT INTO transfers(old_id, new_id, method, created_at, execute_at) "
                                "VALUES (?, ?, ?, ?, ?)", old_id, new_id, method, now(), now() + TRANSFER_DELAY)
        await self.db.audit(new_id, "account.transfer_request", f"{old_id}->{new_id} {method}")
        row = await self.db.one("SELECT * FROM transfers WHERE id = ?", tid)
        new_user = await self.db.one("SELECT username, first_name FROM users WHERE id = ?", new_id)
        who = (f"@{new_user['username']}" if new_user and new_user["username"] else
               (new_user["first_name"] if new_user else "")) or str(new_id)
        when = fmt_time(row["execute_at"])
        warn = (f"⚠️ <b>Ваш аккаунт Baksi Cloud переносят на другой Telegram</b>\n\n"
                f"Новый Telegram: <b>{who}</b> (ID <code>{new_id}</code>)\nСпособ: {METHODS.get(method, method)}\n"
                f"Перенос состоится: <b>{when}</b>, через 24 часа.\n\n"
                "Если это вы - ничего делать не нужно. Если не вы, нажмите кнопку ниже: перенос отменится"
                + (", а ключ восстановления сменится." if method == "key" else "."))
        cancel = kb([InlineKeyboardButton(text="🚫 Это не я, отменить перенос", callback_data=f"trc:{tid}",
                                          style="danger")])
        self.app.notifier.send(old_id, warn, cancel)
        sec = await self.db.one("SELECT reserve_id FROM security WHERE user_id = ?", old_id)
        if sec and sec["reserve_id"] and sec["reserve_id"] != new_id:
            self.app.notifier.send(sec["reserve_id"], "Вы запасной Telegram этого аккаунта.\n\n" + warn, cancel)
        return row

    async def cancel_transfer(self, transfer_id: int, by: int) -> Any:
        row = await self.db.one("SELECT * FROM transfers WHERE id = ?", transfer_id)
        if not row or row["status"] != "pending":
            raise AccountError("Этот перенос уже не ждёт: он выполнен или отменён.")
        sec = await self.db.one("SELECT reserve_id FROM security WHERE user_id = ?", row["old_id"])
        allowed = {row["old_id"], row["new_id"]} | ({sec["reserve_id"]} if sec and sec["reserve_id"] else set())
        if by not in allowed and not self.app.is_owner(by):
            raise AccountError("Отменить перенос может только владелец аккаунта.")
        await self.db.run("UPDATE transfers SET status = 'cancelled', done_at = ?, note = ? WHERE id = ?",
                          now(), f"отменил {by}", transfer_id)
        await self.db.audit(by, "account.transfer_cancel", str(transfer_id))
        if by != row["new_id"]:
            self.app.notifier.send(row["new_id"], "🚫 Перенос аккаунта отменён его владельцем.")
        return row

    async def nudge(self) -> int:
        """Один раз предлагаем защитить аккаунт тем, у кого появился первый бот; через 3 дня - напоминание
        про запасной Telegram, если его так и не указали. Без навязчивости: два сообщения за всё время."""
        sent = 0
        rows = await self.db.all(
            "SELECT DISTINCT i.user_id FROM instances i LEFT JOIN security s ON s.user_id = i.user_id "
            "WHERE i.deleted_at IS NULL AND s.nudged_at IS NULL LIMIT 50")
        for r in rows:
            uid = r["user_id"]
            sec = await self.security(uid)
            key = None if sec["has_key"] else await self.issue_recovery_key(uid)
            await self.db.run("INSERT INTO security(user_id, nudged_at) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE "
                              "SET nudged_at = excluded.nudged_at", uid, now())
            text = ("🛡 <b>Защитите доступ к своим ботам</b>\n\n"
                    "Если Telegram-аккаунт заблокируют или удалят, без защиты вернуть боты и баланс будет сложно.\n\n")
            if key:
                text += (f"🔑 Ваш ключ восстановления:\n<code>{key}</code>\n"
                         "Сохраните его вне Telegram (менеджер паролей, бумага) и удалите это сообщение.\n\n")
            text += ("👥 Ещё надёжнее - указать запасной Telegram: второй свой аккаунт или аккаунт человека, которому "
                     "доверяете. Это займёт минуту.")
            rows_kb = [[InlineKeyboardButton(text="👥 Указать запасной Telegram", callback_data="sec:rs", style="primary")],
                       [InlineKeyboardButton(text="🔐 Безопасность", callback_data="sec")]]
            if key:
                rows_kb.insert(0, [InlineKeyboardButton(text="📋 Скопировать ключ",
                                                        copy_text=CopyTextButton(text=key))])
            self.app.notifier.send(uid, text, InlineKeyboardMarkup(inline_keyboard=rows_kb))
            sent += 1
        later = await self.db.all("SELECT user_id FROM security WHERE nudged_at < ? AND reminded_at IS NULL "
                                  "AND reserve_id IS NULL LIMIT 50", now() - 3 * 86400)
        for r in later:
            await self.db.run("UPDATE security SET reminded_at = ? WHERE user_id = ?", now(), r["user_id"])
            self.app.notifier.send(r["user_id"], (
                "👥 <b>Запасной Telegram всё ещё не указан</b>\n\nЕсли основной аккаунт пропадёт, запасной поможет "
                "вернуть боты и баланс за 24 часа. Напоминаю один раз, дальше решать вам."),
                kb([InlineKeyboardButton(text="👥 Указать запасной Telegram", callback_data="sec:rs", style="primary")]))
            sent += 1
        return sent

    async def execute_due(self) -> int:
        rows = await self.db.all("SELECT * FROM transfers WHERE status = 'pending' AND execute_at <= ?", now())
        for row in rows:
            try:
                await self.rekey(row["old_id"], row["new_id"])
                await self.db.run("UPDATE transfers SET status = 'done', done_at = ? WHERE id = ?", now(), row["id"])
                await self.after_transfer(row["old_id"], row["new_id"], row["method"])
            except AccountError as exc:
                await self.db.run("UPDATE transfers SET status = 'failed', done_at = ?, note = ? WHERE id = ?",
                                  now(), str(exc), row["id"])
                self.app.notifier.send(row["new_id"], f"⚠️ Перенос аккаунта не выполнен: {exc}")
        return len(rows)

    async def transfer_now(self, old_id: int, new_id: int, by: int) -> None:
        """Ручной перенос владельцем платформы (после проверки человека)."""
        await self.check_target_free(new_id)
        await self.rekey(old_id, new_id)
        await self.db.run("INSERT INTO transfers(old_id, new_id, method, status, created_at, execute_at, done_at, note) "
                          "VALUES (?, ?, 'admin', 'done', ?, ?, ?, ?)", old_id, new_id, now(), now(), now(),
                          f"вручную: {by}")
        await self.db.audit(by, "account.transfer_admin", f"{old_id}->{new_id}")
        await self.after_transfer(old_id, new_id, "admin")

    async def rekey(self, old_id: int, new_id: int) -> None:
        """Переносит всё со старого Telegram ID на новый одной транзакцией."""
        if not await self.db.one("SELECT 1 FROM users WHERE id = ?", old_id):
            raise AccountError("Аккаунт не найден")
        await self.check_target_free(new_id)
        async with self.db.tx() as conn:
            await conn.execute("PRAGMA defer_foreign_keys = ON")  # ссылки на users проверятся в конце транзакции
            target = await (await conn.execute("SELECT username, first_name FROM users WHERE id = ?",
                                               (new_id,))).fetchone()
            await conn.execute("DELETE FROM sessions WHERE user_id IN (?, ?)", (old_id, new_id))
            await conn.execute("DELETE FROM login_tokens WHERE user_id IN (?, ?)", (old_id, new_id))
            await conn.execute("DELETE FROM trials WHERE user_id = ?", (new_id,))  # остаются пробные старого
            await conn.execute("DELETE FROM credentials WHERE user_id = ?", (new_id,))
            await conn.execute("DELETE FROM security WHERE user_id = ?", (new_id,))
            await conn.execute("DELETE FROM users WHERE id = ?", (new_id,))
            await conn.execute("UPDATE users SET id = ? WHERE id = ?", (new_id, old_id))
            if target:
                await conn.execute("UPDATE users SET username = ?, first_name = ? WHERE id = ?",
                                   (target[0], target[1], new_id))
            for table, col in (("users", "ref_by"), ("instances", "user_id"), ("trials", "user_id"),
                               ("ledger", "user_id"), ("invoices", "user_id"), ("promo_uses", "user_id"),
                               ("credentials", "user_id"), ("security", "user_id")):
                await conn.execute(f"UPDATE {table} SET {col} = ? WHERE {col} = ?", (new_id, old_id))
            await conn.execute("UPDATE security SET reserve_id = NULL, reserve_name = NULL WHERE user_id = ? "
                               "AND reserve_id = ?", (new_id, new_id))
            await conn.execute("UPDATE users SET hub_blocked = 0 WHERE id = ?", (new_id,))

    async def after_transfer(self, old_id: int, new_id: int, method: str) -> None:
        bots = await self.db.all("SELECT id FROM instances WHERE user_id = ? AND deleted_at IS NULL", new_id)
        for inst in bots:  # права владельца в /admin ботов - у нового Telegram, перезапуск подхватывает сразу
            await self.app.sup.restart(inst["id"])
        self.app.sup.poke()
        user = await self.db.one("SELECT balance FROM users WHERE id = ?", new_id)
        key = await self.issue_recovery_key(new_id)  # старый ключ мог попасть в чужие руки
        from .billing import money
        self.app.notifier.send(new_id, (
            "✅ <b>Аккаунт перенесён на этот Telegram</b>\n\n"
            f"Ботов: <b>{len(bots)}</b> · баланс: <b>{money(user['balance'] if user else 0)}</b>\n"
            "Боты перезапущены, в их /admin теперь владелец - вы.\n\n"
            f"Новый ключ восстановления (старый больше не действует):\n<code>{key}</code>\n"
            "Сохраните его в надёжном месте, не в этом чате."))
        if method != "admin":
            self.app.notifier.send(old_id, "Аккаунт Baksi Cloud перенесён на другой Telegram. Этот аккаунт больше "
                                           "не управляет ботами.")
