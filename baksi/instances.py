"""Копии ботов клиентов: токен, настройки, пауза, удаление, события от самих ботов."""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any

from .billing import DAY
from .catalog import Catalog, plan_label
from .config import Config
from .crypto import Vault, bind_code
from .db import Database, now
from .notify import Notifier
from .supervisor import Supervisor
from .tg import BotInfo, NetworkError, TokenError, check_token

log = logging.getLogger("baksi.instances")

STATUS_TEXT = {
    "setup": "Нужен токен",
    "starting": "Запускается",
    "running": "Работает",
    "waiting_bind": "Ждёт группу",
    "restarting": "Перезапуск",
    "crashed": "Сбой",
    "token_invalid": "Токен недействителен",
    "stopped": "Остановлен",
    "frozen": "Заморожен",
    "expired": "Подписка истекла",
    "queued": "В очереди",
    "unavailable": "Недоступен",
    "deleted": "Удалён",
}


class InstanceError(Exception):
    pass


class Instances:
    def __init__(self, cfg: Config, db: Database, catalog: Catalog, vault: Vault, sup: Supervisor,
                 notifier: Notifier) -> None:
        self.cfg = cfg
        self.db = db
        self.catalog = catalog
        self.vault = vault
        self.sup = sup
        self.notifier = notifier
        self.hub_bot_id: int | None = None
        self._problem_sent: dict[tuple[int, str], float] = {}

    # --- чтение ---------------------------------------------------------------
    async def get(self, instance_id: int, user_id: int | None = None) -> Any:
        row = await self.db.one("SELECT * FROM instances WHERE id = ? AND deleted_at IS NULL", instance_id)
        if not row or (user_id is not None and row["user_id"] != user_id):
            raise InstanceError("Бот не найден")
        return row

    async def for_user(self, user_id: int) -> list[Any]:
        return await self.db.all("SELECT * FROM instances WHERE user_id = ? AND deleted_at IS NULL "
                                 "ORDER BY id", user_id)

    def view(self, row: Any, with_logs: bool = False) -> dict[str, Any]:
        module = self.catalog.get(row["product"])
        settings = json.loads(row["settings"] or "{}")
        state = json.loads(row["state"] or "{}")
        data = {
            "id": row["id"],
            "user_id": row["user_id"],
            "product": row["product"],
            "title": module.title if module else row["product"],
            "color": module.color if module else "#888888",
            "icon": module.icon if module else "bot",
            "bot_username": row["bot_username"],
            "bot_name": row["bot_name"],
            "has_token": bool(row["token_enc"]),
            "status": row["status"],
            "status_text": STATUS_TEXT.get(row["status"], row["status"]),
            "status_note": row["status_note"],
            "expires_at": row["expires_at"],
            "forever": bool(row["forever"]),
            "plan_days": row["plan_days"],
            "plan_label": plan_label(row["plan_days"]) if row["plan_days"] is not None else None,
            "auto_renew": bool(row["auto_renew"]),
            "trial": bool(row["trial"]),
            "paused": bool(row["paused"]),
            "frozen": bool(row["frozen"]),
            "needs_bind": bool(module and module.needs_bind),
            "bind_code": row["bind_code"],
            "bind_hint": module.bind_hint if module else "",
            "group": {"id": state.get("chat_id"), "title": state.get("title")} if state.get("chat_id") else None,
            "bot_changed": {"at": state["bot_changed_at"], "old": state.get("old_bot"),
                            "hint": module.bot_change_hint if module else ""}
            if state.get("bot_changed_at") and now() - state["bot_changed_at"] < 7 * 86400 else None,
            "settings": settings,
            "settings_schema": module.public()["settings"] if module else [],
            "runtime": self.sup.runtime(row["id"]),
            "created_at": row["created_at"],
        }
        if with_logs:
            data["logs"] = self.sup.logs(row["id"], 80)
        return data

    # --- токен ----------------------------------------------------------------
    async def set_token(self, user_id: int | None, instance_id: int, token: str) -> BotInfo:
        row = await self.get(instance_id, user_id)
        try:
            info = await check_token(token, self.cfg.proxy_url)
        except NetworkError:
            raise InstanceError("Telegram сейчас не отвечает, попробуйте через минуту")
        except TokenError as exc:
            raise InstanceError(str(exc))
        if self.hub_bot_id and info.id == self.hub_bot_id:
            raise InstanceError("Это токен самой платформы. Создайте нового бота в @BotFather")
        other = await self.db.one("SELECT id FROM instances WHERE bot_id = ? AND id != ? AND deleted_at IS NULL",
                                  info.id, instance_id)
        if other:
            raise InstanceError(f"Бот @{info.username} уже подключён к другой копии. Создайте отдельного бота")
        changed = bool(row["bot_id"]) and row["bot_id"] != info.id
        try:
            await self.db.run("UPDATE instances SET token_enc = ?, bot_id = ?, bot_username = ?, bot_name = ?, "
                              "token_bad = 0 WHERE id = ?", self.vault.encrypt(token.strip()), info.id,
                              info.username, info.name, instance_id)
        except sqlite3.IntegrityError:  # тот же бот одновременно подключили к другой копии
            raise InstanceError(f"Бот @{info.username} уже подключён к другой копии. Создайте отдельного бота")
        await self.db.audit(user_id, "set_token", f"instance:{instance_id}", f"@{info.username}")
        log.info("Копия %s: токен @%s", instance_id, info.username)
        if changed:
            await self._bot_changed(row, info)
        await self.sup.restart(instance_id)
        return info

    async def _bot_changed(self, row: Any, info: BotInfo) -> None:
        """Подключили ДРУГОГО бота (старого удалили). Новый бот не состоит в группах и каналах старого,
        а пользователи ещё не нажимали у него /start. Данные, тексты и картинки (они хранятся на диске) остаются."""
        instance_id = row["id"]
        log.info("Копия %s сменила бота %s -> %s", instance_id, row["bot_id"], info.id)
        module = self.catalog.get(row["product"])
        state = json.loads(row["state"] or "{}")
        old_group = state.pop("title", None) if state.get("chat_id") else None
        state.pop("chat_id", None)
        state["bot_changed_at"] = now()
        state["old_bot"] = row["bot_username"]
        await self.db.run("UPDATE instances SET state = ? WHERE id = ?", json.dumps(state, ensure_ascii=False),
                          instance_id)
        kit_state = self.cfg.instances_dir / str(instance_id) / "kit_state.json"
        if kit_state.exists():  # привязка группы была у старого бота
            try:
                ks = json.loads(kit_state.read_text(encoding="utf-8"))
                ks.pop("chat_id", None)
                ks.pop("title", None)
                kit_state.write_text(json.dumps(ks, ensure_ascii=False), encoding="utf-8")
            except (OSError, ValueError):
                kit_state.unlink(missing_ok=True)
        self.sup.status_cache.pop(instance_id, None)
        hint = module.bot_change_hint if module else ""
        text = (f"🔄 {module.title if module else 'Бот'}: подключён новый бот @{info.username} вместо "
                f"@{row['bot_username'] or '—'}. Все данные и настройки на месте.\n\n"
                "Что нужно сделать, потому что это другой бот:\n" + hint)
        if old_group and module and module.needs_bind:
            text += f"\n\nПрежняя группа: «{old_group}». Привязка сброшена: добавьте нового бота и отправьте " \
                    f"<code>/bind {row['bind_code']}</code>."
        self.notifier.send(row["user_id"], text)

    # --- управление -----------------------------------------------------------
    async def set_flag(self, user_id: int | None, instance_id: int, flag: str, value: bool) -> None:
        if flag not in ("auto_renew", "paused", "frozen"):
            raise InstanceError("Неизвестное действие")
        await self.get(instance_id, user_id)
        await self.db.run(f"UPDATE instances SET {flag} = ? WHERE id = ?", int(value), instance_id)
        await self.db.audit(user_id, flag, f"instance:{instance_id}", str(value))
        self.sup.poke()

    async def set_plan(self, user_id: int, instance_id: int, days: int) -> None:
        row = await self.get(instance_id, user_id)
        if not await self.catalog.plan(row["product"], days):
            raise InstanceError("Этот тариф сейчас не продаётся")
        await self.db.run("UPDATE instances SET plan_days = ? WHERE id = ?", days, instance_id)

    async def restart(self, user_id: int | None, instance_id: int) -> None:
        row = await self.get(instance_id, user_id)
        if row["token_bad"] and row["token_enc"]:
            # владелец мог восстановить бота в @BotFather - проверим токен ещё раз
            token = self.vault.decrypt(row["token_enc"])
            if token:
                await self.set_token(user_id, instance_id, token)
                return
        await self.sup.restart(instance_id)

    async def new_bind_code(self, user_id: int, instance_id: int) -> str:
        await self.get(instance_id, user_id)
        code = bind_code()
        await self.db.run("UPDATE instances SET bind_code = ? WHERE id = ?", code, instance_id)
        await self.sup.restart(instance_id)
        return code

    async def update_settings(self, user_id: int | None, instance_id: int, values: dict[str, Any]) -> dict:
        row = await self.get(instance_id, user_id)
        module = self.catalog.get(row["product"])
        if not module:
            raise InstanceError("Модуль недоступен")
        current = json.loads(row["settings"] or "{}")
        schema = {s.key: s for s in module.settings}
        need_restart = False
        for key, value in values.items():
            s = schema.get(key)
            if not s:
                raise InstanceError(f"Неизвестная настройка {key}")
            if s.type == "bool":
                value = bool(value)
            elif s.type == "int":
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    raise InstanceError(f"«{s.label}»: нужно число")
            elif s.type == "ids":
                items = value if isinstance(value, list) else str(value).replace(",", " ").split()
                try:
                    value = sorted({int(str(x).strip()) for x in items if str(x).strip()})
                except ValueError:
                    raise InstanceError(f"«{s.label}»: укажите числовые Telegram ID")
                if len(value) > 50:
                    raise InstanceError(f"«{s.label}»: не больше 50")
            elif s.type == "choice":
                if value not in {o[0] for o in s.options}:
                    raise InstanceError(f"«{s.label}»: недопустимое значение")
            else:
                value = str(value)[:2000]
            if current.get(key) != value:
                current[key] = value
                need_restart = need_restart or s.restart
        await self.db.run("UPDATE instances SET settings = ? WHERE id = ?", json.dumps(current, ensure_ascii=False),
                          instance_id)
        await self.db.audit(user_id, "settings", f"instance:{instance_id}", json.dumps(values, ensure_ascii=False))
        if need_restart:
            await self.sup.restart(instance_id)
        return current

    async def delete(self, user_id: int | None, instance_id: int) -> None:
        await self.get(instance_id, user_id)
        await self.db.run("UPDATE instances SET deleted_at = ?, status = 'deleted', bot_id = NULL WHERE id = ?",
                          now(), instance_id)
        await self.db.audit(user_id, "delete", f"instance:{instance_id}")
        proc = self.sup.procs.get(instance_id)
        if proc and proc.running:
            await self.sup._stop(proc, "удалён")
        self.sup.poke()

    async def gift_days(self, actor: int, instance_id: int, days: int) -> None:
        row = await self.get(instance_id)
        if row["forever"]:
            return
        base = max(now(), row["expires_at"] or 0)
        await self.db.run("UPDATE instances SET expires_at = ?, expired_at = NULL WHERE id = ?",
                          base + days * DAY, instance_id)
        await self.db.audit(actor, "gift_days", f"instance:{instance_id}", str(days))
        self.sup.poke()

    # --- события от ботов -----------------------------------------------------
    async def on_event(self, instance_id: int, event: dict) -> None:
        kind = event.get("event")
        row = await self.db.one("SELECT * FROM instances WHERE id = ?", instance_id)
        if not row:
            return
        state = json.loads(row["state"] or "{}")
        if kind == "ready":
            await self.db.run("UPDATE instances SET bot_username = ?, bot_name = ? WHERE id = ?",
                              event.get("username") or row["bot_username"], event.get("name") or row["bot_name"],
                              instance_id)
            if event.get("chat_id") and not state.get("chat_id"):
                state["chat_id"] = event["chat_id"]
        elif kind == "bound":
            first = not state.get("chat_id")
            state.update(chat_id=event.get("chat_id"), title=event.get("title"))
            module = self.catalog.get(row["product"])
            self.notifier.send(row["user_id"], f"✅ {module.title if module else 'Бот'} @{row['bot_username']}: "
                                               f"группа «{event.get('title')}» привязана." +
                               (" Бот готов к работе." if first else ""))
        elif kind == "unbound":
            if state.get("chat_id") == event.get("chat_id"):
                state.pop("chat_id", None)
                state.pop("title", None)
        elif kind == "notify":
            text = str(event.get("text", ""))[:1500]
            if text:
                self.notifier.send(row["user_id"], f"@{row['bot_username']}: {text}")
            return
        elif kind == "state":
            key = str(event.get("key", ""))
            if key and key not in ("chat_id",):
                state[key] = event.get("value")
        else:
            return
        await self.db.run("UPDATE instances SET state = ? WHERE id = ?", json.dumps(state, ensure_ascii=False),
                          instance_id)
        self.sup.status_cache.pop(instance_id, None)
        self.sup.poke()

    async def on_problem(self, instance_id: int, kind: str, details: str) -> None:
        key = (0 if kind in ("low_memory", "capacity") else instance_id, kind)  # про сервер - одно сообщение в час
        if time.time() - self._problem_sent.get(key, 0) < 3600:
            return
        self._problem_sent[key] = time.time()
        row = await self.db.one("SELECT * FROM instances WHERE id = ?", instance_id)
        if not row:
            return
        name = f"@{row['bot_username']}" if row["bot_username"] else f"#{instance_id}"
        if kind == "token":
            self.notifier.send(row["user_id"],
                               f"⚠️ {name} остановлен: Telegram отклонил токен. Бота удалили или сбросили токен "
                               "в @BotFather.\nДанные и настройки сохранены. Откройте «Мои боты» → бот → "
                               "«Сменить токен» и вставьте новый.")
        elif kind == "crashloop":
            self.notifier.send(row["user_id"], f"⚠️ {name} несколько раз упал при запуске. Мы уже знаем о проблеме, "
                                               "бот попробует запуститься снова через 10 минут.")
            self.notifier.owners(f"🔴 Копия #{instance_id} ({row['product']}, {name}) в цикле падений.\n"
                                 f"Последние строки лога:\n{details[-1500:]}")
        elif kind == "memory":
            self.notifier.owners(f"🟠 Копия #{instance_id} ({row['product']}, {name}) превысила лимит памяти "
                                 f"({details}) и перезапущена.")
        elif kind == "low_memory":
            self.notifier.owners(f"🔴 На сервере осталось {details} свободной памяти. Новые боты не запускаются и ждут "
                                 "в очереди. Каждый бот занимает ~180 МБ: добавьте памяти серверу или освободите её.")
        elif kind == "capacity":
            self.notifier.owners("🟠 Достигнут лимит одновременно работающих ботов (MAX_INSTANCES). "
                                 "Новые копии ждут в очереди.")
