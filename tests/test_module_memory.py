"""Лимит памяти модуля должен быть с запасом выше реального потребления бота.

19.11.2026 лимиты стояли наугад (120-180 МБ) при реальных ~175 МБ у любого бота на aiogram: сторож памяти
убивал Link посреди сохранения приветствия. Этот тест замеряет память каждого модуля после загрузки кода.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULES = ROOT / "modules"
PROBE = """
import importlib, sys
sys.argv = ["x"]
try:
    importlib.import_module(sys.argv_entry)
except SystemExit:
    pass
"""


class ModuleMemory(unittest.TestCase):
    def test_limits_have_headroom(self):
        for manifest in sorted(MODULES.glob("*/manifest.json")):
            data = json.loads(manifest.read_text(encoding="utf-8"))
            entry = data.get("entry", "bot")
            target = entry if (manifest.parent / f"{entry}.py").exists() else f"{entry}.__main__"
            with tempfile.TemporaryDirectory() as tmp:
                env = {**os.environ, "BOT_TOKEN": "1:x", "ADMIN_GROUP_ID": "-1", "DATA_DIR": tmp,
                       "PYTHONPATH": str(MODULES / "kit"), "MALLOC_ARENA_MAX": "2"}
                code = ("import importlib, sys\nsys.argv=['x']\n"
                        f"try:\n    importlib.import_module({target!r})\nexcept (SystemExit, Exception):\n    pass\n"
                        "import aiogram.types, aiogram.methods\n"
                        "print([l.split()[1] for l in open('/proc/self/status') if l.startswith('VmRSS')][0])")
                out = subprocess.run([sys.executable, "-c", code], cwd=manifest.parent, env=env,
                                     capture_output=True, text=True, timeout=60)
            rss_mb = int(out.stdout.strip().splitlines()[-1]) / 1024
            limit = data.get("mem_mb", 320)
            with self.subTest(module=data["slug"], rss=round(rss_mb), limit=limit):
                self.assertGreaterEqual(limit, rss_mb * 1.6,
                                        f"{data['slug']}: после загрузки {rss_mb:.0f} МБ, лимит {limit} МБ - мало запаса")


if __name__ == "__main__":
    unittest.main()
