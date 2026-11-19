"""Тесты новых модулей (Экспресс, Связь) на фейковом Bot API: весь путь сообщений туда и обратно."""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "modules" / "kit"))
sys.path.insert(0, str(ROOT / "tests"))

from aiogram import Bot, Dispatcher  # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402

import baksi_kit  # noqa: E402
from baksi_db import ModuleDB  # noqa: E402
from fake_tg import FakeSession, callback_update, message_update, user  # noqa: E402

OWNER, CLIENT, SELLER, GROUP = 10, 20, 30, -100500


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"mod_{name}", ROOT / "modules" / name / "bot" / "__main__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ModuleBase(unittest.IsolatedAsyncioTestCase):
    module = ""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        data = Path(self.tmp.name)
        platform = data / "platform.json"
        platform.write_text(json.dumps({"owner_ids": [OWNER], "bind_code": "123456", "settings": {}, "state": {}}))
        os.environ.update(DATA_DIR=str(data), BAKSI_PLATFORM_FILE=str(platform), OWNER_IDS=str(OWNER))
        baksi_kit._platform_cache = None
        self.mod = load(self.module)
        self.session = FakeSession()
        self.bot = Bot("123:abc", session=self.session)
        self.db = ModuleDB(data / "m.db", self.mod.MIGRATIONS)
        await self.db.connect()

    async def asyncTearDown(self):
        await self.db.close()
        self.tmp.cleanup()
        for k in ("DATA_DIR", "BAKSI_PLATFORM_FILE", "OWNER_IDS"):
            os.environ.pop(k, None)

    async def feed(self, update):
        await self.dp.feed_update(self.bot, update)

    async def bind(self):
        await self.feed(message_update(GROUP, user(OWNER, "Owner"), "/bind 123456"))
        self.assertEqual(baksi_kit.bound_chat_id(), GROUP)


