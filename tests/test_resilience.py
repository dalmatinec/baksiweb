"""Надёжность: только последний бэкап на сервере, части по 45 МБ, канал бэкапов, молчащие боты, запасной хаб."""
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "modules" / "kit"))

from aiogram import Bot, Dispatcher  # noqa: E402
from aiogram.client.default import DefaultBotProperties  # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402

from fake_tg import FakeSession, callback_update, message_update, user  # noqa: E402

OWNER, CLIENT = 1, 100


class ResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ.update(HUB_BOT_TOKEN="1:x", OWNER_IDS=str(OWNER), SECRET_KEY="s" * 40, DATA_DIR=self.tmp.name,
                          PUBLIC_URL="https://example.com", CRYPTOPAY_TOKEN="", USE_ZYGOTE="0",
                          HUB_BOT_TOKEN_RESERVE="2:reserve")
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
        self.session = FakeSession()
        self.bot = Bot("123:abc", session=self.session, default=DefaultBotProperties(parse_mode="HTML"))
        await self.app.bot.session.close()
        self.app.bot = self.bot
        self.dp = Dispatcher(storage=MemoryStorage(), app=self.app)
        guard = Guard(self.app)
        self.dp.message.outer_middleware(guard)
        self.dp.callback_query.outer_middleware(guard)
        for r in (security_router, ops_router, router):
            r._parent_router = None
            self.dp.include_router(r)

    async def asyncTearDown(self):
        await self.app.db.close()
        self.tmp.cleanup()
        os.environ.pop("HUB_BOT_TOKEN_RESERVE", None)

    def notes(self, chat_id):
        return [o for o in list(self.app.notifier.queue._queue) if o.chat_id == chat_id]

    async def bot_instance(self):
        await self.app.billing.ensure_user(CLIENT, "c", "C")
        await self.app.billing.admin_adjust(OWNER, CLIENT, 5000, "")
        await self.app.db.run("UPDATE plans SET price = 500 WHERE product = 'express' AND days = 30")
        iid = await self.app.billing.purchase(CLIENT, "express", 30, None, None)
        await self.app.db.run("UPDATE instances SET token_enc = ?, bot_username = 'cl_bot', status = 'running', "
                              "last_activity_at = ? WHERE id = ?", self.app.vault.encrypt("5:tok"), int(time.time()), iid)
        return iid

    async def test_only_latest_backup_kept(self):
        first = await self.app.backups.full()
        await asyncio.sleep(1.1)
        second = await self.app.backups.full()
        self.assertFalse(first.exists(), "старый бэкап удалён")
        self.assertTrue(second.exists())
        self.assertEqual(len(self.app.backups.list()), 1)

    async def test_split_and_join(self):
        from baksi.resilience import split_file
        src = Path(self.tmp.name) / "baksi-x.zip"
        data = os.urandom(2500)
        src.write_bytes(data)
        parts = split_file(src, part_size=1000)
        self.assertEqual([p.name for p in parts], ["baksi-x.zip.001", "baksi-x.zip.002", "baksi-x.zip.003"])
        self.assertEqual(b"".join(p.read_bytes() for p in parts), data, "cat части > файл даёт исходный архив")
        self.assertEqual(split_file(src, part_size=10_000), [src], "маленький файл не режется")

    async def test_backup_goes_to_channel_in_parts(self):
        import baksi.resilience as res
        res.PART_SIZE = 3000
        try:
            await self.app.db.set_setting("backup_chat", {"id": -1001, "title": "Бэкапы"})
            path = await self.app.backups.full()
            path.write_bytes(os.urandom(7000))
            where = await res.send_backup(self.app, path)
        finally:
            res.PART_SIZE = 45 * 1024 * 1024
        self.assertIn("Бэкапы", where)
        docs = [d for d in self.session.documents if d[0] == -1001]
        self.assertEqual(len(docs), 3)
        self.assertIn("Часть 1 из 3", docs[0][2])
        self.assertIn("cat ", docs[0][2])
        self.assertEqual(list(Path(path.parent).glob("*.zip.0*")), [], "временные части удалены")

    async def test_backup_channel_connect_by_forward(self):
        from aiogram.types import Chat, Message, MessageOriginChannel, Update
        await self.dp.feed_update(self.bot, callback_update(OWNER, user(OWNER), "ops:ch"))
        msg = Message(message_id=900, date=int(time.time()), chat=Chat(id=OWNER, type="private"), from_user=user(OWNER),
                      text="пост", forward_origin=MessageOriginChannel(type="channel", date=int(time.time()),
                                                                        chat=Chat(id=-100777, type="channel", title="Мои бэкапы"),
                                                                        message_id=5))
        await self.dp.feed_update(self.bot, Update(update_id=99999, message=msg))
        self.assertEqual((await self.app.db.get_setting("backup_chat"))["id"], -100777)
        self.assertTrue(any(s.chat_id == -100777 for s in self.session.sent), "в канал ушло проверочное сообщение")

    async def test_idle_bot_warned_then_paused_and_keep(self):
        from baksi.resilience import idle_check
        iid = await self.bot_instance()
        self.assertEqual(await idle_check(self.app), (0, 0))
        await self.app.db.run("UPDATE instances SET last_activity_at = ? WHERE id = ?", int(time.time()) - 12 * 86400 - 60, iid)
        self.assertEqual(await idle_check(self.app), (1, 0))
        self.assertEqual(await idle_check(self.app), (0, 0), "предупреждение одно")
        warn = [o for o in self.notes(CLIENT) if "молчит" in o.text][0]
        self.assertIn("keep:", warn.markup.inline_keyboard[0][0].callback_data)
        # «Оставить работать» сбрасывает отсчёт
        await self.dp.feed_update(self.bot, callback_update(CLIENT, user(CLIENT), f"keep:{iid}"))
        self.assertIsNone(await self.app.db.value("SELECT idle_warned_at FROM instances WHERE id = ?", iid))
        # 14 дней тишины - пауза
        await self.app.db.run("UPDATE instances SET last_activity_at = ? WHERE id = ?", int(time.time()) - 15 * 86400, iid)
        await idle_check(self.app)
        self.assertEqual(await self.app.db.value("SELECT paused FROM instances WHERE id = ?", iid), 1)
        paused = [o for o in self.notes(CLIENT) if "поставлен на паузу" in o.text][0]
        self.assertEqual(paused.markup.inline_keyboard[0][0].callback_data, f"resume:{iid}")
        await self.app.instances.set_flag(CLIENT, iid, "paused", False)
        self.assertGreater(await self.app.db.value("SELECT last_activity_at FROM instances WHERE id = ?", iid),
                           int(time.time()) - 5, "после запуска отсчёт заново")

    async def test_activity_event_resets_and_kit_throttles(self):
        iid = await self.bot_instance()
        await self.app.db.run("UPDATE instances SET last_activity_at = 1, idle_warned_at = 5 WHERE id = ?", iid)
        await self.app.instances.on_event(iid, {"event": "activity"})
        self.assertGreater(await self.app.db.value("SELECT last_activity_at FROM instances WHERE id = ?", iid), 1000)
        import baksi_kit
        emitted = []
        orig = baksi_kit.emit
        baksi_kit.emit = lambda ev, **kw: emitted.append(ev)
        baksi_kit._last_touch = None
        try:
            for _ in range(50):
                baksi_kit.touch()
        finally:
            baksi_kit.emit = orig
        self.assertEqual(emitted, ["activity"], "не чаще раза в 10 минут")

    async def test_failover_switch_and_announce(self):
        import baksi.resilience as res
        from baksi.resilience import active_hub_token, announce_after_failover, switch_to_reserve
        stopped = []
        self.app.request_stop = lambda: stopped.append(True)
        self.app.hub_username = "old_hub"
        await switch_to_reserve(self.app)
        self.assertEqual(stopped, [True], "платформа перезапускается")
        mode, token = await active_hub_token(self.app.db, self.app.cfg)
        self.assertEqual((mode, token), ("reserve", "2:reserve"))
        # после перезапуска на запасном
        self.app.hub_mode, self.app.hub_username = "reserve", "new_hub"
        await self.app.billing.ensure_user(CLIENT, "c", "C")
        calls = []

        async def fake_via(app, text, only_owners=False):
            calls.append(text)
            return 7
        orig = res.notify_via_client_bots
        res.notify_via_client_bots = fake_via
        try:
            self.assertEqual(await announce_after_failover(self.app), 7)
            self.assertEqual(await announce_after_failover(self.app), 0, "оповещение один раз")
        finally:
            res.notify_via_client_bots = orig
        self.assertIn("@new_hub", calls[0])
        self.assertIn("@old_hub", calls[0])
        from baksi.resilience import site_notice
        self.assertIn("@new_hub", await site_notice(self.app))
        self.assertTrue(any("@new_hub" in o.text for o in self.notes(CLIENT)))

    async def test_manual_hub_replace_and_revert(self):
        import baksi.resilience as res
        import baksi.tg as tg
        from baksi.resilience import HubError, active_hub_token, announce_after_failover, replace_hub, site_notice
        stopped = []
        self.app.request_stop = lambda: stopped.append(True)
        self.app.hub_username, self.app.hub_token = "old_hub", "1:x"
        known = {"7:newtoken": tg.BotInfo(id=777, username="new_hub", name="New"),
                 "1:x": tg.BotInfo(id=1, username="env_hub", name="Env"),
                 "8:client": tg.BotInfo(id=888, username="cl_bot", name="C"),
                 "9:same": tg.BotInfo(id=9, username="old_hub", name="Old")}

        async def fake_check(token, proxy=""):
            if token not in known:
                raise tg.TokenError("Telegram не принял токен")
            return known[token]
        orig = tg.check_token
        tg.check_token = fake_check
        try:
            iid = await self.bot_instance()
            await self.app.db.run("UPDATE instances SET bot_id = 888 WHERE id = ?", iid)
            for bad, why in (("garbage", "не принял"), ("9:same", "текущий"), ("8:client", "бот клиента")):
                with self.assertRaises(HubError) as ctx:
                    await replace_hub(self.app, bad, OWNER)
                self.assertIn(why, str(ctx.exception))
            self.assertEqual(await replace_hub(self.app, "7:newtoken", OWNER), "new_hub")
            await asyncio.sleep(2.2)
            self.assertEqual(stopped, [True], "платформа перезапускается на новом боте")
            self.assertEqual(await active_hub_token(self.app.db, self.app.cfg, self.app.vault), ("custom", "7:newtoken"))
            self.assertIsNone(await self.app.db.value("SELECT deleted_at FROM instances WHERE id = ?", iid),
                              "клиентские боты не тронуты")
            # после перезапуска: объявление (прежний бот без сети в тесте - просто без рассылки)
            self.app.hub_mode, self.app.hub_username, self.app.hub_token = "custom", "new_hub", "7:newtoken"
            info = await self.app.db.get_setting("hub_failover")
            info.pop("old_token_enc")
            await self.app.db.set_setting("hub_failover", info)
            await announce_after_failover(self.app)
            self.assertIn("@new_hub", await site_notice(self.app))
            self.assertTrue(any("теперь @new_hub" in o.text for o in self.notes(OWNER)))
            # вернуть бота из .env
            await replace_hub(self.app, None, OWNER)
            self.assertEqual(await active_hub_token(self.app.db, self.app.cfg, self.app.vault), ("main", "1:x"))
        finally:
            tg.check_token = orig
        # баннер сам исчезает
        await self.app.db.set_setting("site_notice", {"text": "старое", "until": int(time.time()) - 1})
        self.assertEqual(await site_notice(self.app), "")
        self.assertTrue(res)

    async def test_ops_screen(self):
        await self.dp.feed_update(self.bot, callback_update(OWNER, user(OWNER), "ops"))
        text = self.session.sent[-1].text
        self.assertIn("Надёжность", text)
        self.assertIn("MIGRATION", text)
        await self.dp.feed_update(self.bot, callback_update(CLIENT, user(CLIENT), "ops"))
        self.assertEqual(self.session.sent[-1].text, text, "клиенту экран не открывается")


if __name__ == "__main__":
    unittest.main()
