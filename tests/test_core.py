"""Тесты ядра без Telegram: биллинг, промокоды, продления, супервизор с настоящими процессами."""
import asyncio
import json
import os
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from baksi.billing import Billing, BillingError, InsufficientFunds, money, parse_money  # noqa: E402
from baksi.catalog import Catalog, load_modules  # noqa: E402
from baksi.config import Config  # noqa: E402
from baksi.crypto import Vault  # noqa: E402
from baksi.db import Database, now  # noqa: E402
from baksi.supervisor import Supervisor  # noqa: E402

GOOD_BOT = '''
import json, sys, time, os
print("@@baksi " + json.dumps({"event": "ready", "username": "test_bot", "name": "Test"}), flush=True)
print("hello from", os.environ["BAKSI_INSTANCE"], flush=True)
while True:
    time.sleep(0.2)
'''
HOG_BOT = '''
import time
junk = bytearray(90 * 1024 * 1024)
for i in range(0, len(junk), 4096):
    junk[i] = 1
while True:
    time.sleep(0.2)
'''
CRASH_BOT = '''
import sys
print("boom", flush=True)
sys.exit(3)
'''


def make_module(root: Path, slug: str, code: str, **extra) -> None:
    d = root / slug
    (d / "bot").mkdir(parents=True)
    (d / "bot" / "__init__.py").write_text("")
    (d / "bot" / "__main__.py").write_text(textwrap.dedent(code))
    (d / "manifest.json").write_text(json.dumps({"slug": slug, "title": slug.title(), **extra}))


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        mods = root / "modules"
        mods.mkdir()
        (mods / "kit").symlink_to(ROOT / "modules" / "kit")
        make_module(mods, "good", GOOD_BOT, mem_mb=60, needs_bind=True)
        make_module(mods, "crash", CRASH_BOT)
        make_module(mods, "hog", HOG_BOT, mem_mb=40)
        self.cfg = Config(brand="Test", hub_token="1:x", owner_ids=frozenset({1}), secret_key="k" * 40,
                          data_dir=root / "data", modules_dir=mods, public_url="", web_host="127.0.0.1",
                          web_port=0, cryptopay_token="", cryptopay_testnet=False, proxy_url="",
                          log_level="INFO", instance_mem_mb=100, max_instances=10, backup_keep_days=7,
                          purge_after_days=30)
        self.db = Database(self.cfg.db_path)
        await self.db.connect()
        self.catalog = Catalog(self.db, load_modules(mods, 100))
        await self.catalog.sync()
        self.billing = Billing(self.db, self.catalog)
        self.vault = Vault(self.cfg.secret_key)
        await self.billing.ensure_user(10, "client", "Client")
        await self.db.run("UPDATE plans SET price = 500 WHERE product = 'good' AND days = 30")
        await self.db.run("UPDATE plans SET price = 3000 WHERE product = 'good' AND days = 0")

    async def asyncTearDown(self):
        await self.db.close()
        self.tmp.cleanup()


