"""SQLite (WAL) с миграциями. Одно соединение на процесс, запись под замком: так нет гонок и "database is locked"."""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Iterable

import aiosqlite

# Новые миграции добавлять ТОЛЬКО в конец списка.
MIGRATIONS: list[str] = [
    """
    CREATE TABLE users (
        id          INTEGER PRIMARY KEY,           -- Telegram ID
        username    TEXT,
        first_name  TEXT,
        balance     INTEGER NOT NULL DEFAULT 0,    -- в центах USD
        ref_by      INTEGER,
        ref_earned  INTEGER NOT NULL DEFAULT 0,
        banned      INTEGER NOT NULL DEFAULT 0,
        hub_blocked INTEGER NOT NULL DEFAULT 0,    -- пользователь заблокировал хаб-бота
        created_at  INTEGER NOT NULL,
        last_seen   INTEGER NOT NULL
    );
    CREATE INDEX users_ref ON users(ref_by);

    CREATE TABLE products (
        slug        TEXT PRIMARY KEY,
        enabled     INTEGER NOT NULL DEFAULT 1,
        trial_days  INTEGER NOT NULL DEFAULT 3,
        sort        INTEGER NOT NULL DEFAULT 100
    );

    CREATE TABLE plans (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        product     TEXT NOT NULL REFERENCES products(slug) ON DELETE CASCADE,
        days        INTEGER NOT NULL,              -- 0 = навсегда
        price       INTEGER,                       -- NULL = не продаётся
        enabled     INTEGER NOT NULL DEFAULT 1,
        UNIQUE(product, days)
    );

    CREATE TABLE instances (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id       INTEGER NOT NULL REFERENCES users(id),
        product       TEXT NOT NULL,
        token_enc     TEXT,
        bot_id        INTEGER,
        bot_username  TEXT,
        bot_name      TEXT,
        expires_at    INTEGER,                     -- NULL при forever=1
        forever       INTEGER NOT NULL DEFAULT 0,
        plan_days     INTEGER,                     -- тариф для автопродления
        auto_renew    INTEGER NOT NULL DEFAULT 1,
        trial         INTEGER NOT NULL DEFAULT 0,
        paused        INTEGER NOT NULL DEFAULT 0,  -- остановлен владельцем
        frozen        INTEGER NOT NULL DEFAULT 0,  -- заморожен администрацией
        token_bad     INTEGER NOT NULL DEFAULT 0,  -- Telegram отклонил токен
        bind_code     TEXT NOT NULL,
        settings      TEXT NOT NULL DEFAULT '{}',  -- JSON: настройки модуля из кабинета
        state         TEXT NOT NULL DEFAULT '{}',  -- JSON: что сообщил сам бот (группа и т.п.)
        status        TEXT NOT NULL DEFAULT 'setup',
        status_note   TEXT NOT NULL DEFAULT '',
        created_at    INTEGER NOT NULL,
        expired_at    INTEGER,
        deleted_at    INTEGER
    );
    CREATE INDEX instances_user ON instances(user_id);
    CREATE INDEX instances_exp ON instances(expires_at);
    CREATE UNIQUE INDEX instances_bot ON instances(bot_id) WHERE bot_id IS NOT NULL AND deleted_at IS NULL;

    CREATE TABLE trials (
        user_id  INTEGER NOT NULL,
        product  TEXT NOT NULL,
        used_at  INTEGER NOT NULL,
        PRIMARY KEY (user_id, product)
    );

    CREATE TABLE ledger (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id       INTEGER NOT NULL,
        amount        INTEGER NOT NULL,            -- +пополнение / -списание, центы
        balance_after INTEGER NOT NULL,
        kind          TEXT NOT NULL,               -- topup|purchase|renew|refund|admin|referral|promo
        ref           TEXT NOT NULL DEFAULT '',
        note          TEXT NOT NULL DEFAULT '',
        created_at    INTEGER NOT NULL
    );
    CREATE INDEX ledger_user ON ledger(user_id, id);
    CREATE INDEX ledger_time ON ledger(created_at);

    CREATE TABLE invoices (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL,
        provider    TEXT NOT NULL,
        external_id TEXT,
        amount      INTEGER NOT NULL,
        status      TEXT NOT NULL DEFAULT 'active', -- active|paid|expired
        pay_url     TEXT NOT NULL DEFAULT '',
        intent      TEXT NOT NULL DEFAULT '{}',     -- что купить сразу после оплаты
        created_at  INTEGER NOT NULL,
        paid_at     INTEGER
    );
    CREATE UNIQUE INDEX invoices_ext ON invoices(provider, external_id);
    CREATE INDEX invoices_status ON invoices(status, created_at);

    CREATE TABLE promocodes (
        code        TEXT PRIMARY KEY,
        kind        TEXT NOT NULL,                 -- percent|amount|balance
        value       INTEGER NOT NULL,              -- % или центы
        product     TEXT,                          -- NULL = на всё
        max_uses    INTEGER NOT NULL DEFAULT 0,    -- 0 = без лимита
        used        INTEGER NOT NULL DEFAULT 0,
        expires_at  INTEGER,
        created_at  INTEGER NOT NULL
    );
    CREATE TABLE promo_uses (
        code     TEXT NOT NULL,
        user_id  INTEGER NOT NULL,
        used_at  INTEGER NOT NULL,
        PRIMARY KEY (code, user_id)
    );

    CREATE TABLE sessions (
        token_hash  TEXT PRIMARY KEY,
        user_id     INTEGER NOT NULL,
        created_at  INTEGER NOT NULL,
        expires_at  INTEGER NOT NULL,
        user_agent  TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX sessions_user ON sessions(user_id);

    CREATE TABLE login_tokens (
        token_hash  TEXT PRIMARY KEY,
        user_id     INTEGER NOT NULL,
        expires_at  INTEGER NOT NULL
    );

    CREATE TABLE reminders (
        instance_id INTEGER NOT NULL,
        kind        TEXT NOT NULL,
        expires_at  INTEGER NOT NULL,
        PRIMARY KEY (instance_id, kind, expires_at)
    );

    CREATE TABLE settings (
        key    TEXT PRIMARY KEY,
        value  TEXT NOT NULL
    );

    CREATE TABLE audit (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        actor       INTEGER,
        action      TEXT NOT NULL,
        target      TEXT NOT NULL DEFAULT '',
        details     TEXT NOT NULL DEFAULT '',
        created_at  INTEGER NOT NULL
    );
    CREATE INDEX audit_time ON audit(created_at);
    """,
]


