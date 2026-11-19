"""Маленькая обёртка над aiosqlite для модулей: WAL, миграции, настройки ключ-значение."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import aiosqlite


class ModuleDB:
    def __init__(self, path: Path, migrations: list[str]) -> None:
        self.path = path
        self.migrations = ["CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)"] + migrations
        self.conn: aiosqlite.Connection | None = None
        self.lock = asyncio.Lock()
        self._kv: dict[str, Any] = {}

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path, timeout=30)
        self.conn.row_factory = aiosqlite.Row
        for pragma in ("journal_mode=WAL", "synchronous=NORMAL", "busy_timeout=10000", "foreign_keys=ON"):
            await self.conn.execute(f"PRAGMA {pragma}")
        version = (await (await self.conn.execute("PRAGMA user_version")).fetchone())[0]
        for i, sql in enumerate(self.migrations[version:], start=version + 1):
            await self.conn.executescript(f"BEGIN; {sql}; PRAGMA user_version = {i}; COMMIT;")
        for row in await self.all("SELECT key, value FROM kv"):
            self._kv[row["key"]] = json.loads(row["value"])

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    async def one(self, sql: str, *args: Any) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, args) as cur:
            return await cur.fetchone()

    async def all(self, sql: str, *args: Any) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, args) as cur:
            return list(await cur.fetchall())

    async def value(self, sql: str, *args: Any, default: Any = None) -> Any:
        row = await self.one(sql, *args)
        return row[0] if row and row[0] is not None else default

    async def run(self, sql: str, *args: Any) -> int:
        async with self.lock:
            cur = await self.conn.execute(sql, args)
            await self.conn.commit()
            return cur.lastrowid or 0

    def get(self, key: str, default: Any = None) -> Any:
        return self._kv.get(key, default)

    async def set(self, key: str, value: Any) -> None:
        self._kv[key] = value
        await self.run("INSERT INTO kv(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                       key, json.dumps(value, ensure_ascii=False))