class ExpressTests(ModuleBase):
    module = "express"

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.ex = self.mod.Express(self.bot, self.db)
        self.dp = Dispatcher(storage=MemoryStorage(), ex=self.ex)
        self.dp.include_router(baksi_kit.bind_router())
        self.dp.include_router(self.mod.router)

    async def test_full_cycle(self):
        await self.feed(message_update(CLIENT, user(CLIENT, "Айгерим"), "Нужен логотип"))
        self.assertIn("ещё настраивается", self.session.to(CLIENT)[-1].text)
        await self.bind()
        await self.feed(message_update(CLIENT, user(CLIENT, "Айгерим"), "Нужен логотип", mid=77))
        fwd = self.session.to(GROUP, "forward")[-1]
        self.assertEqual((fwd.from_chat, fwd.source_id), (CLIENT, 77), "заявка ушла именно форвардом")
        control = self.session.to(GROUP, "send")[-1]
        self.assertIn("Заявка #1", control.text)
        self.assertIn("Заявка отправлена", self.session.to(CLIENT)[-1].text)
        # второе сообщение той же заявки - без новой карточки
        await self.feed(message_update(CLIENT, user(CLIENT, "Айгерим"), "Бюджет 30к", mid=78))
        self.assertEqual(len([s for s in self.session.to(GROUP, "send") if "Заявка #" in (s.text or "")]), 1)
        # продавец отвечает реплаем
        await self.feed(message_update(GROUP, user(SELLER, "Данияр"), "Возьмусь", mid=501, reply_to=fwd.message_id))
        answer = self.session.to(CLIENT, "forward")[-1]
        self.assertEqual((answer.from_chat, answer.source_id), (GROUP, 501))
        self.assertIn((GROUP, 501), self.session.reactions)
        # заказчик отвечает на ответ продавца
        await self.feed(message_update(CLIENT, user(CLIENT, "Айгерим"), "Давайте", mid=79, reply_to=answer.message_id))
        self.assertTrue(any("Ответ заказчика" in (s.text or "") for s in self.session.to(GROUP, "send")))
        # обычный участник не может закрыть, админ группы может
        await self.feed(callback_update(GROUP, user(SELLER), "ex:close:1"))
        self.assertIn("админы", self.session.answers[-1])
        self.session.admins.add((GROUP, 40))
        await self.feed(callback_update(GROUP, user(40), "ex:close:1"))
        self.assertEqual(await self.db.value("SELECT status FROM tickets WHERE id = 1"), "closed")
        await self.feed(message_update(GROUP, user(SELLER, "Данияр"), "Ещё", mid=502, reply_to=fwd.message_id))
        self.assertEqual(len(self.session.to(CLIENT, "forward")), 1, "в закрытую заявку не отвечаем")

    async def test_owner_design_applies(self):
        """Оформление от владельца платформы: текст карточки и кнопки меняются, своя правка клиента важнее."""
        plat = Path(os.environ["BAKSI_PLATFORM_FILE"])
        data = json.loads(plat.read_text())
        data["ui"] = {"texts": {"card": "🔥 Новая заявка №{номер}", "greeting": "Привет от владельца"},
                      "buttons": {"ex_close": {"text": "Готово", "style": "primary", "icon": "123"}}}
        plat.write_text(json.dumps(data))
        baksi_kit._platform_cache = None
        await self.bind()
        await self.feed(message_update(CLIENT, user(CLIENT), "Нужна помощь"))
        card = [s for s in self.session.to(GROUP, "send") if "Новая заявка" in (s.text or "")][-1]
        self.assertEqual(card.text, "🔥 Новая заявка №1")
        close = card.markup.inline_keyboard[0][0]
        self.assertEqual((close.text, close.style, close.icon_custom_emoji_id), ("Готово", "primary", "123"))
        await self.feed(message_update(CLIENT, user(CLIENT), "/start"))
        self.assertEqual(self.session.to(CLIENT)[-1].text, "Привет от владельца")
        await self.ex.db.set("greeting", "Своё приветствие клиента")
        await self.feed(message_update(CLIENT, user(CLIENT), "/start"))
        self.assertEqual(self.session.to(CLIENT)[-1].text, "Своё приветствие клиента")

    async def test_ban_and_cooldown(self):
        await self.bind()
        await self.feed(message_update(CLIENT, user(CLIENT), "Привет"))
        await self.feed(callback_update(GROUP, user(OWNER), f"ex:ban:{CLIENT}"))
        before = len(self.session.to(GROUP, "forward"))
        await self.feed(message_update(CLIENT, user(CLIENT), "Снова"))
        self.assertEqual(len(self.session.to(GROUP, "forward")), before, "забаненный не попадает в группу")
        await self.db.run("UPDATE users SET banned = 0")
        await self.feed(message_update(CLIENT, user(CLIENT), "Новая"))
        self.assertIn("через", self.session.to(CLIENT)[-1].text, "пауза между заявками")

    async def test_admin_settings(self):
        await self.feed(message_update(OWNER, user(OWNER), "/admin"))
        self.assertIn("Экспресс: админка", self.session.to(OWNER)[-1].text)
        await self.feed(callback_update(OWNER, user(OWNER), "exa:tog:anonymous"))
        self.assertTrue(self.ex.opt("anonymous"))
        await self.feed(callback_update(OWNER, user(OWNER), "exa:txt:greeting"))
        await self.feed(message_update(OWNER, user(OWNER), "Новое приветствие"))
        self.assertEqual(self.ex.opt("greeting"), "Новое приветствие")
        await self.feed(message_update(CLIENT, user(CLIENT), "/start"))
        self.assertEqual(self.session.to(CLIENT)[-1].text, "Новое приветствие")


