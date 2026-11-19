"""Пополнение напрямую в USDT (TON и TRC20): точная сумма, комментарий, разовое зачисление, покупка после оплаты."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from aiogram import Bot, Dispatcher  # noqa: E402
from aiogram.client.default import DefaultBotProperties  # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from fake_tg import FakeSession, callback_update, message_update, user  # noqa: E402

OWNER, CLIENT = 1, 100
TON, TRON = "UQ_test_ton_address", "TTestTronAddress"


class UsdtTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ.update(HUB_BOT_TOKEN="1:x", OWNER_IDS=str(OWNER), SECRET_KEY="s" * 40, DATA_DIR=self.tmp.name,
                          PUBLIC_URL="https://example.com", CRYPTOPAY_TOKEN="", USE_ZYGOTE="0",
                          PAY_TON_ADDRESS=TON, PAY_TRON_ADDRESS=TRON)
        from baksi.__main__ import build
        from baksi.config import load_config
        from baksi.hub.handlers import router
        from baksi.hub.ops import router as ops_router
        from baksi.hub.security import router as security_router
        import baksi.hub.middleware as mw
        from baksi.hub.middleware import Guard
        mw.FLOOD_INTERVAL = 0
        self.app = await build(load_config(Path("/nonexistent")))
        self.app.hub_username = "hub_bot"
        self.u = self.app.usdt
        self.transfers = []

        async def fake_ton(since):
            return [t for t in self.transfers if t["net"] == "TON"]

        async def fake_tron(since):
            return [t for t in self.transfers if t["net"] == "TRC20"]
        self.u.fetch_ton, self.u.fetch_tron = fake_ton, fake_tron
        self.session = FakeSession()
        self.bot = Bot("123:abc", session=self.session, default=DefaultBotProperties(parse_mode="HTML"))
        self.dp = Dispatcher(storage=MemoryStorage(), app=self.app)
        guard = Guard(self.app)
        self.dp.message.outer_middleware(guard)
        self.dp.callback_query.outer_middleware(guard)
        for r in (security_router, ops_router, router):
            r._parent_router = None
            self.dp.include_router(r)
        await self.app.billing.ensure_user(CLIENT, "c", "C")

    async def asyncTearDown(self):
        await self.app.db.close()
        self.tmp.cleanup()
        for k in ("PAY_TON_ADDRESS", "PAY_TRON_ADDRESS"):
            os.environ.pop(k, None)

    def pay(self, units, net="TON", comment="", tx=None):
        self.transfers.append({"tx": tx or f"{net}:{len(self.transfers)}", "amount": units, "net": net,
                               "time": int(time.time()), "comment": comment})

    def notes(self, chat_id):
        return [o.text for o in list(self.app.notifier.queue._queue) if o.chat_id == chat_id]

    async def balance(self):
        return await self.app.db.value("SELECT balance FROM users WHERE id = ?", CLIENT)

    async def test_unique_amount_and_exact_match(self):
        from baksi.usdtpay import usdt
        a = await self.u.create(CLIENT, 1000, "ton")
        b = await self.u.create(CLIENT, 1000, "trc20")
        self.assertNotEqual(a["usdt"], b["usdt"], "у каждого счёта своя точная сумма")
        self.assertTrue(10 < float(a["usdt"]) < 10.01, a["usdt"])
        self.assertEqual(a["address"], TON)
        self.assertEqual(a["comment"], f"BC{a['id']}")
        self.assertEqual(b["comment"], "")
        self.assertEqual(usdt(10003700), "10.0037")
        # перевод «чужой» суммы не зачисляется, точная - зачисляется, в любой сети
        self.pay(10_000_000, "TON")
        units_b = int(float(b["usdt"]) * 1_000_000 + 0.5)
        self.pay(units_b, "TON")
        paid = await self.u.poll(force=True)
        self.assertEqual([p["id"] for p in paid], [b["id"]], "счёт TRC20 оплачен переводом в TON - тоже засчитан")
        self.assertEqual(await self.balance(), 1000)
        self.assertTrue(any("не подошёл ни к одному счёту" in t for t in self.notes(OWNER)))
        # повторная проверка не зачисляет второй раз
        self.assertEqual(await self.u.poll(force=True), [])
        self.assertEqual(await self.balance(), 1000)

    async def test_comment_match_and_overpay(self):
        inv = await self.u.create(CLIENT, 2500, "ton")
        self.pay(25_500_000, "TON", comment=f"BC {inv['id']}")
        paid = await self.u.poll(force=True)
        self.assertEqual([p["id"] for p in paid], [inv["id"]], "по комментарию BC<номер>, сумма не меньше счёта")
        self.assertEqual(await self.balance(), 2500)
        inv2 = await self.u.create(CLIENT, 2500, "ton")
        self.pay(24_000_000, "TON", comment=f"BC{inv2['id']}")
        self.assertEqual(await self.u.poll(force=True), [], "меньше суммы счёта - не зачисляем")

    async def test_expiry_and_cryptobot_expiry_does_not_touch_usdt(self):
        inv = await self.u.create(CLIENT, 1000, "trc20")
        await self.app.db.run("UPDATE invoices SET created_at = created_at - 2 * 3600 WHERE id = ?", inv["id"])
        await self.app.db.run("UPDATE invoices SET status = 'expired' WHERE provider = 'cryptobot' AND status = 'active' "
                              "AND created_at < ?", int(time.time()) - 3600)
        self.assertEqual(await self.app.db.value("SELECT status FROM invoices WHERE id = ?", inv["id"]), "active")
        await self.app.db.run("UPDATE invoices SET created_at = created_at - 23 * 3600 WHERE id = ?", inv["id"])
        await self.u.poll(force=True)
        self.assertEqual(await self.app.db.value("SELECT status FROM invoices WHERE id = ?", inv["id"]), "expired")

    async def test_hub_flow_method_choice_and_check(self):
        await self.dp.feed_update(self.bot, callback_update(CLIENT, user(CLIENT), "wallet"))
        await self.dp.feed_update(self.bot, callback_update(CLIENT, user(CLIENT), "top:1000"))
        markup = self.session.sent[-1].markup
        datas = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("tpv:usdt_ton:1000", datas)
        self.assertIn("tpv:usdt_trc20:1000", datas)
        await self.dp.feed_update(self.bot, callback_update(CLIENT, user(CLIENT), "tpv:usdt_trc20:1000"))
        text = self.session.sent[-1].text
        row = await self.app.db.one("SELECT * FROM invoices ORDER BY id DESC LIMIT 1")
        inv = self.u.view(row)
        self.assertIn(f"ровно {inv['usdt']} USDT", text)
        self.assertIn(TRON, text)
        self.assertIn("TRC20", text)
        await self.dp.feed_update(self.bot, callback_update(CLIENT, user(CLIENT), f"uchk:{inv['id']}"))
        self.assertIn("Пока не вижу", self.session.answers[-1])
        self.pay(int(float(inv["usdt"]) * 1_000_000 + 0.5), "TRC20")
        await self.dp.feed_update(self.bot, callback_update(CLIENT, user(CLIENT), f"uchk:{inv['id']}"))
        self.assertIn("оплачен", self.session.sent[-1].text)
        self.assertEqual(await self.balance(), 1000)
        self.assertTrue(any("Баланс пополнен" in t for t in self.notes(CLIENT)))

    async def test_topup_and_buy_runs_purchase(self):
        await self.app.db.run("UPDATE plans SET price = 3000 WHERE product = 'express' AND days = 30")
        await self.dp.feed_update(self.bot, callback_update(CLIENT, user(CLIENT), "tpi:express:30"))
        await self.dp.feed_update(self.bot, callback_update(CLIENT, user(CLIENT), "tpv:usdt_ton:3000"))
        row = await self.app.db.one("SELECT * FROM invoices ORDER BY id DESC LIMIT 1")
        self.assertIn('"purchase"', row["intent"], "намерение купить сохранилось через выбор сети")
        inv = self.u.view(row)
        self.pay(int(float(inv["usdt"]) * 1_000_000 + 0.5), "TON")
        from baksi.jobs import handle_paid
        for done in await self.u.poll(force=True):
            await handle_paid(self.app, done)
        self.assertEqual(await self.app.db.value("SELECT COUNT(*) FROM instances WHERE user_id = ?", CLIENT), 1)
        self.assertEqual(await self.balance(), 0)

    async def test_web_topup_usdt(self):
        from baksi.web.app import create_web
        from baksi.web.security import create_session
        token = await create_session(self.app, CLIENT, "t")
        c = TestClient(create_web(self.app), base_url="https://testserver")
        hdr = {"x-baksi": "1", "authorization": f"Bearer {token}"}
        cfg = c.get("/api/config").json()
        self.assertEqual(cfg["payments"]["usdt"], ["ton", "trc20"])
        r = c.post("/api/topup", json={"amount": 1500, "method": "usdt_ton"}, headers=hdr)
        self.assertEqual(r.status_code, 200, r.text)
        inv = r.json()
        self.assertEqual(inv["address"], TON)
        self.assertEqual(c.get(f"/api/invoices/{inv['id']}", headers=hdr).json()["status"], "active")
        self.pay(int(float(inv["usdt"]) * 1_000_000 + 0.5), "TON")
        self.assertEqual(c.post(f"/api/invoices/{inv['id']}/check", headers=hdr).json()["status"], "paid")
        self.assertEqual(c.post("/api/topup", json={"amount": 50, "method": "usdt_ton"}, headers=hdr).status_code, 400)


if __name__ == "__main__":
    unittest.main()
