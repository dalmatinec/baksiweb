"""Деньги: баланс, покупки, продления, пробный период, промокоды, рефералка. Все суммы в центах USD.

Любое движение денег идёт через одну транзакцию и пишется в ledger: баланс всегда можно пересчитать.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import aiosqlite

from .catalog import Catalog, plan_label
from .crypto import bind_code
from .db import Database, now

log = logging.getLogger("baksi.billing")
DAY = 86400


class BillingError(Exception):
    """Ошибка, текст которой можно показать пользователю."""


class InsufficientFunds(BillingError):
    def __init__(self, need: int) -> None:
        super().__init__("Недостаточно средств на балансе")
        self.need = need


def money(cents: int | None) -> str:
    if cents is None:
        return "—"
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100:,}.{cents % 100:02d}"


def parse_money(text: str) -> int | None:
    """'10', '10.5', '$10,50' -> центы."""
    raw = text.strip().replace("$", "").replace(" ", "").replace(",", ".")
    try:
        value = round(float(raw) * 100)
    except ValueError:
        return None
    return value if value > 0 else None


@dataclass(slots=True)
class Quote:
    product: str
    days: int
    price: int
    discount: int
    total: int
    promo: str | None
    promo_error: str | None

    def as_dict(self) -> dict:
        return {"product": self.product, "days": self.days, "label": plan_label(self.days), "price": self.price,
                "discount": self.discount, "total": self.total, "promo": self.promo, "promo_error": self.promo_error}


class Billing:
    def __init__(self, db: Database, catalog: Catalog) -> None:
        self.db = db
        self.catalog = catalog

    # --- пользователи ---------------------------------------------------------
    async def ensure_user(self, user_id: int, username: str | None, first_name: str | None,
                          ref_by: int | None = None) -> tuple[aiosqlite.Row, bool]:
        row = await self.db.one("SELECT * FROM users WHERE id = ?", user_id)
        ts = now()
        if row:
            if row["username"] != username or row["first_name"] != first_name or ts - row["last_seen"] > 300:
                await self.db.run("UPDATE users SET username = ?, first_name = ?, last_seen = ?, hub_blocked = 0 "
                                  "WHERE id = ?", username, first_name, ts, user_id)
            return row, False
        if ref_by == user_id or not (ref_by and await self.db.one("SELECT 1 FROM users WHERE id = ?", ref_by)):
            ref_by = None
        await self.db.run("INSERT OR IGNORE INTO users(id, username, first_name, ref_by, created_at, last_seen) "
                          "VALUES (?, ?, ?, ?, ?, ?)", user_id, username, first_name, ref_by, ts, ts)
        return await self.db.one("SELECT * FROM users WHERE id = ?", user_id), True

    # --- проводки (только внутри tx) -----------------------------------------
    @staticmethod
    async def _move(conn: aiosqlite.Connection, user_id: int, amount: int, kind: str,
                    ref: str = "", note: str = "") -> int:
        cur = await conn.execute("SELECT balance FROM users WHERE id = ?", (user_id,))
        row = await cur.fetchone()
        if row is None:
            raise BillingError("Пользователь не найден")
        balance = row[0] + amount
        if balance < 0:
            raise InsufficientFunds(-balance)
        await conn.execute("UPDATE users SET balance = ? WHERE id = ?", (balance, user_id))
        await conn.execute("INSERT INTO ledger(user_id, amount, balance_after, kind, ref, note, created_at) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?)", (user_id, amount, balance, kind, ref, note, now()))
        return balance

    # --- промокоды ------------------------------------------------------------
    async def _check_promo(self, code: str, user_id: int, product: str | None, kinds: tuple[str, ...]) -> dict:
        row = await self.db.one("SELECT * FROM promocodes WHERE code = ?", code.strip().upper())
        if not row or row["kind"] not in kinds:
            raise BillingError("Промокод не найден")
        if row["expires_at"] and row["expires_at"] < now():
            raise BillingError("Срок действия промокода истёк")
        if row["max_uses"] and row["used"] >= row["max_uses"]:
            raise BillingError("Промокод закончился")
        if row["product"] and product and row["product"] != product:
            raise BillingError("Промокод не подходит для этого бота")
        if await self.db.one("SELECT 1 FROM promo_uses WHERE code = ? AND user_id = ?", row["code"], user_id):
            raise BillingError("Вы уже использовали этот промокод")
        return dict(row)

    async def quote(self, user_id: int, product: str, days: int, promo: str | None = None) -> Quote:
        plan = await self.catalog.plan(product, days)
        if not plan:
            raise BillingError("Этот тариф сейчас не продаётся")
        price, discount, err, code = plan["price"], 0, None, None
        if promo and promo.strip():
            try:
                p = await self._check_promo(promo, user_id, product, ("percent", "amount"))
                code = p["code"]
                discount = price * min(p["value"], 100) // 100 if p["kind"] == "percent" else min(p["value"], price)
            except BillingError as exc:
                err = str(exc)
        return Quote(product, days, price, discount, price - discount, code, err)

    async def redeem_balance_promo(self, user_id: int, code: str) -> int:
        p = await self._check_promo(code, user_id, None, ("balance",))
        async with self.db.tx() as conn:
            cur = await conn.execute("UPDATE promocodes SET used = used + 1 WHERE code = ? "
                                     "AND (max_uses = 0 OR used < max_uses)", (p["code"],))
            if cur.rowcount != 1:
                raise BillingError("Промокод закончился")
            await conn.execute("INSERT INTO promo_uses(code, user_id, used_at) VALUES (?, ?, ?)",
                               (p["code"], user_id, now()))
            await self._move(conn, user_id, p["value"], "promo", p["code"], "Промокод на баланс")
        return p["value"]

    # --- копии ботов ----------------------------------------------------------
    async def _new_instance(self, conn: aiosqlite.Connection, user_id: int, product: str) -> int:
        cur = await conn.execute("INSERT INTO instances(user_id, product, bind_code, created_at, status) "
                                 "VALUES (?, ?, ?, ?, 'setup')", (user_id, product, bind_code(), now()))
        return cur.lastrowid

    @staticmethod
    async def _extend(conn: aiosqlite.Connection, instance_id: int, days: int) -> None:
        cur = await conn.execute("SELECT expires_at, forever FROM instances WHERE id = ?", (instance_id,))
        row = await cur.fetchone()
        if row["forever"]:
            raise BillingError("У этого бота уже бессрочная подписка")
        if days == 0:
            await conn.execute("UPDATE instances SET forever = 1, expires_at = NULL, plan_days = 0, trial = 0, "
                               "expired_at = NULL WHERE id = ?", (instance_id,))
            return
        base = max(now(), row["expires_at"] or 0)
        await conn.execute("UPDATE instances SET expires_at = ?, plan_days = ?, trial = 0, expired_at = NULL "
                           "WHERE id = ?", (base + days * DAY, days, instance_id))

    async def purchase(self, user_id: int, product: str, days: int, promo: str | None = None,
                       instance_id: int | None = None) -> int:
        """Покупка или продление с баланса. Возвращает id копии. InsufficientFunds - если не хватает."""
        if not self.catalog.get(product):
            raise BillingError("Такого бота нет в каталоге")
        q = await self.quote(user_id, product, days, promo)
        if promo and q.promo_error:
            raise BillingError(q.promo_error)
        async with self.db.tx() as conn:
            if instance_id is not None:
                cur = await conn.execute("SELECT user_id, product, deleted_at FROM instances WHERE id = ?",
                                         (instance_id,))
                inst = await cur.fetchone()
                if not inst or inst["user_id"] != user_id or inst["deleted_at"] or inst["product"] != product:
                    raise BillingError("Бот не найден")
            else:
                instance_id = await self._new_instance(conn, user_id, product)
            if q.promo:
                cur = await conn.execute("UPDATE promocodes SET used = used + 1 WHERE code = ? "
                                         "AND (max_uses = 0 OR used < max_uses)", (q.promo,))
                if cur.rowcount != 1:
                    raise BillingError("Промокод закончился")
                await conn.execute("INSERT INTO promo_uses(code, user_id, used_at) VALUES (?, ?, ?)",
                                   (q.promo, user_id, now()))
            title = self.catalog.get(product).title
            await self._move(conn, user_id, -q.total, "purchase", f"instance:{instance_id}",
                             f"{title} · {plan_label(days)}" + (f" · промокод {q.promo}" if q.promo else ""))
            await self._extend(conn, instance_id, days)
        log.info("Покупка: user=%s %s %s дн. инстанс=%s сумма=%s", user_id, product, days, instance_id, q.total)
        return instance_id

    async def start_trial(self, user_id: int, product: str) -> int:
        row = await self.db.one("SELECT enabled, trial_days FROM products WHERE slug = ?", product)
        if not row or not row["enabled"] or not self.catalog.get(product):
            raise BillingError("Такого бота нет в каталоге")
        if row["trial_days"] <= 0:
            raise BillingError("Для этого бота нет пробного периода")
        async with self.db.tx() as conn:
            cur = await conn.execute("INSERT OR IGNORE INTO trials(user_id, product, used_at) VALUES (?, ?, ?)",
                                     (user_id, product, now()))
            if cur.rowcount != 1:
                raise BillingError("Пробный период для этого бота уже использован")
            instance_id = await self._new_instance(conn, user_id, product)
            await conn.execute("UPDATE instances SET expires_at = ?, trial = 1, auto_renew = 0 WHERE id = ?",
                               (now() + row["trial_days"] * DAY, instance_id))
        return instance_id

    async def trial_available(self, user_id: int, product: str) -> bool:
        row = await self.db.one("SELECT trial_days FROM products WHERE slug = ?", product)
        if not row or row["trial_days"] <= 0:
            return False
        return not await self.db.one("SELECT 1 FROM trials WHERE user_id = ? AND product = ?", user_id, product)

    # --- автопродление --------------------------------------------------------
    async def process_expired(self) -> list[dict[str, Any]]:
        """Истёкшие копии: продлить с баланса или остановить. Возвращает события для уведомлений."""
        events: list[dict[str, Any]] = []
        due = await self.db.all("SELECT * FROM instances WHERE deleted_at IS NULL AND forever = 0 "
                                "AND expires_at IS NOT NULL AND expires_at <= ? AND expired_at IS NULL", now())
        for inst in due:
            title = self.catalog.get(inst["product"]).title if self.catalog.get(inst["product"]) else inst["product"]
            renewed = False
            if inst["auto_renew"] and not inst["trial"] and inst["plan_days"]:
                plan = await self.catalog.plan(inst["product"], inst["plan_days"])
                if plan:
                    try:
                        async with self.db.tx() as conn:
                            await self._move(conn, inst["user_id"], -plan["price"], "renew", f"instance:{inst['id']}",
                                             f"Автопродление {title} · {plan_label(inst['plan_days'])}")
                            await self._extend(conn, inst["id"], inst["plan_days"])
                        renewed = True
                        events.append({"kind": "renewed", "instance": inst["id"], "user": inst["user_id"],
                                       "amount": plan["price"], "days": inst["plan_days"]})
                    except InsufficientFunds as exc:
                        events.append({"kind": "renew_failed", "instance": inst["id"], "user": inst["user_id"],
                                       "need": exc.need, "price": plan["price"]})
            if not renewed:
                await self.db.run("UPDATE instances SET expired_at = ? WHERE id = ?", now(), inst["id"])
                events.append({"kind": "expired", "instance": inst["id"], "user": inst["user_id"],
                               "trial": bool(inst["trial"])})
        return events

    async def upcoming_reminders(self) -> list[dict[str, Any]]:
        """Напоминания за 3 дня и за 1 день. Каждое отправляется один раз на каждую дату окончания."""
        out = []
        ts = now()
        for kind, window in (("3d", 3 * DAY), ("1d", DAY)):
            rows = await self.db.all(
                "SELECT i.*, u.balance FROM instances i JOIN users u ON u.id = i.user_id "
                "WHERE i.deleted_at IS NULL AND i.forever = 0 AND i.expired_at IS NULL AND i.expires_at > ? "
                "AND i.expires_at <= ? AND NOT EXISTS (SELECT 1 FROM reminders r WHERE r.instance_id = i.id "
                "AND r.kind = ? AND r.expires_at = i.expires_at)", ts, ts + window, kind)
            for r in rows:
                if kind == "3d" and r["expires_at"] - ts <= DAY:
                    continue  # для близких сроков достаточно напоминания за день
                await self.db.run("INSERT OR IGNORE INTO reminders(instance_id, kind, expires_at) VALUES (?,?,?)",
                                  r["id"], kind, r["expires_at"])
                price = None
                if r["plan_days"] and not r["trial"]:
                    plan = await self.catalog.plan(r["product"], r["plan_days"])
                    price = plan["price"] if plan else None
                out.append({"kind": kind, "instance": dict(r), "price": price})
        return out

    # --- пополнения -----------------------------------------------------------
    async def mark_invoice_paid(self, invoice_id: int) -> dict | None:
        """Идемпотентно: повторный вызов для оплаченного счёта ничего не делает. Возвращает счёт, если зачислили."""
        async with self.db.tx() as conn:
            cur = await conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,))
            inv = await cur.fetchone()
            if not inv or inv["status"] == "paid":
                return None
            await conn.execute("UPDATE invoices SET status = 'paid', paid_at = ? WHERE id = ?", (now(), invoice_id))
            await self._move(conn, inv["user_id"], inv["amount"], "topup", f"invoice:{invoice_id}",
                             f"Пополнение через {inv['provider']}")
            percent = int(await self._setting(conn, "referral_percent", 0))
            cur = await conn.execute("SELECT ref_by FROM users WHERE id = ?", (inv["user_id"],))
            ref = (await cur.fetchone())["ref_by"]
            bonus = inv["amount"] * percent // 100 if ref and percent > 0 else 0
            if bonus > 0:
                await self._move(conn, ref, bonus, "referral", f"user:{inv['user_id']}",
                                 f"{percent}% с пополнения реферала")
                await conn.execute("UPDATE users SET ref_earned = ref_earned + ? WHERE id = ?", (bonus, ref))
        result = dict(inv) | {"referrer": ref if bonus else None, "bonus": bonus}
        return result

    @staticmethod
    async def _setting(conn: aiosqlite.Connection, key: str, default: Any) -> Any:
        cur = await conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cur.fetchone()
        return json.loads(row[0]) if row else default

    async def run_intent(self, invoice: dict) -> int | None:
        """После оплаты счёта сразу выполнить покупку, ради которой пополняли."""
        try:
            intent = json.loads(invoice.get("intent") or "{}")
        except ValueError:
            return None
        if intent.get("action") != "purchase":
            return None
        try:
            return await self.purchase(invoice["user_id"], intent["product"], int(intent["days"]),
                                       intent.get("promo"), intent.get("instance_id"))
        except BillingError as exc:
            log.info("Покупка после оплаты счёта %s не выполнена: %s", invoice["id"], exc)
            return None

    async def admin_adjust(self, actor: int, user_id: int, amount: int, note: str) -> int:
        async with self.db.tx() as conn:
            balance = await self._move(conn, user_id, amount, "admin", f"admin:{actor}", note or "Корректировка")
        await self.db.audit(actor, "balance", f"user:{user_id}", f"{amount} {note}")
        return balance

    async def refund_instance_days(self, actor: int, instance_id: int, amount: int, note: str) -> None:
        row = await self.db.one("SELECT user_id FROM instances WHERE id = ?", instance_id)
        if not row:
            raise BillingError("Бот не найден")
        async with self.db.tx() as conn:
            await self._move(conn, row["user_id"], amount, "refund", f"instance:{instance_id}", note or "Возврат")
        await self.db.audit(actor, "refund", f"instance:{instance_id}", f"{amount} {note}")
