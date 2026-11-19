"""Пополнение баланса через Crypto Pay (@CryptoBot).

Работает и без домена: платформа сама опрашивает неоплаченные счета каждые 15 секунд.
Если домен подключён, можно включить вебхук в @CryptoBot → Crypto Pay → Webhooks:
https://<домен>/api/cryptopay/webhook - тогда зачисление мгновенное. Подпись вебхука проверяется.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

import aiohttp

from .billing import Billing, money
from .db import Database, now

log = logging.getLogger("baksi.cryptopay")

ASSETS = "USDT,TON,BTC,ETH,LTC,TRX,USDC"
INVOICE_TTL = 3600
MIN_TOPUP = 100        # $1
MAX_TOPUP = 1_000_000  # $10 000


class PaymentError(Exception):
    pass


class CryptoPay:
    def __init__(self, token: str, testnet: bool, db: Database, billing: Billing, brand: str) -> None:
        self.token = token
        self.base = "https://testnet-pay.crypt.bot/api/" if testnet else "https://pay.crypt.bot/api/"
        self.db = db
        self.billing = billing
        self.brand = brand
        self._session: aiohttp.ClientSession | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    async def _call(self, method: str, **params: Any) -> Any:
        if not self._session:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        try:
            async with self._session.post(self.base + method, json=params,
                                          headers={"Crypto-Pay-API-Token": self.token}) as resp:
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise PaymentError(f"CryptoBot недоступен: {exc}") from exc
        if not data.get("ok"):
            raise PaymentError(f"CryptoBot: {data.get('error')}")
        return data["result"]

    async def close(self) -> None:
        if self._session:
            await self._session.close()

    async def create(self, user_id: int, amount: int, intent: dict | None = None) -> dict:
        if not self.enabled:
            raise PaymentError("Оплата криптовалютой пока не подключена")
        if amount < MIN_TOPUP:
            raise PaymentError(f"Минимальное пополнение {money(MIN_TOPUP)}")
        if amount > MAX_TOPUP:
            raise PaymentError(f"Максимальное пополнение {money(MAX_TOPUP)}")
        active = await self.db.value("SELECT COUNT(*) FROM invoices WHERE user_id = ? AND status = 'active'",
                                     user_id, default=0)
        if active >= 5:
            raise PaymentError("У вас уже есть неоплаченные счета. Оплатите их или подождите час")
        invoice_id = await self.db.run(
            "INSERT INTO invoices(user_id, provider, amount, intent, created_at) VALUES (?, 'cryptobot', ?, ?, ?)",
            user_id, amount, json.dumps(intent or {}), now())
        try:
            result = await self._call(
                "createInvoice", currency_type="fiat", fiat="USD", amount=f"{amount / 100:.2f}",
                accepted_assets=ASSETS, description=f"{self.brand}: пополнение баланса на {money(amount)}",
                payload=str(invoice_id), expires_in=INVOICE_TTL, allow_comments=False, allow_anonymous=True)
        except PaymentError:
            await self.db.run("DELETE FROM invoices WHERE id = ?", invoice_id)
            raise
        url = result.get("bot_invoice_url") or result.get("mini_app_invoice_url") or result.get("pay_url", "")
        await self.db.run("UPDATE invoices SET external_id = ?, pay_url = ? WHERE id = ?",
                          str(result["invoice_id"]), url, invoice_id)
        return {"id": invoice_id, "url": url, "amount": amount}

    async def poll(self) -> list[dict]:
        """Проверить неоплаченные счета. Возвращает зачисленные (для уведомлений)."""
        if not self.enabled:
            return []
        rows = await self.db.all("SELECT * FROM invoices WHERE provider = 'cryptobot' AND status = 'active' "
                                 "AND external_id IS NOT NULL ORDER BY id LIMIT 100")
        if not rows:
            return []
        by_ext = {r["external_id"]: r for r in rows}
        result = await self._call("getInvoices", invoice_ids=",".join(by_ext))
        paid = []
        for item in result.get("items", []):
            row = by_ext.get(str(item.get("invoice_id")))
            if not row:
                continue
            if item.get("status") == "paid":
                done = await self.billing.mark_invoice_paid(row["id"])
                if done:
                    paid.append(done)
            elif item.get("status") == "expired":
                await self.db.run("UPDATE invoices SET status = 'expired' WHERE id = ? AND status = 'active'",
                                  row["id"])
        # счета, о которых CryptoBot уже забыл
        await self.db.run("UPDATE invoices SET status = 'expired' WHERE provider = 'cryptobot' AND status = 'active' "
                          "AND created_at < ?",
                          now() - INVOICE_TTL - 600)
        return paid

    def verify(self, body: bytes, signature: str) -> bool:
        secret = hashlib.sha256(self.token.encode()).digest()
        expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
        return bool(signature) and hmac.compare_digest(expected, signature)

    async def webhook(self, body: bytes, signature: str) -> dict | None:
        if not self.enabled or not self.verify(body, signature):
            raise PaymentError("bad signature")
        data = json.loads(body)
        if data.get("update_type") != "invoice_paid":
            return None
        payload = data.get("payload") or {}
        if payload.get("status") != "paid":
            return None
        row = await self.db.one("SELECT * FROM invoices WHERE provider = 'cryptobot' AND external_id = ?",
                                str(payload.get("invoice_id")))
        if not row:
            return None
        return await self.billing.mark_invoice_paid(row["id"])
