"""Капча из kit: виды, ошибки, отложенные сообщения, исключения, настройка владельцем."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "modules" / "kit"))
sys.path.insert(0, str(ROOT / "tests"))

from aiogram import Bot, Dispatcher, F, Router  # noqa: E402
from aiogram.filters import CommandObject, CommandStart  # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402
from aiogram.types import Message  # noqa: E402

import baksi_kit  # noqa: E402
from baksi_captcha import Captcha, make_challenge  # noqa: E402
from fake_tg import FakeSession, callback_update, message_update, user  # noqa: E402

OWNER, NEW, STAFF, GROUP = 10, 20, 30, -100500


class CaptchaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        data = Path(self.tmp.name)
        platform = data / "platform.json"
        platform.write_text(json.dumps({"owner_ids": [OWNER], "settings": {}, "state": {}}))
        os.environ.update(DATA_DIR=str(data), BAKSI_PLATFORM_FILE=str(platform), OWNER_IDS=str(OWNER))
        baksi_kit._platform_cache = None
        self.session = FakeSession()
        self.bot = Bot("123:abc", session=self.session)
        self.handled: list[tuple[int, str]] = []
        router = Router()

        @router.message(CommandStart())
        async def start(message: Message, command: CommandObject):
            self.handled.append((message.from_user.id, f"start:{command.args or ''}"))

        @router.message(F.text)
        async def text(message: Message):
            self.handled.append((message.from_user.id, message.text))

        await self.make_dp(router)

    async def make_dp(self, router):
        self.router = router
        self.dp = Dispatcher(storage=MemoryStorage())
        self.dp.include_router(router)
        self.captcha = Captcha(back="adm:home", exempt=lambda uid: uid == STAFF)
        await self.captcha.setup(self.dp)

    async def asyncTearDown(self):
        await self.captcha.close()
        self.tmp.cleanup()
        for k in ("DATA_DIR", "BAKSI_PLATFORM_FILE", "OWNER_IDS"):
            os.environ.pop(k, None)

    async def feed(self, update):
        await self.dp.feed_update(self.bot, update)

    def last(self, chat_id):
        return [s for s in self.session.sent if s.chat_id == chat_id][-1]

    async def solve(self, uid, right=True):
        p = self.captcha.pending[uid]
        choice = p["answer"] if right else (p["answer"] + 1) % len(self.buttons(uid))
        await self.feed(callback_update(uid, user(uid), f"kcap:a:{p['token']}:{choice}", message_id=p["msg"]))

    def buttons(self, uid):
        markup = [s for s in self.session.sent if s.chat_id == uid and s.markup][-1].markup
        return [b for row in markup.inline_keyboard for b in row]

    async def test_off_by_default(self):
        await self.feed(message_update(NEW, user(NEW), "привет"))
        self.assertEqual(self.handled, [(NEW, "привет")])

    async def test_owner_menu_and_modes(self):
        await self.feed(message_update(OWNER, user(OWNER), "/captcha"))
        self.assertIn("Капча", self.last(OWNER).text)
        await self.feed(message_update(NEW, user(NEW), "/captcha"))
        self.assertEqual(self.handled, [(NEW, "/captcha")], "не владельцу /captcha не открывается")
        for mode in ("button", "math", "emoji"):
            await self.feed(callback_update(OWNER, user(OWNER), f"kcap:m:{mode}"))
            self.assertEqual(self.captcha.mode, mode)
        await self.feed(callback_update(NEW, user(NEW), "kcap:m:off"))
        self.assertEqual(self.captcha.mode, "emoji", "не владелец не меняет режим")
        # режим сохраняется после перезапуска
        await self.captcha.close()
        self.captcha = Captcha(back="adm:home")
        await self.captcha.setup(Dispatcher())
        self.assertEqual(self.captcha.mode, "emoji")

    async def test_challenge_shapes(self):
        for mode, count in (("button", 1), ("math", 4), ("emoji", 6)):
            for _ in range(30):
                text, options, answer, _ = make_challenge(mode)
                self.assertEqual(len(options), count)
                self.assertEqual(len(set(options)), count)
                self.assertTrue(0 <= answer < count)
                if mode == "math":
                    expr = text.split("<b>")[2].split("</b>")[0].replace("−", "-")
                    self.assertEqual(int(options[answer]), eval(expr))

    async def test_new_user_held_then_replayed(self):
        await self.captcha.set_mode("math")
        await self.feed(message_update(NEW, user(NEW), "/start op_abc"))
        await self.feed(message_update(NEW, user(NEW), "вопрос"))
        self.assertEqual(self.handled, [], "до проверки ничего не обрабатывается")
        self.assertIn("Сколько будет", self.last(NEW).text)
        self.assertEqual(sum("Сколько будет" in (s.text or "") for s in self.session.sent), 1, "одна проверка")
        await self.solve(NEW)
        self.assertEqual(self.handled, [(NEW, "start:op_abc"), (NEW, "вопрос")], "отложенное обработано по порядку")
        await self.feed(message_update(NEW, user(NEW), "дальше"))
        self.assertEqual(self.handled[-1], (NEW, "дальше"))
        self.assertIn(NEW, self.captcha.passed)

    async def test_wrong_answers_block(self):
        await self.captcha.set_mode("emoji")
        await self.feed(message_update(NEW, user(NEW), "привет"))
        await self.solve(NEW, right=False)
        self.assertIn("Неверно", self.session.sent[-1].text)
        await self.solve(NEW, right=False)
        await self.solve(NEW, right=False)
        self.assertNotIn(NEW, self.captcha.pending)
        self.assertIn("Попробуйте через", self.session.sent[-1].text)
        sent = len(self.session.sent)
        await self.feed(message_update(NEW, user(NEW), "ещё"))
        self.assertEqual(len(self.session.sent), sent, "в блоке новую проверку не шлём")
        self.assertEqual(self.handled, [])
        self.captcha.blocked[NEW] = 0
        await self.feed(message_update(NEW, user(NEW), "снова"))
        await self.solve(NEW)
        self.assertEqual(self.handled, [(NEW, "снова")])

    async def test_stale_token(self):
        await self.captcha.set_mode("button")
        await self.feed(message_update(NEW, user(NEW), "привет"))
        await self.feed(callback_update(NEW, user(NEW), "kcap:a:000000:0"))
        self.assertIn("устарела", self.session.answers[-1])
        self.assertEqual(self.handled, [])

    async def test_exempt_owner_staff_groups(self):
        await self.captcha.set_mode("button")
        await self.feed(message_update(OWNER, user(OWNER), "я владелец"))
        await self.feed(message_update(STAFF, user(STAFF), "я сотрудник"))
        await self.feed(message_update(GROUP, user(NEW), "в группе"))
        self.assertEqual(self.handled, [(OWNER, "я владелец"), (STAFF, "я сотрудник"), (NEW, "в группе")])

    async def test_callback_from_unchecked_user(self):
        await self.captcha.set_mode("button")
        await self.feed(callback_update(NEW, user(NEW), "menu:1"))
        self.assertIn("пройдите проверку", self.session.answers[-1])
        self.assertIn("не робот", self.last(NEW).text)


if __name__ == "__main__":
    unittest.main()
