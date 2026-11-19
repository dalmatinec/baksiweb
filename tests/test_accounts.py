"""Аккаунты: регистрация на сайте с подтверждением в Telegram, вход по паролю, ключ, запасной Telegram,
перенос с задержкой 24 часа и отменой, перенос всех данных на новый Telegram ID, ручной перенос."""
import os
import sys
import tempfile
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

OWNER, OLD, NEW, RESERVE, STRANGER = 1, 100, 200, 300, 400


class AccountTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ.update(HUB_BOT_TOKEN="1:x", OWNER_IDS=str(OWNER), SECRET_KEY="s" * 40, DATA_DIR=self.tmp.name,
                          PUBLIC_URL="https://example.com", CRYPTOPAY_TOKEN="", USE_ZYGOTE="0")
        from baksi.__main__ import build
        from baksi.config import load_config
        from baksi.hub.handlers import router
        from baksi.hub.security import router as security_router
        import baksi.hub.middleware as mw
        from baksi.hub.middleware import Guard
        mw.FLOOD_INTERVAL = 0
        self.app = await build(load_config(Path("/nonexistent")))
        self.app.hub_username = "hub_bot"
        self.acc = self.app.accounts
        self.session = FakeSession()
        self.bot = Bot("123:abc", session=self.session, default=DefaultBotProperties(parse_mode="HTML"))
        self.dp = Dispatcher(storage=MemoryStorage(), app=self.app)
        guard = Guard(self.app)
        self.dp.message.outer_middleware(guard)
        self.dp.callback_query.outer_middleware(guard)
        router._parent_router = None
        security_router._parent_router = None
        self.dp.include_router(security_router)
        self.dp.include_router(router)
        self.restarted: list[int] = []

        async def fake_restart(iid):
            self.restarted.append(iid)
        self.app.sup.restart = fake_restart

    async def asyncTearDown(self):
        await self.app.db.close()
        self.tmp.cleanup()

    async def feed(self, upd):
        await self.dp.feed_update(self.bot, upd)

    def notes(self, chat_id) -> list[str]:
        items = list(self.app.notifier.queue._queue)
        return [o.text for o in items if o.chat_id == chat_id]

    def texts(self, chat_id) -> str:
        return "\n".join(s.text or "" for s in self.session.sent if s.chat_id == chat_id)

    async def make_account_with_bot(self, uid=OLD, balance=1500):
        await self.app.billing.ensure_user(uid, "old_user", "Old")
        await self.app.billing.admin_adjust(OWNER, uid, balance, "")
        await self.app.db.run("UPDATE plans SET price = 500 WHERE product = 'express' AND days = 30")
        iid = await self.app.billing.purchase(uid, "express", 30, None, None)
        return iid

    async def test_password_rules_and_login(self):
        from baksi.accounts import AccountError, hash_password, verify_password
        self.assertTrue(verify_password("secret-pass-1", hash_password("secret-pass-1")))
        self.assertFalse(verify_password("wrong", hash_password("secret-pass-1")))
        await self.app.billing.ensure_user(OLD, "u", "U")
        for bad_login, bad_pass in (("ab", "goodpass1"), ("ok_login", "short"), ("ok_login", "12345678")):
            with self.assertRaises(AccountError):
                await self.acc.set_password(OLD, bad_login, bad_pass)
        await self.acc.set_password(OLD, "Daniyar", "goodpass1")
        self.assertEqual(await self.acc.login("daniyar", "goodpass1"), OLD, "логин без учёта регистра")
        await self.app.billing.ensure_user(NEW, "n", "N")
        with self.assertRaises(AccountError):
            await self.acc.set_password(NEW, "DANIYAR", "otherpass1")
        for _ in range(8):
            with self.assertRaises(AccountError):
                await self.acc.login("daniyar", "wrong-pass")
        with self.assertRaises(AccountError) as ctx:
            await self.acc.login("daniyar", "goodpass1")
        self.assertIn("попыток", str(ctx.exception), "после 8 ошибок вход по паролю на паузе")
        self.assertTrue(any("неверно ввёл пароль" in t for t in self.notes(OLD)))

    async def test_signup_on_site_confirmed_in_telegram(self):
        from baksi.web.app import create_web
        client = TestClient(create_web(self.app), base_url="https://testserver")
        hdr = {"x-baksi": "1"}
        r = client.post("/api/auth/signup", json={"login": "newbie", "password": "goodpass1", "password2": "nope"},
                        headers=hdr)
        self.assertEqual(r.status_code, 400)
        r = client.post("/api/auth/signup", json={"login": "newbie", "password": "goodpass1", "password2": "goodpass1"},
                        headers=hdr)
        self.assertEqual(r.status_code, 200, r.text)
        token, link = r.json()["token"], r.json()["link"]
        self.assertTrue(link.startswith("https://t.me/hub_bot?start=su_"))
        self.assertEqual(client.get(f"/api/auth/signup/{token}").json(), {"pending": True})
        # человек открывает ссылку в Telegram
        await self.feed(message_update(NEW, user(NEW, "Newbie"), "/start " + link.split("start=")[1]))
        self.assertIn("Регистрация завершена", self.texts(NEW))
        r = client.get(f"/api/auth/signup/{token}")
        self.assertTrue(r.json()["ok"])
        me = client.get("/api/me").json()
        self.assertEqual(me["user"]["id"], NEW)
        self.assertEqual(me["security"]["login"], "newbie")
        self.assertEqual(client.get(f"/api/auth/signup/{token}").status_code, 410, "вход по токену один раз")
        # вход по паролю
        client2 = TestClient(create_web(self.app), base_url="https://testserver")
        self.assertEqual(client2.post("/api/auth/login", json={"login": "newbie", "password": "bad"},
                                      headers=hdr).status_code, 401)
        r = client2.post("/api/auth/login", json={"login": "newbie", "password": "goodpass1"}, headers=hdr)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(client2.get("/api/me").json()["user"]["id"], NEW)

    async def test_recovery_key_transfer_after_24h(self):
        iid = await self.make_account_with_bot()
        await self.acc.set_password(OLD, "oldlogin", "goodpass1")
        await self.feed(callback_update(OLD, user(OLD), "sec:key"))
        key = [s for s in self.session.sent if s.chat_id == OLD and "BAKSI-" in (s.text or "")][-1].text
        key = key[key.index("BAKSI-"):key.index("BAKSI-") + 25]
        # с нового аккаунта: неверный ключ, потом верный
        await self.feed(message_update(NEW, user(NEW, "New", "new_acc"), "/recover"))
        await self.feed(message_update(NEW, user(NEW, "New", "new_acc"), "BAKSI-AAAA-BBBB-CCCC-DDDD"))
        self.assertIn("Ключ не подошёл", self.texts(NEW))
        await self.feed(message_update(NEW, user(NEW, "New", "new_acc"), "/recover"))
        await self.feed(message_update(NEW, user(NEW, "New", "new_acc"), key.lower().replace("-", " ")))
        self.assertIn("Запрос на перенос принят", self.texts(NEW))
        warn = [t for t in self.notes(OLD) if "переносят на другой Telegram" in t]
        self.assertTrue(warn and "@new_acc" in warn[0], "прежний аккаунт предупреждён")
        self.assertEqual(await self.acc.execute_due(), 0, "раньше 24 часов перенос не выполняется")
        await self.app.db.run("UPDATE transfers SET execute_at = 0")
        self.assertEqual(await self.acc.execute_due(), 1)
        # всё на новом ID
        self.assertEqual(await self.app.db.value("SELECT user_id FROM instances WHERE id = ?", iid), NEW)
        self.assertEqual(await self.app.db.value("SELECT balance FROM users WHERE id = ?", NEW), 1000)
        self.assertIsNone(await self.app.db.one("SELECT 1 FROM users WHERE id = ?", OLD))
        self.assertEqual(await self.acc.login("oldlogin", "goodpass1"), NEW, "логин и пароль переехали")
        self.assertEqual(self.restarted, [iid], "боты перезапущены с новым владельцем")
        self.assertTrue(await self.app.db.value("SELECT COUNT(*) FROM ledger WHERE user_id = ?", NEW))
        done = [t for t in self.notes(NEW) if "Аккаунт перенесён" in t][0]
        self.assertIn("Новый ключ восстановления", done)
        from baksi.accounts import AccountError
        with self.assertRaises(AccountError):
            await self.acc.recover_with_key(STRANGER, key)

    async def test_owner_cancels_and_key_rotates(self):
        await self.make_account_with_bot()
        key = await self.acc.issue_recovery_key(OLD)
        row = await self.acc.recover_with_key(NEW, key)
        await self.feed(callback_update(OLD, user(OLD), f"trc:{row['id']}"))
        self.assertEqual(await self.app.db.value("SELECT status FROM transfers WHERE id = ?", row["id"]), "cancelled")
        self.assertIn("Перенос аккаунта отменён", self.session.sent[-1].text)
        self.assertIn("ключ восстановления мог попасть", self.session.sent[-1].text.lower())
        from baksi.accounts import AccountError
        with self.assertRaises(AccountError):
            await self.acc.recover_with_key(NEW, key)
        await self.app.db.run("UPDATE transfers SET execute_at = 0")
        self.assertEqual(await self.acc.execute_due(), 0)
        self.assertTrue(any("отменён" in t for t in self.notes(NEW)))

    async def test_reserve_telegram(self):
        await self.make_account_with_bot()
        await self.feed(callback_update(OLD, user(OLD), "sec:rs"))
        link = [s for s in self.session.sent if s.chat_id == OLD and "start=rs_" in (s.text or "")][-1].text
        payload = link[link.index("rs_"):].split("<")[0].split()[0]
        await self.feed(message_update(RESERVE, user(RESERVE, "Reserve"), f"/start {payload}"))
        self.assertIn("теперь запасной", self.texts(RESERVE))
        self.assertEqual((await self.acc.security(OLD))["reserve_id"], RESERVE)
        await self.feed(message_update(RESERVE, user(RESERVE), "/recover"))
        await self.feed(callback_update(RESERVE, user(RESERVE), f"rcv:{OLD}"))
        self.assertIn("Запрос на перенос принят", self.texts(RESERVE))
        await self.feed(callback_update(STRANGER, user(STRANGER), f"rcv:{OLD}"))
        self.assertEqual(await self.app.db.value("SELECT COUNT(*) FROM transfers"), 1, "чужой не может")
        await self.app.db.run("UPDATE transfers SET execute_at = 0")
        await self.acc.execute_due()
        self.assertEqual(await self.app.db.value("SELECT COUNT(*) FROM instances WHERE user_id = ?", RESERVE), 1)
        self.assertIsNone((await self.acc.security(RESERVE))["reserve_id"], "сам себе запасным не остаётся")

    async def test_target_with_own_bots_refused_and_admin_transfer(self):
        from baksi.accounts import AccountError
        await self.make_account_with_bot(OLD)
        await self.make_account_with_bot(NEW)
        key = await self.acc.issue_recovery_key(OLD)
        with self.assertRaises(AccountError) as ctx:
            await self.acc.recover_with_key(NEW, key)
        self.assertIn("уже есть свои боты", str(ctx.exception))
        await self.app.billing.ensure_user(STRANGER, "s", "S")  # пустой аккаунт (только нажал /start) - можно
        await self.acc.transfer_now(OLD, STRANGER, OWNER)
        self.assertEqual(await self.app.db.value("SELECT COUNT(*) FROM instances WHERE user_id = ?", STRANGER), 1)
        self.assertEqual(await self.app.db.value("SELECT method FROM transfers ORDER BY id DESC LIMIT 1"), "admin")

    async def test_key_attempts_limited(self):
        from baksi.accounts import AccountError
        for _ in range(5):
            with self.assertRaises(AccountError):
                await self.acc.recover_with_key(STRANGER, "BAKSI-AAAA-BBBB-CCCC-DDDD")
        with self.assertRaises(AccountError) as ctx:
            await self.acc.recover_with_key(STRANGER, "BAKSI-AAAA-BBBB-CCCC-DDDD")
        self.assertIn("через час", str(ctx.exception))

    async def test_nudge_once_and_reminder(self):
        await self.make_account_with_bot()
        self.assertEqual(await self.acc.nudge(), 1)
        first = [t for t in self.notes(OLD) if "Защитите доступ" in t]
        self.assertTrue(first and "BAKSI-" in first[0], "ключ выдан в первом сообщении")
        self.assertEqual(await self.acc.nudge(), 0, "повторно не пристаём")
        await self.app.db.run("UPDATE security SET nudged_at = nudged_at - 4 * 86400")
        self.assertEqual(await self.acc.nudge(), 1)
        self.assertTrue(any("всё ещё не указан" in t for t in self.notes(OLD)))
        self.assertEqual(await self.acc.nudge(), 0)

    async def test_security_screen_in_hub(self):
        await self.feed(message_update(OLD, user(OLD), "/start"))
        await self.feed(callback_update(OLD, user(OLD), "sec"))
        self.assertIn("Безопасность аккаунта", self.session.sent[-1].text)
        self.assertIn("/recover", self.session.sent[-1].text)


if __name__ == "__main__":
    unittest.main()
