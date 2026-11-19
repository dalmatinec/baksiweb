"""Запуск ботов через заготовку: изоляция процессов, вывод, коды выхода, падение самой заготовки."""
import asyncio
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from baksi.zygote import Zygote, _alive  # noqa: E402

BOT = '''
import os, sys, time
print("start", os.environ["BAKSI_INSTANCE"], os.environ["BOT_TOKEN"], os.getcwd() == os.environ["EXPECT_CWD"],
      flush=True)
if os.environ.get("CRASH"):
    sys.exit(int(os.environ["CRASH"]))
while True:
    time.sleep(0.1)
'''


class ZygoteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mod = Path(self.tmp.name) / "mod"
        self.mod.mkdir()
        (self.mod / "botmain.py").write_text(BOT)
        kit = str(ROOT / "modules" / "kit")
        self.base_env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": kit, "PYTHONUNBUFFERED": "1"}
        self.z = Zygote(ROOT / "modules", Path(self.tmp.name) / "z.sock", dict(self.base_env))

    async def asyncTearDown(self):
        await self.z.close()
        self.tmp.cleanup()

    async def spawn(self, iid, token, **extra):
        env = {**self.base_env, "BAKSI_INSTANCE": str(iid), "BOT_TOKEN": token, "EXPECT_CWD": str(self.mod), **extra}
        p = await self.z.spawn("botmain", self.mod, env)
        self.assertIsNotNone(p)
        line = await asyncio.wait_for(p.stdout.readline(), 15)
        return p, line.decode().split()

    async def test_isolated_env_output_and_exit_codes(self):
        a, out_a = await self.spawn(1, "AAA")
        b, out_b = await self.spawn(2, "BBB")
        self.assertEqual(out_a, ["start", "1", "AAA", "True"])
        self.assertEqual(out_b, ["start", "2", "BBB", "True"], "у каждого бота свои токен и папка")
        self.assertNotEqual(a.pid, b.pid)
        self.assertEqual(os.getpgid(a.pid), a.pid, "свой сеанс: остановка группы не задевает соседей")
        os.killpg(a.pid, signal.SIGTERM)
        self.assertEqual(await asyncio.wait_for(a.wait(), 10), -signal.SIGTERM)
        self.assertTrue(_alive(b.pid), "падение одного бота не задевает другого")
        c, _ = await self.spawn(3, "CCC", CRASH="3")
        self.assertEqual(await asyncio.wait_for(c.wait(), 10), 3, "код выхода доходит до платформы")
        os.killpg(b.pid, signal.SIGKILL)
        await asyncio.wait_for(b.wait(), 10)

    async def test_bots_survive_zygote_death(self):
        a, _ = await self.spawn(1, "AAA")
        self.z.process.kill()
        await self.z.process.wait()
        await asyncio.sleep(0.5)
        self.assertTrue(_alive(a.pid), "бот работает, даже если заготовка упала")
        b, out_b = await self.spawn(2, "BBB")  # заготовка поднимается заново
        self.assertEqual(out_b[:3], ["start", "2", "BBB"])
        os.kill(a.pid, signal.SIGKILL)
        self.assertEqual(await asyncio.wait_for(a.wait(), 10), -1, "без заготовки код неизвестен, но конец замечен")
        os.killpg(b.pid, signal.SIGKILL)
        await asyncio.wait_for(b.wait(), 10)

    async def test_real_module_imports_under_zygote(self):
        """Настоящие модули импортируются в процессе из заготовки (без запуска поллинга)."""
        for slug, entry in (("svyaz", "bot"), ("express", "bot")):
            check = Path(self.tmp.name) / f"check_{slug}"
            check.mkdir()
            (check / "probe.py").write_text(
                "import importlib.util, sys\n"
                f"spec = importlib.util.spec_from_file_location('m', r'{ROOT / 'modules' / slug / entry / '__main__.py'}')\n"
                "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
                "print('ok', hasattr(m, 'main'), flush=True)\n")
            env = {**self.base_env, "DATA_DIR": str(check), "BOT_TOKEN": "1:x"}
            p = await self.z.spawn("probe", check, env)
            line = await asyncio.wait_for(p.stdout.readline(), 15)
            self.assertEqual(line.decode().split(), ["ok", "True"], slug)
            self.assertEqual(await asyncio.wait_for(p.wait(), 10), 0)


if __name__ == "__main__":
    unittest.main()