class BillingTests(Base):
    async def test_money(self):
        self.assertEqual(money(123456), "$1,234.56")
        self.assertEqual(parse_money("7,5"), 750)
        self.assertIsNone(parse_money("abc"))

    async def test_unpriced_plan_not_for_sale(self):
        products = await self.catalog.products()
        self.assertEqual([p["slug"] for p in products], ["good"])  # crash без цен скрыт
        with self.assertRaises(BillingError):
            await self.billing.quote(10, "good", 7)

    async def test_purchase_and_extend(self):
        with self.assertRaises(InsufficientFunds) as ctx:
            await self.billing.purchase(10, "good", 30)
        self.assertEqual(ctx.exception.need, 500)
        await self.billing.admin_adjust(1, 10, 1000, "test")
        iid = await self.billing.purchase(10, "good", 30)
        row = await self.db.one("SELECT * FROM instances WHERE id = ?", iid)
        self.assertAlmostEqual(row["expires_at"], now() + 30 * 86400, delta=5)
        await self.billing.purchase(10, "good", 30, instance_id=iid)
        row = await self.db.one("SELECT * FROM instances WHERE id = ?", iid)
        self.assertAlmostEqual(row["expires_at"], now() + 60 * 86400, delta=5)
        self.assertEqual(await self.db.value("SELECT balance FROM users WHERE id = 10"), 0)
        total = await self.db.value("SELECT SUM(amount) FROM ledger WHERE user_id = 10")
        self.assertEqual(total, 0)

    async def test_promo_percent_once(self):
        await self.db.run("INSERT INTO promocodes(code, kind, value, max_uses, created_at) VALUES "
                          "('HALF', 'percent', 50, 10, ?)", now())
        await self.billing.admin_adjust(1, 10, 1000, "")
        q = await self.billing.quote(10, "good", 30, "half")
        self.assertEqual(q.total, 250)
        await self.billing.purchase(10, "good", 30, "half")
        q = await self.billing.quote(10, "good", 30, "half")
        self.assertEqual(q.promo_error, "Вы уже использовали этот промокод")

    async def test_trial_once(self):
        iid = await self.billing.start_trial(10, "good")
        row = await self.db.one("SELECT * FROM instances WHERE id = ?", iid)
        self.assertEqual(row["trial"], 1)
        with self.assertRaises(BillingError):
            await self.billing.start_trial(10, "good")

    async def test_auto_renew_and_expire(self):
        await self.billing.admin_adjust(1, 10, 1000, "")
        iid = await self.billing.purchase(10, "good", 30)
        await self.db.run("UPDATE instances SET expires_at = ? WHERE id = ?", now() - 1, iid)
        events = await self.billing.process_expired()
        self.assertEqual(events[0]["kind"], "renewed")
        await self.db.run("UPDATE instances SET expires_at = ? WHERE id = ?", now() - 1, iid)
        events = await self.billing.process_expired()
        self.assertEqual(events[0]["kind"], "renew_failed")
        self.assertEqual(events[1]["kind"], "expired")
        self.assertIsNotNone(await self.db.value("SELECT expired_at FROM instances WHERE id = ?", iid))

    async def test_forever(self):
        await self.billing.admin_adjust(1, 10, 3000, "")
        iid = await self.billing.purchase(10, "good", 0)
        row = await self.db.one("SELECT * FROM instances WHERE id = ?", iid)
        self.assertEqual(row["forever"], 1)
        self.assertIsNone(row["expires_at"])

    async def test_invoice_paid_idempotent_with_referral(self):
        await self.billing.ensure_user(20, "friend", "F", ref_by=10)
        await self.db.set_setting("referral_percent", 10)
        inv = await self.db.run("INSERT INTO invoices(user_id, provider, external_id, amount, intent, created_at) "
                                "VALUES (20, 'cryptobot', 'x1', 1000, ?, ?)",
                                json.dumps({"action": "purchase", "product": "good", "days": 30}), now())
        first = await self.billing.mark_invoice_paid(inv)
        second = await self.billing.mark_invoice_paid(inv)
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(await self.db.value("SELECT balance FROM users WHERE id = 20"), 1000)
        self.assertEqual(await self.db.value("SELECT balance FROM users WHERE id = 10"), 100)
        iid = await self.billing.run_intent(first)
        self.assertIsNotNone(iid)
        self.assertEqual(await self.db.value("SELECT balance FROM users WHERE id = 20"), 500)


class SupervisorTests(Base):
    async def _instance(self, product: str) -> int:
        await self.billing.admin_adjust(1, 10, 10000, "")
        await self.db.run("UPDATE plans SET price = 100 WHERE days = 30")
        iid = await self.billing.purchase(10, product, 30)
        await self.db.run("UPDATE instances SET token_enc = ? WHERE id = ?",
                          self.vault.encrypt("123456:" + "A" * 35), iid)
        return iid

    async def _wait(self, cond, timeout=8.0):
        t = time.time()
        while time.time() - t < timeout:
            if await cond():
                return True
            await asyncio.sleep(0.1)
        return False

    async def test_runs_reports_and_stops(self):
        sup = Supervisor(self.cfg, self.db, self.catalog, self.vault)
        events = []

        async def on_event(iid, ev):
            events.append(ev)
        sup.on_event = on_event
        iid = await self._instance("good")
        task = asyncio.create_task(sup.run())
        try:
            self.assertTrue(await self._wait(lambda: asyncio.sleep(0, bool(events))))
            self.assertEqual(events[0]["event"], "ready")
            self.assertTrue(await self._wait(lambda: self.db.value(
                "SELECT status = 'waiting_bind' FROM instances WHERE id = ?", iid)))
            platform = json.loads((self.cfg.instances_dir / str(iid) / "platform.json").read_text())
            self.assertEqual(platform["owner_ids"], [10])
            self.assertTrue(any("hello from" in line for line in sup.logs(iid)))
            await self.db.run("UPDATE instances SET paused = 1 WHERE id = ?", iid)
            sup.poke()
            self.assertTrue(await self._wait(lambda: self.db.value(
                "SELECT status = 'stopped' FROM instances WHERE id = ?", iid)))
            self.assertFalse(sup.procs[iid].running)
        finally:
            await sup.shutdown()
            task.cancel()

    async def test_crash_backoff(self):
        sup = Supervisor(self.cfg, self.db, self.catalog, self.vault)
        iid = await self._instance("crash")
        task = asyncio.create_task(sup.run())
        try:
            self.assertTrue(await self._wait(lambda: self.db.value(
                "SELECT status = 'restarting' FROM instances WHERE id = ?", iid)))
            self.assertGreaterEqual(sup.procs[iid].restarts, 1)
            self.assertEqual(sup.procs[iid].last_exit, 3)
        finally:
            await sup.shutdown()
            task.cancel()

    async def test_memory_limit(self):
        problems = []
        sup = Supervisor(self.cfg, self.db, self.catalog, self.vault)

        async def on_problem(iid, kind, details):
            problems.append(kind)
        sup.on_problem = on_problem
        await self._instance("hog")
        try:
            await sup.reconcile()
            await asyncio.sleep(2)
            await sup.watchdog()
            self.assertIn("memory", problems)
        finally:
            await sup.shutdown()


if __name__ == "__main__":
    unittest.main()
