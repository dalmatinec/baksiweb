"""Пополнение напрямую на ваши кошельки: USDT (сети TON и TRC20), TON и Bitcoin. Подход как в dalmatinec/baxi.

Как узнаём платёж: у каждого счёта своя точная сумма (например 10.0037 USDT или 1.8342 TON), по ней и находим
счёт. В сети TON счёт находится ещё и по комментарию BC<номер>. USDT годится в любой из двух сетей.
TON и BTC считаются по курсу на момент счёта (Binance, запасной - CoinGecko), курс держится 2 часа.
Bitcoin засчитывается после 1 подтверждения сети (защита от отмены перевода).
Проверка каждые полминуты (и сразу по кнопке «Я оплатил»). Каждая транзакция учитывается один раз.
Платёж, который не подошёл ни к одному счёту, - уведомление владельцу: разберётся вручную.

Настройки в .env: PAY_TON_ADDRESS, PAY_TRON_ADDRESS, PAY_BTC_ADDRESS (любые из них).
По желанию TONCENTER_API_KEY, TRONGRID_API_KEY. PAY_TON_COIN=0 - не принимать сам TON, только USDT в сети TON.
"""
from __future__ import annotations

import json
import logging
import math
import os
import random
import re
import time
from typing import Any

import aiohttp

from .billing import money
from .db import now

log = logging.getLogger("baksi.usdt")

MIN_TOPUP = 100          # $1
MAX_TOPUP = 1_000_000    # $10 000
CHECK_EVERY = 25
RATE_TTL = 60

METHODS: dict[str, dict] = {
    "usdt_ton": {"asset": "usdt", "net": "ton", "title": "USDT · сеть TON", "ticker": "USDT", "decimals": 6,
                 "ttl": 24 * 3600, "net_title": "TON"},
    "usdt_trc20": {"asset": "usdt", "net": "trc20", "title": "USDT · TRC20", "ticker": "USDT", "decimals": 6,
                   "ttl": 24 * 3600, "net_title": "TRON (TRC20)"},
    "ton": {"asset": "ton", "net": "ton", "title": "TON", "ticker": "TON", "decimals": 9, "ttl": 2 * 3600,
            "net_title": "TON"},
    "btc": {"asset": "btc", "net": "btc", "title": "Bitcoin", "ticker": "BTC", "decimals": 8, "ttl": 2 * 3600,
            "net_title": "Bitcoin"},
}
NETS = {k: v["title"] for k, v in METHODS.items()}  # совместимость

MIGRATION = """
CREATE TABLE IF NOT EXISTS usdt_seen (
    tx      TEXT PRIMARY KEY,
    seen_at INTEGER NOT NULL
);
"""


class UsdtError(Exception):
    pass


def fmt_units(units: int, decimals: int) -> str:
    """Минимальные единицы → строка без лишних нулей: (10003700, 6) → '10.0037'."""
    return f"{units / 10 ** decimals:.{decimals}f}".rstrip("0").rstrip(".")


def usdt(units: int) -> str:
    return fmt_units(units, 6)


def extract_comment(payload: Any) -> str:
    """Комментарий из ответа toncenter: любая строка по ключам comment/text."""
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
    """Точная сумма счёта хранится в external_id как '<минимальные единицы>#<номер счёта>'."""
    try:
        return int(str(row["external_id"]).split("#")[0])
    except (TypeError, ValueError, IndexError):
        return 0


def method_of(row: Any) -> dict:
    return METHODS.get(str(row["provider"]), METHODS["usdt_ton"])


def invoice_min_units(row: Any) -> int:
    """Сколько минимально должно прийти по комментарию: ровно сумма счёта без «копеек»."""
    m = method_of(row)
    if m["asset"] == "usdt":
        return row["amount"] * 10_000
    return invoice_units(row) - 10 ** (m["decimals"] - 2)  # TON/BTC: допуск на «копейки» уникальности