class SvyazTests(ModuleBase):
    module = "svyaz"

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.sv = self.mod.Svyaz(self.bot, self.db, "test_bot")
        self.dp = Dispatcher(storage=MemoryStorage(), sv=self.sv)
        self.dp.include_router(baksi_kit.bind_router())
        self.dp.include_router(self.mod.router)

    async def make_dept(self, title: str) -> int:
        await self.feed(callback_update(OWNER, user(OWNER), "sva:newdept"))
        await self.feed(message_update(OWNER, user(OWNER), title))
        return await self.db.value("SELECT id FROM departments WHERE title = ?", title)

    async def invite(self, dept_id: int, uid: int, name: str) -> None:
        await self.feed(callback_update(OWNER, user(OWNER), f"sva:dept:{dept_id}"))
        token = await self.db.value("SELECT token FROM invites ORDER BY rowid DESC LIMIT 1")
        await self.feed(message_update(uid, user(uid, name), f"/start staff_{token}"))

    async def test_personal_mode_cycle(self):
        ops = await self.make_dept("Оператор")
        design = await self.make_dept("Дизайнеры")
        await self.invite(ops, SELLER, "Данияр")
        await self.invite(design, 41, "Ерлан")
        self.assertTrue(await self.sv.is_staff(SELLER))
        await self.feed(message_update(CLIENT, user(CLIENT, "Клиент"), "/start"))
        self.assertIsNotNone(self.session.to(CLIENT)[-1].markup, "клиент выбирает отдел")
        await self.feed(callback_update(CLIENT, user(CLIENT), f"sv:dept:{ops}"))
        await self.feed(message_update(CLIENT, user(CLIENT, "Клиент"), "У меня спам-блок", mid=90))
        copies = self.session.to(SELLER, "copy")
        self.assertEqual(copies[-1].source_id, 90)
        self.assertEqual(self.session.to(41, "copy"), [], "другой отдел не получает")
        # сотрудник берёт диалог и пишет без реплаев
        await self.feed(callback_update(SELLER, user(SELLER, "Данияр"), "sv:act:1"))
        await self.feed(message_update(SELLER, user(SELLER, "Данияр"), "Здравствуйте!", mid=91))
        self.assertTrue(any("Вам отвечает Данияр" in (s.text or "") for s in self.session.to(CLIENT, "send")))
        self.assertEqual(self.session.to(CLIENT, "copy")[-1].source_id, 91)
        # клиент отвечает - уходит только назначенному сотруднику
        await self.feed(message_update(CLIENT, user(CLIENT), "Спасибо", mid=92))
        self.assertEqual(self.session.to(SELLER, "copy")[-1].source_id, 92)
        # передача в дизайнеры
        await self.feed(callback_update(SELLER, user(SELLER), f"sv:trd:1:{design}"))
        self.assertTrue(any("передан диалог" in (s.text or "") for s in self.session.to(41, "send")))
        await self.feed(callback_update(41, user(41, "Ерлан"), "sv:act:1"))
        await self.feed(message_update(41, user(41, "Ерлан"), "Я дизайнер", mid=93))
        self.assertEqual(self.session.to(CLIENT, "copy")[-1].source_id, 93)
        await self.feed(message_update(41, user(41), "/close"))
        self.assertEqual(await self.db.value("SELECT status FROM dialogs WHERE id = 1"), "closed")

    async def test_topics_mode(self):
        await self.make_dept("Поддержка")
        await self.sv.db.set("mode", "topics")
        await self.bind()
        await self.feed(message_update(CLIENT, user(CLIENT, "Клиент"), "Помогите", mid=95))
        topic_copy = [s for s in self.session.to(GROUP, "copy") if s.source_id == 95][0]
        self.assertIsNotNone(topic_copy.thread)
        await self.feed(message_update(GROUP, user(SELLER, "Данияр"), "Слушаю", mid=600, thread=topic_copy.thread))
        self.assertEqual(self.session.to(CLIENT, "copy")[-1].source_id, 600)

    async def test_blocked_client_reported(self):
        await self.make_dept("Оператор")
        await self.invite(1, SELLER, "Данияр")
        await self.feed(message_update(CLIENT, user(CLIENT), "Привет"))
        await self.feed(callback_update(SELLER, user(SELLER), "sv:act:1"))
        self.session.blocked.add(CLIENT)
        await self.feed(message_update(SELLER, user(SELLER), "Ответ"))
        self.assertIn("заблокировал", self.session.to(SELLER, "send")[-1].text)


if __name__ == "__main__":
    unittest.main()
