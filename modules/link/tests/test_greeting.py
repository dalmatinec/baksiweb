"""Приветствие: новый текст и картинка сразу видны пользователю по /start."""
from tests.test_flow import OWNER, USER, Base


class Greeting(Base):
    async def test_new_text_shows_on_start(self):
        h = self.h
        await h.send(USER, "/start")
        await h.admin("x:htm:text:start")
        await h.send(OWNER, "Совсем новое приветствие")
        await h.send(USER, "/start")
        self.assertIn("Совсем новое приветствие", h.user_screen(USER).text or h.user_screen(USER).caption or "")

    async def test_photo_with_caption_shows_on_start(self):
        h = self.h
        h.tg.download_data = b"img-bytes"
        await h.admin("x:htm:text:start")
        await h.send(OWNER, None, photo=[{"file_id": "PH1", "file_unique_id": "PH1", "width": 1, "height": 1}],
                     caption="Привет с картинкой")
        await h.send(USER, "/start")
        screen = h.user_screen(USER)
        self.assertEqual(screen.kind, "photo")
        self.assertIn("Привет с картинкой", screen.caption or "")

    async def test_photo_then_text(self):
        h = self.h
        h.tg.download_data = b"img-bytes-2"
        await h.admin("x:med:text:start")
        await h.send(OWNER, None, photo=[{"file_id": "PH2", "file_unique_id": "PH2", "width": 1, "height": 1}])
        await h.admin("x:htm:text:start")
        await h.send(OWNER, "Текст после картинки")
        await h.send(USER, "/start")
        screen = h.user_screen(USER)
        self.assertEqual(screen.kind, "photo")
        self.assertIn("Текст после картинки", screen.caption or "")


class GreetingUnderPlatform(Base):
    """Под платформой: оформление владельца - только стандарт, правка клиента всегда видна."""

    async def asyncSetUp(self):
        import json, os, sys, tempfile
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "kit"))
        import baksi_kit
        from bot import store as store_mod
        self.tmpdir = tempfile.TemporaryDirectory()
        plat = Path(self.tmpdir.name) / "platform.json"
        plat.write_text(json.dumps({"owner_ids": [OWNER], "ui": {"texts": {"start": "Стандарт от владельца"}}}))
        os.environ["BAKSI_PLATFORM_FILE"] = str(plat)
        baksi_kit._platform_cache = None
        self._old_kit = store_mod._kit
        store_mod._kit = baksi_kit
        await super().asyncSetUp()

    async def asyncTearDown(self):
        import os
        from bot import store as store_mod
        store_mod._kit = self._old_kit
        os.environ.pop("BAKSI_PLATFORM_FILE", None)
        await super().asyncTearDown()
        self.tmpdir.cleanup()

    async def test_owner_default_then_client_text(self):
        h = self.h
        await h.send(USER, "/start")
        self.assertIn("Стандарт от владельца", h.user_screen(USER).text)
        await h.admin("x:htm:text:start")
        await h.send(OWNER, "Моё приветствие")
        await h.send(USER, "/start")
        self.assertIn("Моё приветствие", h.user_screen(USER).text)

    async def test_client_photo_greeting(self):
        h = self.h
        h.tg.download_data = b"img-3"
        await h.admin("x:htm:text:start")
        await h.send(OWNER, None, photo=[{"file_id": "PH3", "file_unique_id": "PH3", "width": 1, "height": 1}],
                     caption="Фото-приветствие")
        await h.send(USER, "/start")
        self.assertIn("Фото-приветствие", h.user_screen(USER).caption or "")
