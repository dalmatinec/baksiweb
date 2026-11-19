"""Каталог ботов. Каждый бот - папка в modules/ с manifest.json. Новый бот = новая папка, код платформы не трогаем."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .db import Database

log = logging.getLogger("baksi.catalog")

DEFAULT_PLAN_DAYS = [7, 30, 90, 180, 365, 0]
SETTING_TYPES = {"bool", "text", "int", "ids", "choice"}


@dataclass(frozen=True, slots=True)
class Setting:
    key: str
    type: str
    label: str
    hint: str = ""
    default: Any = None
    options: tuple[tuple[str, str], ...] = ()
    restart: bool = True     # нужна ли перезагрузка копии после изменения


@dataclass(frozen=True, slots=True)
class Module:
    slug: str
    title: str
    tagline: str
    description: str
    path: Path
    entry: str
    color: str
    icon: str
    mem_mb: int
    needs_bind: bool
    bind_hint: str
    how: tuple[str, ...]
    features: tuple[str, ...]
    settings: tuple[Setting, ...]
    default_plans: tuple[int, ...]
    sort: int = 100
    extra: dict = field(default_factory=dict)

    def public(self) -> dict:
        return {
            "slug": self.slug, "title": self.title, "tagline": self.tagline,
            "description": self.description, "color": self.color, "icon": self.icon,
            "needs_bind": self.needs_bind, "bind_hint": self.bind_hint,
            "how": list(self.how), "features": list(self.features),
            "settings": [{"key": s.key, "type": s.type, "label": s.label, "hint": s.hint,
                          "default": s.default, "options": [list(o) for o in s.options]} for s in self.settings],
        }


def _setting(raw: dict) -> Setting:
    kind = raw.get("type", "text")
    if kind not in SETTING_TYPES:
        raise ValueError(f"неизвестный тип настройки {kind!r}")
    return Setting(
        key=str(raw["key"]), type=kind, label=str(raw.get("label", raw["key"])), hint=str(raw.get("hint", "")),
        default=raw.get("default"), options=tuple((str(a), str(b)) for a, b in raw.get("options", [])),
        restart=bool(raw.get("restart", True)),
    )


def load_modules(modules_dir: Path, default_mem_mb: int) -> dict[str, Module]:
    found: dict[str, Module] = {}
    if not modules_dir.exists():
        log.warning("Папка модулей %s не найдена", modules_dir)
        return found
    for manifest in sorted(modules_dir.glob("*/manifest.json")):
        try:
            raw = json.loads(manifest.read_text(encoding="utf-8"))
            slug = str(raw["slug"])
            if not slug.isidentifier():
                raise ValueError("slug должен состоять из латиницы, цифр и _")
            entry_name = str(raw.get("entry", "bot")).split(".")[0]
            if not ((manifest.parent / entry_name).is_dir() or (manifest.parent / f"{entry_name}.py").exists()):
                raise ValueError(f"нет точки входа {entry_name}")
            found[slug] = Module(
                slug=slug, title=str(raw["title"]), tagline=str(raw.get("tagline", "")),
                description=str(raw.get("description", "")), path=manifest.parent,
                entry=str(raw.get("entry", "bot")), color=str(raw.get("color", "#B6F23A")),
                icon=str(raw.get("icon", "bot")), mem_mb=int(raw.get("mem_mb", default_mem_mb)),
                needs_bind=bool(raw.get("needs_bind", False)), bind_hint=str(raw.get("bind_hint", "")),
                how=tuple(raw.get("how", [])), features=tuple(raw.get("features", [])),
                settings=tuple(_setting(s) for s in raw.get("settings", [])),
                default_plans=tuple(int(d) for d in raw.get("default_plans", DEFAULT_PLAN_DAYS)),
                sort=int(raw.get("sort", 100)),
            )
        except Exception as exc:  # битый манифест не должен ронять платформу
            log.error("Модуль %s пропущен: %s", manifest.parent.name, exc)
    return found


class Catalog:
    def __init__(self, db: Database, modules: dict[str, Module]) -> None:
        self.db = db
        self.modules = modules

    async def sync(self) -> None:
        """Новые модули появляются в базе сами, цены не трогаем (их ставит владелец)."""
        for m in self.modules.values():
            await self.db.run("INSERT OR IGNORE INTO products(slug, sort) VALUES (?, ?)", m.slug, m.sort)
            await self.db.many("INSERT OR IGNORE INTO plans(product, days, price) VALUES (?, ?, NULL)",
                               [(m.slug, d) for d in m.default_plans])

    def get(self, slug: str) -> Module | None:
        return self.modules.get(slug)

    async def products(self, include_hidden: bool = False) -> list[dict]:
        rows = await self.db.all("SELECT * FROM products ORDER BY sort, slug")
        plans = await self.db.all("SELECT * FROM plans ORDER BY CASE days WHEN 0 THEN 100000 ELSE days END")
        by_product: dict[str, list[dict]] = {}
        for p in plans:
            by_product.setdefault(p["product"], []).append(
                {"id": p["id"], "days": p["days"], "price": p["price"], "enabled": bool(p["enabled"])})
        out = []
        for r in rows:
            m = self.modules.get(r["slug"])
            if not m:
                continue
            all_plans = by_product.get(r["slug"], [])
            on_sale = [p for p in all_plans if p["enabled"] and p["price"] is not None]
            if not include_hidden and (not r["enabled"] or not on_sale):
                continue
            item = m.public() | {"enabled": bool(r["enabled"]), "trial_days": r["trial_days"],
                                 "plans": all_plans if include_hidden else on_sale}
            item["min_price"] = min((p["price"] for p in on_sale), default=None)
            out.append(item)
        return out

    async def plan(self, product: str, days: int) -> dict | None:
        row = await self.db.one("SELECT p.*, pr.enabled AS product_enabled FROM plans p "
                                "JOIN products pr ON pr.slug = p.product WHERE p.product = ? AND p.days = ?",
                                product, days)
        if not row or not row["enabled"] or not row["product_enabled"] or row["price"] is None:
            return None
        return dict(row)


def plan_label(days: int) -> str:
    if days == 0:
        return "Навсегда"
    if days % 365 == 0:
        n = days // 365
        return "1 год" if n == 1 else f"{n} года" if n < 5 else f"{n} лет"
    n = days
    if n % 10 == 1 and n % 100 != 11:
        word = "день"
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        word = "дня"
    else:
        word = "дней"
    return f"{n} {word}"