def now() -> int:
    return int(time.time())


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn: aiosqlite.Connection | None = None
        self.lock = asyncio.Lock()

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path, timeout=30)
        self.conn.row_factory = aiosqlite.Row
        for pragma in ("journal_mode=WAL", "synchronous=NORMAL", "foreign_keys=ON",
                       "busy_timeout=10000", "temp_store=MEMORY", "cache_size=-8000"):
            await self.conn.execute(f"PRAGMA {pragma}")
        await self._migrate()

    async def close(self) -> None:
        if self.conn:
            await self.conn.execute("PRAGMA optimize")
            await self.conn.close()
            self.conn = None

    async def _migrate(self) -> None:
        assert self.conn
        version = (await (await self.conn.execute("PRAGMA user_version")).fetchone())[0]
        for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
            await self.conn.executescript(f"BEGIN; {sql}; PRAGMA user_version = {i}; COMMIT;")

    # --- чтение -------------------------------------------------------------
    async def one(self, sql: str, *args: Any) -> aiosqlite.Row | None:
        assert self.conn
        async with self.conn.execute(sql, args) as cur:
            return await cur.fetchone()

    async def all(self, sql: str, *args: Any) -> list[aiosqlite.Row]:
        assert self.conn
        async with self.conn.execute(sql, args) as cur:
            return list(await cur.fetchall())

    async def value(self, sql: str, *args: Any, default: Any = None) -> Any:
        row = await self.one(sql, *args)
        return row[0] if row and row[0] is not None else default

    # --- запись -------------------------------------------------------------
    async def run(self, sql: str, *args: Any) -> int:
        """Одиночная запись. Возвращает lastrowid."""
        assert self.conn
        async with self.lock:
            cur = await self.conn.execute(sql, args)
            await self.conn.commit()
            return cur.lastrowid or 0

    async def many(self, sql: str, rows: Iterable[tuple]) -> None:
        assert self.conn
        async with self.lock:
            await self.conn.executemany(sql, rows)
            await self.conn.commit()

    @asynccontextmanager
    async def tx(self) -> AsyncIterator[aiosqlite.Connection]:
        """Транзакция: всё внутри либо применится целиком, либо откатится."""
        assert self.conn
        async with self.lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                await self.conn.rollback()
                raise
            else:
                await self.conn.commit()

    # --- настройки ----------------------------------------------------------
    async def get_setting(self, key: str, default: Any = None) -> Any:
        raw = await self.value("SELECT value FROM settings WHERE key = ?", key)
        return default if raw is None else json.loads(raw)

    async def set_setting(self, key: str, value: Any) -> None:
        await self.run("INSERT INTO settings(key, value) VALUES (?, ?) "
                       "ON CONFLICT(key) DO UPDATE SET value = excluded.value", key, json.dumps(value))

    async def audit(self, actor: int | None, action: str, target: str = "", details: str = "") -> None:
        await self.run("INSERT INTO audit(actor, action, target, details, created_at) VALUES (?,?,?,?,?)",
                       actor, action, target, details[:2000], now())

    async def backup_to(self, dest: Path) -> None:
        """Целостная копия базы на ходу (SQLite backup API), без остановки."""
        assert self.conn
        dest.parent.mkdir(parents=True, exist_ok=True)
        async with self.lock:
            target = await aiosqlite.connect(dest)
            try:
                await self.conn.backup(target)
            finally:
                await target.close()
