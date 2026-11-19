"""Заготовка для запуска ботов: python -m baksi_zygote <путь к сокету>.

Один раз загружает тяжёлые библиотеки (aiogram, pydantic, aiohttp, aiosqlite) и дальше запускает каждого
бота через fork. Каждый бот остаётся отдельным процессом со своей памятью: падение одного не задевает
остальных. Но код библиотек в памяти общий, поэтому бот занимает в несколько раз меньше (замер:
20 ботов - 266 МБ вместо 1258 МБ при обычном запуске).

Протокол (одно соединение на одного бота):
  платформа -> заготовка: строка JSON {"entry", "cwd", "env"} и дескриптор канала для вывода (SCM_RIGHTS)
  заготовка -> платформа: "pid <N>\\n", затем, когда бот завершится, "exit <код>\\n"
Если заготовка падает, уже запущенные боты продолжают работать, платформа поднимет её заново.
"""
from __future__ import annotations

import gc
import io
import json
import os
import select
import signal
import socket
import sys
import traceback


def preload() -> None:
    """Тяжёлое, но безопасное для fork: только импорт, без потоков, циклов событий и соединений."""
    import asyncio  # noqa: F401
    import aiohttp  # noqa: F401
    import aiosqlite  # noqa: F401
    import aiogram  # noqa: F401
    import aiogram.methods  # noqa: F401
    import aiogram.types  # noqa: F401
    from aiogram import Bot, Dispatcher, F, Router  # noqa: F401
    from aiogram.client.default import DefaultBotProperties  # noqa: F401
    from aiogram.client.session.aiohttp import AiohttpSession  # noqa: F401
    from aiogram.exceptions import TelegramBadRequest  # noqa: F401
    from aiogram.filters import Command, CommandStart  # noqa: F401
    from aiogram.fsm.context import FSMContext  # noqa: F401
    from aiogram.fsm.state import State, StatesGroup  # noqa: F401
    from aiogram.fsm.storage.memory import MemoryStorage  # noqa: F401
    from aiogram.utils.keyboard import InlineKeyboardBuilder  # noqa: F401
    for optional in ("uvloop", "aiohttp_socks"):
        try:
            __import__(optional)
        except ImportError:
            pass
    # модели aiogram собираются лениво: разбор одного апдейта прогревает их один раз для всех ботов
    from aiogram.types import Update
    Update.model_validate({"update_id": 1, "message": {"message_id": 1, "date": 0, "text": "x",
                                                       "chat": {"id": 1, "type": "private"},
                                                       "from": {"id": 1, "is_bot": False, "first_name": "x"}}})


def run_child(request: dict, out_fd: int, close_fds: list[int]) -> None:
    """Код процесса бота после fork. Никогда не возвращается."""
    code = 0
    try:
        for fd in close_fds:
            try:
                os.close(fd)
            except OSError:
                pass
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        os.setsid()
        devnull = os.open(os.devnull, os.O_RDONLY)
        os.dup2(devnull, 0)
        os.dup2(out_fd, 1)
        os.dup2(out_fd, 2)
        os.close(devnull)
        os.close(out_fd)
        sys.stdout = io.TextIOWrapper(io.FileIO(1, "w", closefd=False), encoding="utf-8", line_buffering=True,
                                      write_through=True)
        sys.stderr = io.TextIOWrapper(io.FileIO(2, "w", closefd=False), encoding="utf-8", line_buffering=True,
                                      write_through=True)
        os.chdir(request["cwd"])
        os.environ.clear()
        os.environ.update(request["env"])
        for path in reversed([p for p in request["env"].get("PYTHONPATH", "").split(os.pathsep) if p]):
            if path not in sys.path:
                sys.path.insert(0, path)
        gc.unfreeze()
        import baksi_launch
        sys.argv = ["baksi_launch", request["entry"]]
        baksi_launch.main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    except BaseException:
        traceback.print_exc()
        code = 1
    finally:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        os._exit(code)


def serve(path: str) -> None:
    preload()
    gc.collect()
    gc.freeze()  # объекты библиотек не трогаем сборщиком мусора: страницы памяти остаются общими
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    os.chmod(path, 0o600)
    server.listen(64)
    wake_r, wake_w = os.pipe()
    os.set_blocking(wake_w, False)
    signal.signal(signal.SIGCHLD, lambda *_: os.write(wake_w, b"x"))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    children: dict[int, socket.socket] = {}
    print(f"zygote ready pid {os.getpid()}", flush=True)
    while True:
        try:
            ready, _, _ = select.select([server, wake_r], [], [])
        except InterruptedError:
            continue
        if wake_r in ready:
            os.read(wake_r, 4096)
            while True:
                try:
                    pid, status = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    break
                if pid == 0:
                    break
                conn = children.pop(pid, None)
                if conn:
                    try:
                        conn.sendall(f"exit {os.waitstatus_to_exitcode(status)}\n".encode())
                    except OSError:
                        pass
                    conn.close()
        if server in ready:
            conn, _ = server.accept()
            try:
                conn.settimeout(5)
                msg, fds, _, _ = socket.recv_fds(conn, 1 << 16, 1)
                if not fds:
                    conn.close()
                    continue
                request = json.loads(msg.decode())
                conn.settimeout(None)
            except (OSError, ValueError):
                conn.close()
                continue
            out_fd = fds[0]
            others = [server.fileno(), wake_r, wake_w, conn.fileno(), *(c.fileno() for c in children.values())]
            pid = os.fork()
            if pid == 0:
                run_child(request, out_fd, others)
            os.close(out_fd)
            children[pid] = conn
            try:
                conn.sendall(f"pid {pid}\n".encode())
            except OSError:
                pass


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m baksi_zygote <socket>")
    serve(sys.argv[1])
