"""Хаб-бот на фейковом Bot API: от /start до оплаты и карточки бота."""
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

from fake_tg import FakeSession, callback_update, message_update, user  # noqa: E402

OWNER, CLIENT = 1, 2


class HubTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ.update(HUB_BOT_TOKEN="1:x", OWNER_IDS=str(OWNER), SECRET_KEY="s" * 40, DATA_DIR=self.tmp.name,
                          PUBLIC_URL="https://example.com", CRYPTOPAY_TOKEN="")
        from baksi.__main__ import build
        from baksi.config import load_config
        from baksi.hub.handlers import router
        from baksi.hub.middleware import Guard
        import baksi.hub.middleware as mw
        mw.FLOOD_INTERVAL = 0
        self.app = await build(load_config(Path("/nonexistent")))
        self.app.hub_username = "hub_bot"
        await self.app.db.run("UPDATE plans SET price = NULL WHERE product = 'link'")
        await self.app.db.run("UPDATE plans SET price = 500 WHERE product = 'express' AND days = 30")
        self.session = FakeSession()
        self.bot = Bot("123:abc", session=self.session, default=DefaultBotProperties(parse_mode="HTML"))
        self.dp = Dispatcher(storage=MemoryStorage(), app=self.app)
        guard = Guard(self.app)
        self.dp.message.outer_middleware(guard)
        self.dp.callback_query.outer_middleware(guard)
        router._parent_router = None  # роутер модульный: подключаем к новому диспетчеру в каждом тесте
        from baksi.hub.security import router as security_router
        security_router._parent_router = None
        self.dp.include_router(security_router)
        from baksi.hub.ops import router as ops_router
        ops_router._parent_router = None
        self.dp.include_router(ops_router)
        self.dp.include_router(router)

    async def asyncTearDown(self):
        await self.app.db.close()
        self.tmp.cleanup()

    def last(self, chat):
        return [s for s in self.session.sent if s.chat_id == chat and s.text][-1]

    async def feed(self, upd):
        await self.dp.feed_update(self.bot, upd)

    async def test_buy_flow(self):
        await self.feed(message_update(CLIENT, user(CLIENT, "Client"), "/start"))
        self.assertIn("Баланс", self.last(CLIENT).text)
        await self.feed(callback_update(CLIENT, user(CLIENT), "cat"))
        self.assertIn("Экспресс", self.last(CLIENT).text)
        self.assertNotIn("Link", self.last(CLIENT).text, "без цен бот не продаётся")
        await self.feed(callback_update(CLIENT, user(CLIENT), "p:express"))
        await self.feed(callback_update(CLIENT, user(CLIENT), "buy:express:30"))
        self.assertIn("Не хватает $5.00", self.last(CLIENT).text)
        await self.app.billing.admin_adjust(OWNER, CLIENT, 1000, "")
        await self.feed(callback_update(CLIENT, user(CLIENT), "pay:express:30"))
        card = self.last(CLIENT)
        self.assertIn("Подключите бота", card.text)
        self.assertEqual(await self.app.db.value("SELECT balance FROM users WHERE id = ?", CLIENT), 500)
        await self.feed(callback_update(CLIENT, user(CLIENT), "my"))
        self.assertIn("Экспресс", self.last(CLIENT).text)
        await self.feed(callback_update(CLIENT, user(CLIENT), "tok:1"))
        await self.feed(message_update(CLIENT, user(CLIENT), "not a token"))
        self.assertIn("не похоже на токен", [s for s in self.session.sent if s.chat_id == CLIENT][-1].text)

    async def test_trial_wallet_ref_admin(self):
        await self.feed(message_update(CLIENT, user(CLIENT), "/start r1"))
        self.assertEqual(await self.app.db.value("SELECT ref_by FROM users WHERE id = ?", CLIENT), None,
                         "реферер должен существовать")
        await self.feed(callback_update(CLIENT, user(CLIENT), "trial:express"))
        self.assertIn("пробный до", self.last(CLIENT).text)
        await self.feed(callback_update(CLIENT, user(CLIENT), "wallet"))
        self.assertIn("Пополнение пока недоступно", self.last(CLIENT).text)
        await self.feed(callback_update(CLIENT, user(CLIENT), "ref"))
        self.assertIn("t.me/hub_bot?start=r2", self.last(CLIENT).text)
        await self.feed(callback_update(CLIENT, user(CLIENT), "web"))
        self.assertIn("одноразовая", self.last(CLIENT).text)
        await self.feed(message_update(OWNER, user(OWNER), "/start"))
        await self.feed(callback_update(OWNER, user(OWNER), "adm"))
        self.assertIn("Админка", self.last(OWNER).text)
        await self.feed(message_update(OWNER, user(OWNER), f"/give {CLIENT} 12.5 бонус"))
        self.assertEqual(await self.app.db.value("SELECT balance FROM users WHERE id = ?", CLIENT), 1250)
        await self.feed(callback_update(CLIENT, user(CLIENT), "adm"))  # не владелец - ничего
        self.assertNotIn("Админка", self.last(CLIENT).text)


    async def test_design_in_telegram(self):
        from aiogram.types import MessageEntity
        await self.feed(message_update(OWNER, user(OWNER), "/start"))
        await self.feed(callback_update(OWNER, user(OWNER), "des"))
        self.assertIn("Оформление ботов", self.last(OWNER).text)
        await self.feed(callback_update(OWNER, user(OWNER), "des:t:hub"))
        await self.feed(callback_update(OWNER, user(OWNER), "des:x:hub:catalog"))
        # текст с жирным и премиум-эмодзи: сохраняется как HTML
        text = "Каталог 🔥"
        ents = [MessageEntity(type="bold", offset=0, length=7),
                MessageEntity(type="custom_emoji", offset=8, length=2, custom_emoji_id="5368324170671202286")]
        await self.feed(message_update(OWNER, user(OWNER), text, entities=ents))
        saved = self.app.design.values["texts"]["catalog"]
        self.assertIn("<b>Каталог</b>", saved)
        self.assertIn('<tg-emoji emoji-id="5368324170671202286">', saved)
        # кнопка: премиум-эмодзи в начале становится иконкой
        await self.feed(callback_update(OWNER, user(OWNER), "des:bt:hub:h_cat"))
        await self.feed(message_update(OWNER, user(OWNER), "🛍 Магазин ботов",
                                       entities=[MessageEntity(type="custom_emoji", offset=0, length=2, custom_emoji_id="777")]))
        btn = self.app.design.values["buttons"]["h_cat"]
        self.assertEqual(btn, {"text": "Магазин ботов", "icon": "777"})
        await self.feed(callback_update(OWNER, user(OWNER), "des:bs:hub:h_cat:danger"))
        self.assertEqual(self.app.design.values["buttons"]["h_cat"]["style"], "danger")
        await self.feed(callback_update(OWNER, user(OWNER), "des:l:hub:main_cols"))
        self.assertEqual(self.app.design.values["layout"]["main_cols"], 3)
        # меню клиента уже в новом виде
        await self.feed(message_update(CLIENT, user(CLIENT), "/start"))
        first = [s for s in self.session.sent if s.chat_id == CLIENT][-1].markup.inline_keyboard[0][0]
        self.assertEqual((first.text, first.icon_custom_emoji_id, first.style), ("Магазин ботов", "777", "danger"))
        # не владелец в оформление не попадает
        await self.feed(callback_update(CLIENT, user(CLIENT), "des"))
        self.assertNotIn("Оформление", self.last(CLIENT).text)


if __name__ == "__main__":
    unittest.main()
