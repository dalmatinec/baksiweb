"""Медиа: одинаковая картинка, присланная одновременно или повторно, не ломает сохранение."""
import asyncio
import io
import tempfile
import time
import unittest
from pathlib import Path

from aiogram.types import Chat, Message, PhotoSize, User

from bot.db import Database
from bot.media import MediaStore


class FakeBot:
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def download(self, file_id):
        await asyncio.sleep(0.01)  # как настоящая загрузка: даёт второму обработчику вклиниться
        return io.BytesIO(self.data)


def photo_message(file_id: str) -> Message:
    return Message(message_id=1, date=int(time.time()), chat=Chat(id=1, type="private"),
                   from_user=User(id=1, is_bot=False, first_name="A"),
                   photo=[PhotoSize(file_id=file_id, file_unique_id="u" + file_id, width=10, height=10, file_size=3)])


class MediaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "bot.db")
        await self.db.connect()
        self.media = MediaStore(self.db, Path(self.tmp.name) / "media")
        await self.media.load(999)

    async def asyncTearDown(self):
        await self.db.close()
        self.tmp.cleanup()

    async def test_same_image_twice_concurrently(self):
        bot = FakeBot(b"same-image-bytes")
        ids = await asyncio.gather(self.media.save_from_message(bot, photo_message("f1"), 10_000_000),
                                   self.media.save_from_message(bot, photo_message("f2"), 10_000_000))
        self.assertEqual(ids[0], ids[1])
        again = await self.media.save_from_message(bot, photo_message("f3"), 10_000_000)
        self.assertEqual(again, ids[0])

    async def test_known_in_db_but_not_in_memory(self):
        bot = FakeBot(b"image-2")
        mid = await self.media.save_from_message(bot, photo_message("a"), 10_000_000)
        self.media.files.clear()  # как после восстановления/перезагрузки кэша
        self.assertEqual(await self.media.save_from_message(bot, photo_message("b"), 10_000_000), mid)


if __name__ == "__main__":
    unittest.main()