def match_invoice(transfer: dict, active: list[Any]) -> Any:
    """Чей платёж: по комментарию BC<номер> (сумма не меньше счёта) или по точной сумме. Только тот же актив."""
    asset = transfer.get("asset", "usdt")
    same = [inv for inv in active if method_of(inv)["asset"] == asset]
    m = re.search(r"BC\s*(\d+)", transfer.get("comment") or "", re.I)
    if m:
        for inv in same:
            if inv["id"] == int(m.group(1)) and transfer["amount"] >= invoice_min_units(inv):
                return inv
    for inv in same:
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
        self.btc_address = env("PAY_BTC_ADDRESS", "").strip()
        self.ton_coin = env("PAY_TON_COIN", "1").strip() not in ("0", "false", "no")
        self.toncenter_key = env("TONCENTER_API_KEY", "").strip()
        self.trongrid_key = env("TRONGRID_API_KEY", "").strip()
        self.toncenter_url = env("TONCENTER_URL", "https://toncenter.com/api/v3").rstrip("/")
        self.trongrid_url = env("TRONGRID_URL", "https://api.trongrid.io").rstrip("/")
        self.btc_urls = [u.rstrip("/") for u in env("BTC_API_URLS", "https://mempool.space/api,"
                                                     "https://blockstream.info/api").split(",") if u.strip()]
        self.usdt_master = env("USDT_MASTER", "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs")
        self.tron_contract = env("TRON_USDT_CONTRACT", "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t")
        self._session: aiohttp.ClientSession | None = None
        self._last_check = 0.0
        self._rates: dict[str, tuple[float, float]] = {}

    async def migrate(self) -> None:
        await self.db.conn.executescript(MIGRATION)
        await self.db.conn.commit()

    @property
    def methods(self) -> list[str]:
        out = []
        if self.ton_address:
            out.append("usdt_ton")
        if self.tron_address:
            out.append("usdt_trc20")
        if self.ton_address and self.ton_coin:
            out.append("ton")
        if self.btc_address:
            out.append("btc")
        return out

    @property
    def nets(self) -> list[str]:  # совместимость: какие сети USDT включены
        return [m.removeprefix("usdt_") for m in self.methods if m.startswith("usdt_")]

    @property
    def enabled(self) -> bool:
        return bool(self.methods)

    def address(self, net: str) -> str:
        return {"ton": self.ton_address, "trc20": self.tron_address, "btc": self.btc_address}.get(net, "")

    # --- курс ---------------------------------------------------------------------------
    async def rate(self, asset: str) -> float:
        """Цена 1 TON или 1 BTC в долларах. Кэш минута; Binance, при сбое - CoinGecko."""
        cached = self._rates.get(asset)
        if cached and time.monotonic() - cached[1] < RATE_TTL:
            return cached[0]
        symbol = {"ton": "TONUSDT", "btc": "BTCUSDT"}[asset]
        gecko = {"ton": "the-open-network", "btc": "bitcoin"}[asset]
        price = 0.0
        try:
            data = await self._get("https://api.binance.com/api/v3/ticker/price", {"symbol": symbol}, {})
            price = float(data.get("price") or 0)
        except (aiohttp.ClientError, TimeoutError, ValueError):
            price = 0.0
        if price <= 0:
            try:
                data = await self._get("https://api.coingecko.com/api/v3/simple/price",
                                       {"ids": gecko, "vs_currencies": "usd"}, {})
                price = float((data.get(gecko) or {}).get("usd") or 0)
            except (aiohttp.ClientError, TimeoutError, ValueError):
                price = 0.0
        if price <= 0:
            raise UsdtError("Не удалось узнать курс. Попробуйте через минуту или оплатите в USDT.")
        self._rates[asset] = (price, time.monotonic())
        return price

    # --- счёт ---------------------------------------------------------------------------
    async def unique_units(self, cents: int, method: str, rate: float | None = None) -> int:
        """Сумма плюс «копейки», которых нет у других неоплаченных счетов того же актива."""
        m = METHODS[method]
        same = [k for k, v in METHODS.items() if v["asset"] == m["asset"]]
        rows = await self.db.all(f"SELECT external_id FROM invoices WHERE status = 'active' AND provider IN "
                                 f"({','.join('?' * len(same))})", *same)
        taken = {invoice_units(r) for r in rows}
        if m["asset"] == "usdt":
            base, step = cents * 10_000, 100                       # 10.0001 … 10.0099 USDT
        else:
            amount = cents / 100 / rate                            # в монетах
            unit = 10 ** (m["decimals"] - 2) if m["asset"] == "ton" else 100  # TON - до 0.01, BTC - до 100 сат
            base = math.ceil(amount * 10 ** m["decimals"] / unit) * unit
            step = 10 ** (m["decimals"] - 4) if m["asset"] == "ton" else 1   # TON 0.0001…0.0099, BTC 1…99 сат
        for _ in range(300):
            units = base + random.randint(1, 99) * step
            if units not in taken:
                return units
        return base + random.randint(100, 999) * step

    async def create(self, user_id: int, cents: int, method: str, intent: dict | None = None) -> dict:
        if method not in self.methods:
            raise UsdtError("Этот способ оплаты сейчас не принимается")
        if cents < MIN_TOPUP:
            raise UsdtError(f"Минимальное пополнение {money(MIN_TOPUP)}")
        if cents > MAX_TOPUP:
            raise UsdtError(f"Максимальное пополнение {money(MAX_TOPUP)}")
        active = await self.db.value("SELECT COUNT(*) FROM invoices WHERE user_id = ? AND status = 'active'",
                                     user_id, default=0)
        if active >= 5:
            raise UsdtError("У вас уже есть неоплаченные счета. Оплатите их или подождите")
        m = METHODS[method]
        rate = await self.rate(m["asset"]) if m["asset"] != "usdt" else None
        units = await self.unique_units(cents, method, rate)
        meta = dict(intent or {})
        if rate:
            meta["_rate"] = rate
        invoice_id = await self.db.run(
            "INSERT INTO invoices(user_id, provider, amount, intent, created_at) VALUES (?, ?, ?, ?, ?)",
            user_id, method, cents, json.dumps(meta), now())
        await self.db.run("UPDATE invoices SET external_id = ? WHERE id = ?", f"{units}#{invoice_id}", invoice_id)
        return self.view(await self.db.one("SELECT * FROM invoices WHERE id = ?", invoice_id))

    def view(self, row: Any) -> dict:
        m = method_of(row)
        method = str(row["provider"]) if str(row["provider"]) in METHODS else "usdt_ton"
        units = invoice_units(row)
        try:
            rate = json.loads(row["intent"] or "{}").get("_rate")
        except (TypeError, ValueError):
            rate = None
        amount = fmt_units(units, m["decimals"])
        return {"id": row["id"], "provider": method, "method": method, "net": m["net"], "net_title": m["net_title"],
                "title": m["title"], "ticker": m["ticker"], "amount": row["amount"], "crypto": amount,
                "usdt": amount, "rate": rate, "address": self.address(m["net"]),
                "comment": f"BC{row['id']}" if m["net"] == "ton" else "", "status": row["status"],
                "expires_at": row["created_at"] + m["ttl"]}

    def instructions(self, inv: dict) -> str:
        """Текст счёта для Telegram (HTML)."""
        lines = [f"🧾 <b>Счёт #{inv['id']} на {money(inv['amount'])}</b>\n",
                 f"Переведите <b>ровно {inv['crypto']} {inv['ticker']}</b> в сети <b>{inv['net_title']}</b>:",
                 f"<code>{inv['address']}</code>"]
        if inv["comment"]:
            lines.append(f"Комментарий, если кошелёк позволяет: <code>{inv['comment']}</code>")
        if inv.get("rate"):
            lines.append(f"Курс: 1 {inv['ticker']} = ${inv['rate']:,.2f}, сумма держится 2 часа.".replace(",", " "))
        lines += ["", f"⚠️ Только {inv['ticker']} и только в этой сети, иначе деньги потеряются. Сумма должна совпасть "
                      "до последней цифры: по ней я узнаю ваш платёж. С биржи учитывайте её комиссию: на адрес должно "
                      f"прийти ровно {inv['crypto']} {inv['ticker']}.",
                  ("Bitcoin засчитывается после 1 подтверждения сети, обычно 10–30 минут. " if inv["ticker"] == "BTC" else "")
                  + "Проверяю сам каждые полминуты. Как только перевод подтвердится, баланс пополнится и придёт уведомление."]
        return "\n".join(lines)

    # --- проверка поступлений --------------------------------------------------------------
    async def _get(self, url: str, params: dict, headers: dict) -> Any:
        if not self._session:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        async with self._session.get(url, params=params, headers=headers) as resp:
            if resp.status != 200:
                text = await resp.text()
                log.warning("%s ответил %s: %s", url.split("/")[2], resp.status, text[:200])
                return {}
            return await resp.json(content_type=None)

    async def fetch_ton(self, since: int) -> list[dict]:
        """USDT (jetton) на TON-адрес."""
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
            out.append({"tx": "ton:" + t["transaction_hash"], "amount": amount, "net": "TON", "asset": "usdt",
                        "time": int(t.get("transaction_now") or 0),
                        "comment": extract_comment(t.get("decoded_forward_payload"))})
        return out

    async def fetch_ton_coin(self, since: int) -> list[dict]:
        """Сами TON на TON-адрес (входящие сообщения с суммой; служебные копейки jetton-уведомлений отсеиваем)."""
        headers = {"X-API-Key": self.toncenter_key} if self.toncenter_key else {}
        data = await self._get(f"{self.toncenter_url}/transactions", {
            "account": self.ton_address, "start_utime": since, "limit": 100, "sort": "desc"}, headers)
        out = []
        for t in data.get("transactions", []) or []:
            msg = t.get("in_msg") or {}
            desc = t.get("description") or {}
            if not msg.get("source") or desc.get("aborted") or not t.get("hash"):
                continue
            try:
                value = int(msg.get("value") or 0)
            except (TypeError, ValueError):
                continue
            decoded = (msg.get("message_content") or {}).get("decoded") or {}
            if value < 10_000_000 or (decoded and decoded.get("type") not in (None, "text_comment")):
                continue  # меньше 0.01 TON или служебное сообщение (уведомление о jetton и т. п.)
            out.append({"tx": "toncoin:" + t["hash"], "amount": value, "net": "TON", "asset": "ton",
                        "time": int(t.get("now") or 0), "comment": decoded.get("comment") or ""})
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
            out.append({"tx": "tron:" + t["transaction_id"], "amount": amount, "net": "TRC20", "asset": "usdt",
                        "time": int(t.get("block_timestamp", 0)) // 1000, "comment": ""})
        return out

    async def fetch_btc(self, since: int) -> list[dict]:
        """Подтверждённые входящие на BTC-адрес (mempool.space, запасной - blockstream.info)."""
        data: Any = None
        for base in self.btc_urls:
            try:
                data = await self._get(f"{base}/address/{self.btc_address}/txs", {}, {})
            except (aiohttp.ClientError, TimeoutError, ValueError):
                data = None
            if isinstance(data, list):
                break
        out = []
        for t in data or []:
            status = t.get("status") or {}
            if not status.get("confirmed") or int(status.get("block_time") or 0) < since:
                continue
            value = sum(int(o.get("value") or 0) for o in t.get("vout") or []
                        if o.get("scriptpubkey_address") == self.btc_address)
            if value > 0 and t.get("txid"):
                out.append({"tx": "btc:" + t["txid"], "amount": value, "net": "BTC", "asset": "btc",
                            "time": int(status.get("block_time") or 0), "comment": ""})
        return out

    async def poll(self, force: bool = False) -> list[dict]:
        """Проверить поступления. Возвращает зачисленные счета (для уведомлений и покупок)."""
        if not self.enabled or (not force and time.monotonic() - self._last_check < CHECK_EVERY):
            return []
        self._last_check = time.monotonic()
        providers = list(METHODS)
        marks = ",".join("?" * len(providers))
        for key, m in METHODS.items():
            await self.db.run("UPDATE invoices SET status = 'expired' WHERE provider = ? AND status = 'active' "
                              "AND created_at < ?", key, now() - m["ttl"])
        active = await self.db.all(f"SELECT * FROM invoices WHERE status = 'active' AND provider IN ({marks})",
                                   *providers)
        if not active:
            return []
        since = min(r["created_at"] for r in active) - 300
        assets = {method_of(r)["asset"] for r in active}
        sources = []
        if "usdt" in assets:
            sources += [(self.ton_address, self.fetch_ton, "toncenter"), (self.tron_address, self.fetch_tron, "TronGrid")]
        if "ton" in assets:
            sources.append((self.ton_address, self.fetch_ton_coin, "toncenter"))
        if "btc" in assets:
            sources.append((self.btc_address, self.fetch_btc, "mempool.space"))
        transfers: list[dict] = []
        for enabled, fetch, name in sources:
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
            ticker = {"usdt": "USDT", "ton": "TON", "btc": "BTC"}[t.get("asset", "usdt")]
            decimals = {"usdt": 6, "ton": 9, "btc": 8}[t.get("asset", "usdt")]
            if inv is None:
                self.notifier.owners(f"💸 Пришёл платёж {fmt_units(t['amount'], decimals)} {ticker} ({t['net']}), но он "
                                     f"не подошёл ни к одному счёту.\nТранзакция: <code>{t['tx']}</code>\nЕсли клиент "
                                     "написал в поддержку, зачислите вручную: веб-админка → Клиенты → баланс.")
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
