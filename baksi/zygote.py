"""Запуск ботов через заготовку (modules/kit/baksi_zygote.py): fork вместо нового интерпретатора.

ZygoteProcess повторяет то, чем супервизор пользуется у asyncio.subprocess.Process: pid, returncode,
stdout (поток вывода), wait(). Поэтому перезапуски, логи, сторож памяти и остановка работают как раньше.
Если заготовку не удалось поднять, супервизор запускает бота обычным способом.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sys
import time
from pathlib import Path

log = logging.getLogger("baksi.zygote")


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return False


class ZygoteProcess:
    def __init__(self, pid: int, stdout: asyncio.StreamReader, control: asyncio.StreamReader,
                 writer: asyncio.StreamWriter) -> None:
        self.pid = pid
        self.stdout = stdout
        self.returncode: int | None = None
        self._control = control
        self._writer = writer
        self._done = asyncio.get_running_loop().create_future()
        self._task = asyncio.create_task(self._watch())

    async def _watch(self) -> None:
        code: int | None = None
        try:
            while True:
                line = await self._control.readline()
                if not line:
                    break
                parts = line.decode().split()
                if len(parts) == 2 and parts[0] == "exit":
                    code = int(parts[1])
                    break
        except (OSError, ValueError, asyncio.IncompleteReadError):
            pass
        if code is None:
            # заготовка упала раньше бота: бот жив сам по себе, ждём его завершения по /proc
            while _alive(self.pid):
                await asyncio.sleep(1)
            code = -1
        self.returncode = code
        try:
            self._writer.close()
        except Exception:
            pass
        if not self._done.done():
            self._done.set_result(code)

    async def wait(self) -> int:
        return await asyncio.shield(self._done)


class Zygote:
    def __init__(self, modules_dir: Path, socket_path: Path, env: dict[str, str]) -> None:
        self.modules_dir = modules_dir
        self.socket_path = socket_path
        self.env = env
        self.process: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()
        self.failed_at = 0.0
        self._log_task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def ensure(self) -> bool:
        async with self.lock:
            if self.running and self.socket_path.exists():
                return True
            if time.monotonic() - self.failed_at < 60:
                return False
            try:
                self.socket_path.parent.mkdir(parents=True, exist_ok=True)
                self.process = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "baksi_zygote", str(self.socket_path),
                    cwd=str(self.modules_dir / "kit"), env=self.env,
                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT, start_new_session=True)
                line = await asyncio.wait_for(self.process.stdout.readline(), 60)
                if not line.startswith(b"zygote ready"):
                    raise RuntimeError(line.decode(errors="replace").strip() or "заготовка не ответила")
                self._log_task = asyncio.create_task(self._drain())
                log.info("Заготовка для ботов запущена, pid %s", self.process.pid)
                return True
            except Exception as exc:
                log.warning("Заготовка не запустилась (%s), боты будут запускаться обычным способом", exc)
                self.failed_at = time.monotonic()
                if self.process and self.process.returncode is None:
                    self.process.kill()
                self.process = None
                return False

    async def _drain(self) -> None:
        assert self.process and self.process.stdout
        while True:
            line = await self.process.stdout.readline()
            if not line:
                log.warning("Заготовка завершилась, при следующем запуске бота поднимется заново")
                return
            log.info("zygote: %s", line.decode(errors="replace").rstrip())

    async def spawn(self, entry: str, cwd: Path, env: dict[str, str]) -> ZygoteProcess | None:
        if not await self.ensure():
            return None
        loop = asyncio.get_running_loop()
        out_r, out_w = os.pipe()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.setblocking(False)
            await loop.sock_connect(sock, str(self.socket_path))
            payload = json.dumps({"entry": entry, "cwd": str(cwd), "env": env}).encode()
            sock.setblocking(True)
            sock.settimeout(5)
            socket.send_fds(sock, [payload], [out_w])
            sock.setblocking(False)
        except OSError as exc:
            log.warning("Не удалось передать запуск заготовке: %s", exc)
            sock.close()
            os.close(out_r)
            os.close(out_w)
            return None
        finally:
            try:
                os.close(out_w)
            except OSError:
                pass
        control, writer = await asyncio.open_unix_connection(sock=sock)
        try:
            first = await asyncio.wait_for(control.readline(), 10)
            pid = int(first.decode().split()[1])
        except (asyncio.TimeoutError, ValueError, IndexError):
            writer.close()
            os.close(out_r)
            return None
        stdout = asyncio.StreamReader(limit=1 << 20)
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(stdout), os.fdopen(out_r, "rb", 0))
        return ZygoteProcess(pid, stdout, control, writer)

    async def close(self) -> None:
        if self.running:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                self.process.kill()
