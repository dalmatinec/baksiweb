"""Тесты новых модулей (Экспресс, Связь) на фейковом Bot API: весь путь сообщений туда и обратно."""
import asyncio
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

    async def test_album_makes_one_ticket(self):
        """Альбом приходит пачкой одновременно: заявка и карточка должны быть одни."""
        await self.bind()
        await asyncio.gather(*(self.feed(message_update(CLIENT, user(CLIENT), f"фото {i}", mid=300 + i)) for i in range(5)))
        self.assertEqual(await self.db.value("SELECT COUNT(*) FROM tickets"), 1)
        self.assertEqual(len([s for s in self.session.to(GROUP, "send") if "Заявка #" in (s.text or "")]), 1)
        self.assertEqual(len(self.session.to(GROUP, "forward")), 5)

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

    async def add_operator(self, uid: int, name: str, username: str | None = None):
        await self.feed(callback_update(OWNER, user(OWNER), "sva:ops"))
        token = await self.db.value("SELECT token FROM invites ORDER BY rowid DESC LIMIT 1")
        await self.feed(message_update(uid, user(uid, name, username), f"/start op_{token}"))

    def texts(self, chat_id: int) -> str:
        return "\n".join(s.text or "" for s in self.session.to(chat_id, "send"))

    async def test_only_operators_receive_and_see_client_data(self):
        await self.add_operator(SELLER, "Данияр")
        await self.add_operator(41, "Ерлан")
        self.assertTrue(await self.sv.is_operator(SELLER))
        self.assertFalse(await self.sv.is_operator(OWNER), "владелец не оператор сам по себе")
        await self.feed(message_update(CLIENT, user(CLIENT, "Клиент"), "/start"))
        self.assertIsNone(self.session.to(CLIENT)[-1].markup, "отделов больше нет, клиент сразу пишет")
        await self.feed(message_update(CLIENT, user(CLIENT, "Клиент", "client_nick"), "У меня спам-блок", mid=90))
        for op in (SELLER, 41):
            self.assertEqual(self.session.to(op, "copy")[-1].source_id, 90)
            card = self.texts(op)
            self.assertIn("Новое обращение #1", card)
            self.assertIn("@client_nick", card)
            self.assertIn(str(CLIENT), card)
        self.assertEqual(self.session.to(OWNER, "copy"), [], "владелец без роли оператора ничего не получает")
        self.assertIn("получено", self.session.to(CLIENT, "send")[-1].text)
        cards = self.texts(SELLER).count("Новое обращение")
        received = len(self.session.to(CLIENT, "send"))
        await self.feed(message_update(CLIENT, user(CLIENT, "Клиент"), "Ещё вопрос", mid=91))
        self.assertEqual(self.texts(SELLER).count("Новое обращение"), cards)
        self.assertEqual(len(self.session.to(CLIENT, "send")), received)
        self.assertEqual(self.session.to(41, "copy")[-1].source_id, 91)
        # оператор, добавленный посреди обращения, получает карточку клиента со следующим сообщением
        await self.add_operator(42, "Асель")
        await self.feed(message_update(CLIENT, user(CLIENT, "Клиент"), "Алло", mid=92))
        self.assertIn("Новое обращение #1", self.texts(42))

    async def test_reply_is_anonymous_and_mirrored(self):
        await self.add_operator(SELLER, "Данияр")
        await self.add_operator(41, "Ерлан")
        await self.feed(message_update(CLIENT, user(CLIENT, "Клиент"), "Привет", mid=90))
        copy_mid = self.session.to(SELLER, "copy")[-1].message_id
        await self.feed(message_update(SELLER, user(SELLER, "Данияр"), "Здравствуйте!", mid=91, reply_to=copy_mid))
        self.assertEqual(self.session.to(CLIENT, "copy")[-1].source_id, 91)
        self.assertNotIn("Данияр", self.texts(CLIENT), "клиент не видит, кто ответил")
        self.assertRegex(self.texts(41), r"Данияр</b> ответил в \d\d:\d\d")
        self.assertEqual(self.session.to(41, "copy")[-1].source_id, 91, "второй оператор видит сам ответ")
        self.assertNotIn("ответил в", self.texts(SELLER), "себе зеркало не шлём")
        self.assertIn((SELLER, 91), self.session.reactions)
        await self.feed(callback_update(41, user(41, "Ерлан"), "sv:act:1"))
        self.assertIn("Последним отвечал(а): Данияр", self.texts(41))
        await self.feed(message_update(41, user(41, "Ерлан"), "Дополню", mid=92))
        self.assertEqual(self.session.to(CLIENT, "copy")[-1].source_id, 92)
        self.assertIn("Ерлан</b> ответил", self.texts(SELLER))
        await self.feed(callback_update(41, user(41, "Ерлан"), "sv:hist:1"))
        self.assertEqual([s.source_id for s in self.session.to(41, "copy")][-3:], [90, 91, 92])
        self.assertIn("🎧 Данияр", self.texts(41))
        await self.feed(message_update(41, user(41), "/close"))
        self.assertEqual(await self.db.value("SELECT status FROM dialogs WHERE id = 1"), "closed")
        self.assertIn("закрыто", self.texts(SELLER))

    async def test_no_operators_reminds_owner_and_keeps_history(self):
        await self.feed(message_update(CLIENT, user(CLIENT, "Клиент"), "Есть кто?", mid=90))
        self.assertIn("обращения никто не видит", self.texts(OWNER))
        self.assertEqual(await self.db.value("SELECT COUNT(*) FROM history"), 1)
        before = self.texts(OWNER).count("никто не видит")
        await self.feed(message_update(CLIENT, user(CLIENT, "Клиент"), "Алло", mid=91))
        self.assertEqual(self.texts(OWNER).count("никто не видит"), before, "напоминание не чаще раза в 30 минут")
        await self.feed(message_update(OWNER, user(OWNER), "/admin"))
        self.assertIn("Обращения никто не видит", self.texts(OWNER))
        await self.add_operator(SELLER, "Данияр")
        self.assertIn("ждут ответа: 1", self.texts(SELLER))
        await self.feed(callback_update(SELLER, user(SELLER), "sv:hist:1"))
        self.assertEqual([s.source_id for s in self.session.to(SELLER, "copy")][-2:], [90, 91])
        await self.feed(callback_update(SELLER, user(SELLER), "sv:act:1"))
        await self.feed(message_update(SELLER, user(SELLER, "Данияр"), "Мы тут", mid=92))
        self.assertEqual(self.session.to(CLIENT, "copy")[-1].source_id, 92)

    async def test_owner_becomes_operator_by_invite_and_removal(self):
        await self.add_operator(OWNER, "Owner")
        self.assertTrue(await self.sv.is_operator(OWNER))
        await self.feed(message_update(CLIENT, user(CLIENT), "Привет", mid=90))
        self.assertEqual(self.session.to(OWNER, "copy")[-1].source_id, 90)
        await self.add_operator(SELLER, "Данияр")
        await self.feed(callback_update(OWNER, user(OWNER), f"sva:oprm:{SELLER}"))
        self.assertFalse(await self.sv.is_operator(SELLER))
        self.assertIn("больше не оператор", self.texts(SELLER))

    async def test_non_operator_cannot_reply(self):
        await self.add_operator(SELLER, "Данияр")
        await self.feed(message_update(CLIENT, user(CLIENT), "Привет", mid=90))
        await self.feed(callback_update(OWNER, user(OWNER), "sv:act:1"))
        self.assertIn("только операторы", self.session.answers[-1])
        await self.feed(message_update(OWNER, user(OWNER), "Я владелец", mid=91))
        self.assertNotIn(91, [s.source_id for s in self.session.to(CLIENT, "copy")])

    async def test_texts_menu_edit_and_reset(self):
        await self.feed(callback_update(OWNER, user(OWNER), "sva:texts"))
        self.assertIn("Тексты", self.session.sent[-1].text)
        await self.feed(callback_update(OWNER, user(OWNER), "sva:txt:greeting"))
        await self.feed(message_update(OWNER, user(OWNER), "Привет, пишите!"))
        self.assertIn("Сохранено", self.texts(OWNER))
        await self.feed(message_update(CLIENT, user(CLIENT), "/start"))
        self.assertEqual(self.session.to(CLIENT, "send")[-1].text, "Привет, пишите!")
        await self.feed(callback_update(OWNER, user(OWNER), "sva:txr:greeting"))
        await self.feed(message_update(CLIENT, user(CLIENT), "/start"))
        self.assertIn("Здравствуйте", self.session.to(CLIENT, "send")[-1].text)

    async def test_album_makes_one_dialog(self):
        await self.add_operator(SELLER, "Данияр")
        await asyncio.gather(*(self.feed(message_update(CLIENT, user(CLIENT), f"фото {i}", mid=400 + i)) for i in range(4)))
        self.assertEqual(await self.db.value("SELECT COUNT(*) FROM dialogs"), 1)
        self.assertEqual(self.texts(SELLER).count("Новое обращение"), 1)
        self.assertEqual(len(self.session.to(SELLER, "copy")), 4)

    async def test_plain_group_reply_and_mirror(self):
        await self.add_operator(SELLER, "Данияр")
        await self.bind()
        await self.feed(message_update(CLIENT, user(CLIENT, "Клиент", "client_nick"), "Помогите", mid=95))
        self.assertIn("@client_nick", self.texts(GROUP))
        card = [s for s in self.session.to(GROUP, "send") if "Новое обращение" in (s.text or "")][0]
        copy = [s for s in self.session.to(GROUP, "copy") if s.source_id == 95][0]
        self.assertEqual(copy.reply_to, card.message_id, "сообщение клиента привязано к его карточке")
        self.assertEqual(self.session.to(SELLER, "copy")[-1].source_id, 95, "операторы в личке тоже получают")
        # участник группы (не оператор) отвечает реплаем
        await self.feed(message_update(GROUP, user(77, "Участник"), "Слушаю", mid=600, reply_to=copy.message_id))
        self.assertEqual(self.session.to(CLIENT, "copy")[-1].source_id, 600)
        self.assertIn("Участник</b> ответил", self.texts(SELLER))
        # сообщение в группе без реплая клиенту не уходит
        await self.feed(message_update(GROUP, user(77, "Участник"), "болтаем", mid=601))
        self.assertNotIn(601, [s.source_id for s in self.session.to(CLIENT, "copy")])
        # ответ оператора из лички виден в группе
        await self.feed(message_update(SELLER, user(SELLER, "Данияр"), "Дополню", mid=96,
                                       reply_to=self.session.to(SELLER, "copy")[0].message_id))
        self.assertIn("Данияр</b> ответил", self.texts(GROUP))
        self.assertEqual(self.session.to(GROUP, "copy")[-1].source_id, 96)
        await self.feed(callback_update(GROUP, user(77), "sv:close:1"))
        self.assertEqual(await self.db.value("SELECT status FROM dialogs WHERE id = 1"), "closed")

    async def test_group_only_without_operators(self):
        await self.bind()
        await self.feed(message_update(CLIENT, user(CLIENT, "Клиент"), "Есть кто?", mid=95))
        self.assertNotIn("никто не видит", self.texts(OWNER), "группа подключена - напоминать не нужно")
        self.assertTrue(any(s.source_id == 95 for s in self.session.to(GROUP, "copy")))

    async def test_topics_group(self):
        self.session.forums.add(GROUP)
        await self.bind()
        await self.feed(message_update(CLIENT, user(CLIENT, "Клиент"), "Помогите", mid=95))
        topic_copy = [s for s in self.session.to(GROUP, "copy") if s.source_id == 95][0]
        self.assertIsNotNone(topic_copy.thread)
        await self.feed(message_update(GROUP, user(SELLER, "Данияр"), "Слушаю", mid=600, thread=topic_copy.thread))
        self.assertEqual(self.session.to(CLIENT, "copy")[-1].source_id, 600)

    async def test_group_screen_and_unbind(self):
        await self.feed(callback_update(OWNER, user(OWNER), "sva:group"))
        self.assertIn("/bind 123456", self.session.sent[-1].text)
        await self.bind()
        await self.feed(callback_update(OWNER, user(OWNER), "sva:group"))
        self.assertIn("подключена", self.session.sent[-1].text)
        await self.feed(callback_update(OWNER, user(OWNER), "sva:unbind"))
        self.assertIsNone(baksi_kit.bound_chat_id())

    async def test_blocked_client_reported(self):
        await self.add_operator(SELLER, "Данияр")
        await self.feed(message_update(CLIENT, user(CLIENT), "Привет"))
        await self.feed(callback_update(SELLER, user(SELLER), "sv:act:1"))
        self.session.blocked.add(CLIENT)
        await self.feed(message_update(SELLER, user(SELLER), "Ответ"))
        self.assertIn("заблокировал", self.session.to(SELLER, "send")[-1].text)

    async def test_blocked_operator_does_not_break_others(self):
        await self.add_operator(SELLER, "Данияр")
        await self.add_operator(41, "Ерлан")
        self.session.blocked.add(SELLER)
        await self.feed(message_update(CLIENT, user(CLIENT), "Привет", mid=90))
        self.assertEqual(self.session.to(41, "copy")[-1].source_id, 90)

    async def test_migration_keeps_old_staff_as_operators(self):
        await self.db.close()
        path = Path(self.tmp.name) / "old.db"
        old = ModuleDB(path, self.mod.MIGRATIONS[:1])
        await old.connect()
        await old.run("INSERT INTO departments(title) VALUES ('Продажи')")
        await old.run("INSERT INTO staff(user_id, dept_id, name, added_at) VALUES (?, 1, 'Данияр', 1)", SELLER)
        await old.close()
        self.db = ModuleDB(path, self.mod.MIGRATIONS)
        await self.db.connect()
        self.assertEqual(await self.db.value("SELECT name FROM operators WHERE user_id = ?", SELLER), "Данияр")


if __name__ == "__main__":
    unittest.main()
