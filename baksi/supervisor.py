"""Супервизор копий ботов.

Каждая копия - отдельный процесс модуля со своим токеном и своей папкой данных. Супервизор раз в несколько
секунд сверяет, что ДОЛЖНО работать (оплачено, есть токен, не на паузе) с тем, что работает, и чинит разницу:
- упал процесс - перезапуск с растущей паузой (2 с, 4 с … 5 мин);
- 5 падений за 10 минут - статус «сбой», владелец бота и администрация получают уведомление, повтор через 10 минут;
- процесс съел больше памяти, чем разрешено модулю, - перезапуск;
- Telegram отклонил токен - бот не перезапускается в цикле, владельцу приходит «вставьте новый токен».
Модуль сообщает о себе строками `@@baksi {json}` в stdout (готов, привязана группа и т.п.).
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import re
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .catalog import Catalog, Module
from .config import Config
from .crypto import Vault
from .db import Database, now
from .tg import NetworkError, TokenError, check_token

log = logging.getLogger("baksi.supervisor")

EVENT_PREFIX = "@@baksi "
LOG_MAX_BYTES = 1_000_000
LOG_TAIL = 300
CRASH_WINDOW = 600
CRASH_LIMIT = 5
CRASH_COOLDOWN = 600
STOP_TIMEOUT = 10
RECONCILE_EVERY = 5
WATCHDOG_EVERY = 15

TOKEN_IN_TEXT = re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,50}")

Hook = Callable[..., Awaitable[None]]


@dataclass
class Proc:
    instance_id: int
    module: Module
    process: asyncio.subprocess.Process | None = None
    started_at: float = 0.0
    tail: collections.deque = field(default_factory=lambda: collections.deque(maxlen=LOG_TAIL))
    crashes: list[float] = field(default_factory=list)
    retry_at: float = 0.0
    crashed_until: float = 0.0
    stopping: bool = False
    saw_unauthorized: bool = False
    rss_mb: float = 0.0
    last_exit: int | None = None
    restarts: int = 0
    reader: asyncio.Task | None = None
    waiter: asyncio.Task | None = None
    log_file: Path | None = None

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.returncode is None


def _rss_mb(pid: int) -> float:
    try:
        with open(f"/proc/{pid}/status", encoding="ascii") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except (OSError, ValueError):
        pass
    return 0.0


class Supervisor:
    def __init__(self, cfg: Config, db: Database, catalog: Catalog, vault: Vault) -> None:
        self.cfg = cfg
        self.db = db
        self.catalog = catalog
        self.vault = vault
        self.procs: dict[int, Proc] = {}
        self.status_cache: dict[int, tuple[str, str]] = {}
        self.ui: dict[str, dict] = {}  # оформление от владельца по типам ботов (settings ui:<slug>)
        self.start_sem = asyncio.Semaphore(3)
        self.wake = asyncio.Event()
        self.closing = False
        # Хуки, которые подставляет приложение (уведомления, сохранение состояния)
        self.on_event: Hook | None = None      # (instance_id, event: dict)
        self.on_problem: Hook | None = None    # (instance_id, kind: str, details: str)

    # --- публичное ------------------------------------------------------------
    def poke(self) -> None:
        """Пересверить состояние немедленно (после оплаты, смены токена и т.п.)."""
        self.wake.set()

    async def run(self) -> None:
        self.cfg.instances_dir.mkdir(parents=True, exist_ok=True)
        self.cfg.logs_dir.mkdir(parents=True, exist_ok=True)
        last_watch = 0.0
        while not self.closing:
            try:
                await self.reconcile()
                if time.monotonic() - last_watch >= WATCHDOG_EVERY:
                    last_watch = time.monotonic()
                    await self.watchdog()
            except Exception:
                log.exception("Ошибка в цикле супервизора")
            try:
                await asyncio.wait_for(self.wake.wait(), RECONCILE_EVERY)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()

    async def shutdown(self) -> None:
        self.closing = True
        self.wake.set()
        await asyncio.gather(*(self._stop(p, "остановка платформы") for p in list(self.procs.values())),
                             return_exceptions=True)

    async def restart(self, instance_id: int) -> None:
        proc = self.procs.get(instance_id)
        if proc:
            proc.crashes.clear()
            proc.crashed_until = proc.retry_at = 0
            if proc.running:
                await self._stop(proc, "перезапуск")
        self.poke()

    def runtime(self, instance_id: int) -> dict[str, Any]:
        p = self.procs.get(instance_id)
        if not p:
            return {"running": False}
        return {
            "running": p.running,
            "pid": p.process.pid if p.running and p.process else None,
            "uptime": int(time.time() - p.started_at) if p.running else 0,
            "rss_mb": round(p.rss_mb, 1),
            "mem_limit_mb": p.module.mem_mb,
            "restarts": p.restarts,
            "last_exit": p.last_exit,
        }

    def logs(self, instance_id: int, limit: int = 100) -> list[str]:
        p = self.procs.get(instance_id)
        if p and p.tail:
            return list(p.tail)[-limit:]
        path = self.cfg.logs_dir / f"{instance_id}.log"
        if not path.exists():
            return []
        with path.open("rb") as f:
            f.seek(max(0, path.stat().st_size - 64_000))
            lines = f.read().decode("utf-8", "replace").splitlines()
        return lines[-limit:]

    def totals(self) -> dict[str, Any]:
        running = [p for p in self.procs.values() if p.running]
        return {"running": len(running), "rss_mb": round(sum(p.rss_mb for p in running), 1)}

    # --- сверка ---------------------------------------------------------------
    async def _set_status(self, instance_id: int, status: str, note: str = "") -> None:
        if self.status_cache.get(instance_id) == (status, note):
            return
        self.status_cache[instance_id] = (status, note)
        await self.db.run("UPDATE instances SET status = ?, status_note = ? WHERE id = ?", status, note, instance_id)

    def _blocker(self, inst: Any, module: Module | None) -> tuple[str, str] | None:
        """Почему копия не должна работать (статус, пояснение) или None, если должна."""
        if inst["deleted_at"]:
            return "deleted", ""
        if not module:
            return "unavailable", "Модуль временно недоступен"
        if not inst["token_enc"]:
            return "setup", "Нужен токен бота"
        if inst["token_bad"]:
            return "token_invalid", "Telegram отклонил токен"
        if inst["frozen"]:
            return "frozen", "Заморожен администрацией"
        if not inst["forever"] and (inst["expired_at"] or (inst["expires_at"] or 0) <= now()):
            return "expired", "Подписка закончилась"
        if inst["paused"]:
            return "stopped", "Остановлен владельцем"
        return None

    async def reconcile(self) -> None:
        rows = await self.db.all("SELECT * FROM instances WHERE deleted_at IS NULL OR id IN (%s)" %
                                 (",".join(str(i) for i in self.procs) or "0"))
        seen: set[int] = set()
        for inst in rows:
            iid = inst["id"]
            seen.add(iid)
            module = self.catalog.get(inst["product"])
            blocker = self._blocker(inst, module)
            proc = self.procs.get(iid)
            if blocker:
                if proc and proc.running:
                    await self._stop(proc, blocker[1] or blocker[0])
                if blocker[0] != "deleted":
                    await self._set_status(iid, *blocker)
                continue
            assert module
            if proc is None or proc.module.slug != module.slug:
                proc = self.procs[iid] = Proc(iid, module)
            if proc.running:
                state = json.loads(inst["state"] or "{}")
                if module.needs_bind and not state.get("chat_id"):
                    await self._set_status(iid, "waiting_bind", "Привяжите группу командой /bind")
                else:
                    await self._set_status(iid, "running", "")
                continue
            ts = time.time()
            if proc.crashed_until > ts:
                continue
            if proc.retry_at > ts:
                continue
            if sum(1 for p in self.procs.values() if p.running) >= self.cfg.max_instances:
                await self._set_status(iid, "queued", "Сервер загружен, бот запустится автоматически")
                if self.on_problem:
                    await self.on_problem(iid, "capacity", "Достигнут лимит MAX_INSTANCES")
                continue
            asyncio.create_task(self._start(proc, inst))
        for iid in list(self.procs):
            if iid not in seen and not self.procs[iid].running:
                self.procs.pop(iid, None)

    # --- запуск / остановка ---------------------------------------------------
    def write_platform_file(self, inst: Any, data_dir: Path) -> None:
        settings = json.loads(inst["settings"] or "{}")
        module = self.catalog.get(inst["product"])
        payload = {
            "instance_id": inst["id"],
            "product": inst["product"],
            "owner_ids": sorted({inst["user_id"], *[int(x) for x in settings.get("admins", []) if str(x).isdigit()]}),
            "owner_id": inst["user_id"],
            "bind_code": inst["bind_code"],
            "needs_bind": bool(module and module.needs_bind),
            "settings": settings,
            "state": json.loads(inst["state"] or "{}"),
            "brand": self.cfg.brand,
            "expires_at": inst["expires_at"],
            "forever": bool(inst["forever"]),
            "ui": self.ui.get(inst["product"], {}),
        }
        tmp = data_dir / "platform.json.tmp"
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(data_dir / "platform.json")

    async def refresh_platform_files(self, product: str) -> int:
        """Переписать platform.json всех копий типа product: боты подхватят изменения сами, без перезапуска."""
        rows = await self.db.all("SELECT * FROM instances WHERE product = ? AND deleted_at IS NULL", product)
        n = 0
        for inst in rows:
            data_dir = self.cfg.instances_dir / str(inst["id"])
            if data_dir.exists():
                self.write_platform_file(inst, data_dir)
                n += 1
        return n

    async def _start(self, proc: Proc, inst: Any) -> None:
        async with self.start_sem:
            if proc.running or self.closing:
                return
            token = self.vault.decrypt(inst["token_enc"])
            if not token:
                await self._set_status(proc.instance_id, "token_invalid", "Токен не расшифровать: сменился SECRET_KEY")
                return
            await self._set_status(proc.instance_id, "starting", "")
            data_dir = self.cfg.instances_dir / str(proc.instance_id)
            data_dir.mkdir(parents=True, exist_ok=True)
            self.write_platform_file(inst, data_dir)
            kit = str(self.cfg.modules_dir / "kit")
            env = {
                "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                "HOME": str(data_dir),
                "LANG": "C.UTF-8",
                "PYTHONUNBUFFERED": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "MALLOC_ARENA_MAX": "2",  # меньше фрагментации памяти у долгоживущих процессов
                "PYTHONPATH": kit,
                "BOT_TOKEN": token,
                "OWNER_IDS": ",".join(str(x) for x in json.loads(
                    (data_dir / "platform.json").read_text(encoding="utf-8"))["owner_ids"]),
                "DATA_DIR": str(data_dir),
                "BAKSI_INSTANCE": str(proc.instance_id),
                "BAKSI_PLATFORM_FILE": str(data_dir / "platform.json"),
                "LOG_LEVEL": "INFO",
            }
            if self.cfg.proxy_url:
                env["PROXY_URL"] = self.cfg.proxy_url
            proc.log_file = self.cfg.logs_dir / f"{proc.instance_id}.log"
            proc.saw_unauthorized = False
            proc.stopping = False
            try:
                proc.process = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "baksi_launch", proc.module.entry,
                    cwd=str(proc.module.path), env=env,
                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT, start_new_session=True,
                    limit=1 << 20,
                )
            except OSError as exc:
                log.error("Копия %s не запустилась: %s", proc.instance_id, exc)
                proc.retry_at = time.time() + 30
                await self._set_status(proc.instance_id, "crashed", f"Не удалось запустить процесс: {exc}")
                return
            proc.started_at = time.time()
            self._log_line(proc, f"=== запуск, pid {proc.process.pid} ===")
            proc.reader = asyncio.create_task(self._read(proc))
            proc.waiter = asyncio.create_task(self._wait(proc))
            await asyncio.sleep(0.3)  # не стартуем десятки процессов одновременно

    async def _stop(self, proc: Proc, reason: str) -> None:
        if not proc.running or not proc.process:
            return
        proc.stopping = True
        self._log_line(proc, f"=== остановка: {reason} ===")
        try:
            os.killpg(proc.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.process.wait(), STOP_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("Копия %s не остановилась за %s с, SIGKILL", proc.instance_id, STOP_TIMEOUT)
            try:
                os.killpg(proc.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.process.wait()

    async def _wait(self, proc: Proc) -> None:
        assert proc.process
        code = await proc.process.wait()
        if proc.reader:
            try:
                await asyncio.wait_for(proc.reader, 2)
            except asyncio.TimeoutError:
                proc.reader.cancel()
        proc.last_exit = code
        proc.rss_mb = 0
        self._log_line(proc, f"=== процесс завершился, код {code} ===")
        if proc.stopping or self.closing:
            return
        # Неожиданное падение
        if proc.saw_unauthorized:
            await self._recheck_token(proc)
            if self.closing:
                return
        ts = time.time()
        proc.restarts += 1
        proc.crashes = [t for t in proc.crashes if ts - t < CRASH_WINDOW] + [ts]
        if len(proc.crashes) >= CRASH_LIMIT:
            proc.crashed_until = ts + CRASH_COOLDOWN
            proc.crashes.clear()
            await self._set_status(proc.instance_id, "crashed", "Бот падает при запуске, повтор через 10 минут")
            if self.on_problem:
                await self.on_problem(proc.instance_id, "crashloop", "\n".join(list(proc.tail)[-12:]))
        else:
            delay = min(300, 2 ** len(proc.crashes))
            proc.retry_at = ts + delay
            await self._set_status(proc.instance_id, "restarting", f"Перезапуск через {delay} с")
        self.poke()

    async def _recheck_token(self, proc: Proc) -> None:
        row = await self.db.one("SELECT token_enc FROM instances WHERE id = ?", proc.instance_id)
        token = self.vault.decrypt(row["token_enc"]) if row else None
        if not token:
            return
        try:
            await check_token(token, self.cfg.proxy_url)
        except TokenError:
            await self.db.run("UPDATE instances SET token_bad = 1 WHERE id = ?", proc.instance_id)
            await self._set_status(proc.instance_id, "token_invalid", "Telegram отклонил токен")
            if self.on_problem:
                await self.on_problem(proc.instance_id, "token", "")
        except NetworkError:
            pass

    # --- вывод процесса -------------------------------------------------------
    def _log_line(self, proc: Proc, line: str) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        text = f"{stamp} {line}"
        proc.tail.append(text)
        if not proc.log_file:
            proc.log_file = self.cfg.logs_dir / f"{proc.instance_id}.log"
        try:
            if proc.log_file.exists() and proc.log_file.stat().st_size > LOG_MAX_BYTES:
                proc.log_file.replace(proc.log_file.with_suffix(".log.1"))
            with proc.log_file.open("a", encoding="utf-8") as f:
                f.write(text + "\n")
        except OSError:
            pass

    async def _read(self, proc: Proc) -> None:
        assert proc.process and proc.process.stdout
        while True:
            try:
                raw = await proc.process.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError):
                continue  # слишком длинная строка - пропускаем
            if not raw:
                return
            line = raw.decode("utf-8", "replace").rstrip()
            if line.startswith(EVENT_PREFIX):
                try:
                    event = json.loads(line[len(EVENT_PREFIX):])
                except ValueError:
                    continue
                if isinstance(event, dict) and self.on_event:
                    try:
                        await self.on_event(proc.instance_id, event)
                    except Exception:
                        log.exception("Ошибка обработки события %s от копии %s", event, proc.instance_id)
                continue
            if "Unauthorized" in line or "TelegramUnauthorizedError" in line:
                proc.saw_unauthorized = True
            line = TOKEN_IN_TEXT.sub("•••token•••", line)  # токены в логах не храним
            self._log_line(proc, line[:2000])

    # --- сторож памяти --------------------------------------------------------
    async def watchdog(self) -> None:
        for proc in list(self.procs.values()):
            if not proc.running or not proc.process:
                continue
            proc.rss_mb = _rss_mb(proc.process.pid)
            if proc.rss_mb > proc.module.mem_mb:
                log.warning("Копия %s: память %.0f МБ > лимита %s МБ, перезапуск",
                            proc.instance_id, proc.rss_mb, proc.module.mem_mb)
                self._log_line(proc, f"=== превышен лимит памяти: {proc.rss_mb:.0f} МБ из {proc.module.mem_mb} ===")
                proc.stopping = False
                try:
                    os.killpg(proc.process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                if self.on_problem:
                    await self.on_problem(proc.instance_id, "memory", f"{proc.rss_mb:.0f} МБ")
