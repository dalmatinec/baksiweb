"""Пополнение напрямую в USDT: сеть TON (toncenter) и TRON/TRC20 (TronGrid), как в dalmatinec/baxi.

Как узнаём платёж: у каждого счёта своя точная сумма (например 10.0037 USDT), по ней и находим счёт;
годится перевод в любой из двух сетей. В сети TON счёт находится ещё и по комментарию BC<номер>.
Проверка каждые полминуты (и сразу по кнопке «Я оплатил»). Каждая транзакция учитывается один раз.
Платёж, который не подошёл ни к одному счёту, - уведомление владельцу: разберётся вручную.

Настройки в .env: PAY_TON_ADDRESS и/или PAY_TRON_ADDRESS (адреса ваших кошельков), по желанию
TONCENTER_API_KEY и TRONGRID_API_KEY (без них тоже работает, но с меньшим лимитом запросов).
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from typing import Any

import aiohttp

from .billing import money
from .db import now

log = logging.getLogger("baksi.usdt")

INVOICE_TTL = 24 * 3600
MIN_TOPUP = 100          # $1
MAX_TOPUP = 1_000_000    # $10 000
CHECK_EVERY = 25
NETS = {"ton": "USDT · сеть TON", "trc20": "USDT · TRC20"}

MIGRATION = """
CREATE TABLE IF NOT EXISTS usdt_seen (
    tx      TEXT PRIMARY KEY,
    seen_at INTEGER NOT NULL
);
"""


class UsdtError(Exception):
    pass


def usdt(units: int) -> str:
    """Микро-USDT → строка без лишних нулей: 10003700 → '10.0037'."""
    return f"{units / 1_000_000:.6f}".rstrip("0").rstrip(".")


def extract_comment(payload: Any) -> str:
    """Комментарий из decoded_forward_payload toncenter: любая строка по ключам comment/text."""
    if isinstance(payload, dict):
        for key in ("comment", "text"):
            if isinstance(payload.get(key), str):
                return payload[key]
        for value in payload.values():
            found = extract_comment(value)
            if found:
                return found
    if isinstance(payload, list):
        for value in payload:
            found = extract_comment(value)
            if found:
                return found
    return ""


def invoice_units(row: Any) -> int:
    """Точная сумма счёта хранится в external_id как '<микро-USDT>#<номер счёта>'."""
    try:
        return int(str(row["external_id"]).split("#")[0])
    except (TypeError, ValueError, IndexError):
        return 0


def match_invoice(transfer: dict, active: list[Any]) -> Any:
    """Чей платёж: по комментарию BC<номер> (сумма не меньше счёта) или по точной сумме."""
    m = re.search(r"BC\s*(\d+)", transfer.get("comment") or "", re.I)
    if m:
        for inv in active:
            if inv["id"] == int(m.group(1)) and transfer["amount"] >= inv["amount"] * 10_000:
                return inv
    for inv in active:
        if invoice_units(inv) == transfer["amount"] and transfer["time"] >= inv["created_at"] - 60:
            return inv
    return None


class UsdtPay:
    def __init__(self, db, billing, notifier) -> None:
        self.db = db
        self.billing = billing
        self.notifier = notifier
        env = os.environ.get
        self.ton_address = env("PAY_TON_ADDRESS", "").strip()
        self.tron_address = env("PAY_TRON_ADDRESS", "").strip()
        self.toncenter_key = env("TONCENTER_API_KEY", "").strip()
        self.trongrid_key = env("TRONGRID_API_KEY", "").strip()
        self.toncenter_url = env("TONCENTER_URL", "https://toncenter.com/api/v3").rstrip("/")
        self.trongrid_url = env("TRONGRID_URL", "https://api.trongrid.io").rstrip("/")
        self.usdt_master = env("USDT_MASTER", "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs")
        self.tron_contract = env("TRON_USDT_CONTRACT", "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t")
        self._session: aiohttp.ClientSession | None = None
        self._last_check = 0.0

    async def migrate(self) -> None:
        await self.db.conn.executescript(MIGRATION)
        await self.db.conn.commit()

    @property
    def nets(self) -> list[str]:
        return [n for n, addr in (("ton", self.ton_address), ("trc20", self.tron_address)) if addr]

    @property
    def enabled(self) -> bool:
        return bool(self.nets)

    def address(self, net: str) -> str:
        return self.ton_address if net == "ton" else self.tron_address

    # --- счёт ---------------------------------------------------------------------------
    async def unique_units(self, cents: int) -> int:
        """Сумма плюс «копейки» 0.0001–0.0099, которых нет у других неоплаченных USDT-счетов."""
        base = cents * 10_000
        taken = {invoice_units(r) for r in await self.db.all(
            "SELECT external_id FROM invoices WHERE provider LIKE 'usdt_%' AND status = 'active'")}
        for _ in range(300):
            units = base + random.randint(1, 99) * 100
            if units not in taken:
                return units
        return base + random.randint(100, 9999)

    async def create(self, user_id: int, cents: int, net: str, intent: dict | None = None) -> dict:
        if net not in self.nets:
            raise UsdtError("Эта сеть сейчас не принимается")
        if cents < MIN_TOPUP:
            raise UsdtError(f"Минимальное пополнение {money(MIN_TOPUP)}")
        if cents > MAX_TOPUP:
            raise UsdtError(f"Максимальное пополнение {money(MAX_TOPUP)}")
        active = await self.db.value("SELECT COUNT(*) FROM invoices WHERE user_id = ? AND status = 'active'",
                                     user_id, default=0)
        if active >= 5:
            raise UsdtError("У вас уже есть неоплаченные счета. Оплатите их или подождите")
        units = await self.unique_units(cents)
        invoice_id = await self.db.run(
            "INSERT INTO invoices(user_id, provider, amount, intent, created_at) VALUES (?, ?, ?, ?, ?)",
            user_id, f"usdt_{net}", cents, json.dumps(intent or {}), now())
        await self.db.run("UPDATE invoices SET external_id = ? WHERE id = ?", f"{units}#{invoice_id}", invoice_id)
        return self.view(await self.db.one("SELECT * FROM invoices WHERE id = ?", invoice_id))

    def view(self, row: Any) -> dict:
        net = str(row["provider"]).removeprefix("usdt_")
        units = invoice_units(row)
        return {"id": row["id"], "provider": row["provider"], "net": net, "net_title": NETS.get(net, net),
                "amount": row["amount"], "usdt": usdt(units), "address": self.address(net),
                "comment": f"BC{row['id']}" if net == "ton" else "", "status": row["status"],
                "expires_at": row["created_at"] + INVOICE_TTL}

    def instructions(self, inv: dict) -> str:
        """Текст счёта для Telegram (HTML)."""
        lines = [f"🧾 <b>Счёт #{inv['id']} на {money(inv['amount'])}</b>\n",
                 f"Переведите <b>ровно {inv['usdt']} USDT</b> в сети <b>{'TON' if inv['net'] == 'ton' else 'TRON (TRC20)'}</b>:",
                 f"<code>{inv['address']}</code>"]
        if inv["comment"]:
            lines.append(f"Комментарий, если кошелёк позволяет: <code>{inv['comment']}</code>")
        lines += ["", "⚠️ Только USDT и только в этой сети, иначе деньги потеряются. Сумма должна совпасть до последней "
                      "цифры: по ней я узнаю ваш платёж. С биржи учитывайте её комиссию: на адрес должно прийти ровно "
                      f"{inv['usdt']} USDT.",
                  "Проверяю сам каждые полминуты, счёт действует 24 часа. Как только перевод подтвердится, баланс "
                  "пополнится и придёт уведомление."]
        return "\n".join(lines)

    # --- проверка поступлений --------------------------------------------------------------
    async def _get(self, url: str, params: dict, headers: dict) -> dict:
        if not self._session:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        async with self._session.get(url, params=params, headers=headers) as resp:
            if resp.status != 200:
                text = await resp.text()
                log.warning("%s ответил %s: %s", url.split("/")[2], resp.status, text[:200])
                return {}
            return await resp.json(content_type=None)

    async def fetch_ton(self, since: int) -> list[dict]:
        headers = {"X-API-Key": self.toncenter_key} if self.toncenter_key else {}
        data = await self._get(f"{self.toncenter_url}/jetton/transfers", {
            "owner_address": self.ton_address, "direction": "in", "jetton_master": self.usdt_master,
            "start_utime": since, "limit": 100, "sort": "desc"}, headers)
        out = []
        for t in data.get("jetton_transfers", []) or []:
            if t.get("transaction_aborted") or not t.get("transaction_hash"):
                continue
            try:
                amount = int(t.get("amount", 0))
            except (TypeError, ValueError):
                continue
            out.append({"tx": "ton:" + t["transaction_hash"], "amount": amount, "net": "TON",
                        "time": int(t.get("transaction_now") or 0),
                        "comment": extract_comment(t.get("decoded_forward_payload"))})
        return out

    async def fetch_tron(self, since: int) -> list[dict]:
        headers = {"TRON-PRO-API-KEY": self.trongrid_key} if self.trongrid_key else {}
        data = await self._get(f"{self.trongrid_url}/v1/accounts/{self.tron_address}/transactions/trc20", {
            "only_to": "true", "only_confirmed": "true", "contract_address": self.tron_contract,
            "min_timestamp": since * 1000, "limit": 100, "order_by": "block_timestamp,desc"}, headers)
        out = []
        for t in data.get("data", []) or []:
            if t.get("to") != self.tron_address or not t.get("transaction_id"):
                continue
            try:
                amount = int(t.get("value", 0))
            except (TypeError, ValueError):
                continue
            out.append({"tx": "tron:" + t["transaction_id"], "amount": amount, "net": "TRC20",
                        "time": int(t.get("block_timestamp", 0)) // 1000, "comment": ""})
        return out

    async def poll(self, force: bool = False) -> list[dict]:
        """Проверить поступления. Возвращает зачисленные счета (для уведомлений и покупок)."""
        if not self.enabled or (not force and time.monotonic() - self._last_check < CHECK_EVERY):
            return []
        self._last_check = time.monotonic()
        await self.db.run("UPDATE invoices SET status = 'expired' WHERE provider LIKE 'usdt_%' AND status = 'active' "
                          "AND created_at < ?", now() - INVOICE_TTL)
        active = await self.db.all("SELECT * FROM invoices WHERE provider LIKE 'usdt_%' AND status = 'active'")
        if not active:
            return []
        since = min(r["created_at"] for r in active) - 300
        transfers: list[dict] = []
        for enabled, fetch, name in ((self.ton_address, self.fetch_ton, "toncenter"),
                                     (self.tron_address, self.fetch_tron, "TronGrid")):
            if not enabled:
                continue
            try:
                transfers += await fetch(since)
            except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
                log.warning("%s недоступен: %s", name, exc)
        paid = []
        for t in sorted(transfers, key=lambda x: x["time"]):
            if await self.db.one("SELECT 1 FROM usdt_seen WHERE tx = ?", t["tx"]):
                continue
            await self.db.run("INSERT OR IGNORE INTO usdt_seen(tx, seen_at) VALUES (?, ?)", t["tx"], now())
            inv = match_invoice(t, active)
            if inv is None:
                self.notifier.owners(f"💸 Пришёл платёж {usdt(t['amount'])} USDT ({t['net']}), но он не подошёл ни к "
                                     f"одному счёту.\nТранзакция: <code>{t['tx']}</code>\nЕсли клиент написал в "
                                     "поддержку, зачислите вручную: веб-админка → Клиенты → баланс.")
                continue
            active = [a for a in active if a["id"] != inv["id"]]
            done = await self.billing.mark_invoice_paid(inv["id"])
            if done:
                await self.db.run("UPDATE invoices SET pay_url = ? WHERE id = ?", t["tx"], inv["id"])
                paid.append(done)
        return paid

    async def close(self) -> None:
        if self._session:
            await self._session.close()
