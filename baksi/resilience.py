"""Надёжность платформы: копии бэкапов вне сервера, запасной хаб-бот, пауза для «молчащих» ботов.

Бэкапы. На сервере хранится только последний полный бэкап (новый создан - старые удалены). Копия уходит
в приватный Telegram-канал владельца (или ему в личку): бот может отправить файл до 50 МБ, поэтому
большой архив режется на части по 45 МБ. Канал и есть история бэкапов вне сервера.

Запасной хаб. Если Telegram трижды подряд (~3 минуты) отклоняет токен хаб-бота (бот удалён или
заблокирован), а в .env задан HUB_BOT_TOKEN_RESERVE, платформа переключается на запасной бот
и перезапускается. Сообщить людям о новом боте основной уже не может, поэтому оповещение идёт:
- через их собственных ботов (владелец открывал своего бота, значит, тот может ему написать);
- от запасного бота тем, кто заранее нажал в нём /start;
- баннером на сайте.

Молчащие боты. Если в бот 14 дней не приходило ни одного сообщения и он ничего не делал сам, за 2 дня
до этого владельцу приходит предупреждение, на 14-й день бот ставится на паузу. Запуск - одной кнопкой.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from .db import now
from .tg import make_session

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger("baksi.resilience")

PART_SIZE = 45 * 1024 * 1024
IDLE_WARN_DAYS = 12
IDLE_PAUSE_DAYS = 14
FAILOVER_CHECKS = 3


# --- бэкапы ---------------------------------------------------------------------------------
def split_file(path: Path, part_size: int = PART_SIZE) -> list[Path]:
    """Режет файл на части для отправки в Telegram. Собрать обратно: cat файл.zip.0* > файл.zip"""
    if path.stat().st_size <= part_size:
        return [path]
    parts: list[Path] = []
    with path.open("rb") as src:
        index = 1
        while chunk := src.read(part_size):
            part = path.with_name(f"{path.name}.{index:03d}")
            part.write_bytes(chunk)
            parts.append(part)
            index += 1
    return parts


async def send_backup(app: "App", path: Path) -> str:
    """Отправляет бэкап в канал бэкапов (или владельцам). Возвращает, куда отправлено."""
    chat = await app.db.get_setting("backup_chat", None)
    targets = [int(chat["id"])] if chat else list(app.cfg.owner_ids)
    where = f"канал «{chat.get('title', chat['id'])}»" if chat else "владельцу в личку"
    parts = await asyncio.to_thread(split_file, path, PART_SIZE)
    size_mb = path.stat().st_size / 1048576
    failed = []
    try:
        for target in targets:
            for i, part in enumerate(parts, 1):
                caption = (f"💾 Бэкап {app.cfg.brand}: {path.name} ({size_mb:.1f} МБ)"
                           + (f"\nЧасть {i} из {len(parts)}. Собрать: cat {path.name}.0* > {path.name}"
                              if len(parts) > 1 else ""))
                for attempt in range(3):
                    try:
                        await app.bot.send_document(target, FSInputFile(part), caption=caption,
                                                    disable_notification=True, request_timeout=600)
                        break
                    except Exception as exc:
                        if attempt == 2:
                            failed.append(f"{target}: {exc}")
                        await asyncio.sleep(5)
    finally:
        for part in parts:
            if part != path:
                part.unlink(missing_ok=True)
    if failed:
        log.warning("Бэкап отправлен не везде: %s", failed)
        app.notifier.owners("🟠 Бэкап создан, но копия в Telegram не отправилась: " + "; ".join(failed)[:500]
                            + "\nПроверьте, что бот - админ канала бэкапов.")
    return where


async def set_backup_chat(app: "App", chat_id: int, title: str) -> None:
    msg = await app.bot.send_message(chat_id, f"✅ Сюда будут приходить бэкапы {app.cfg.brand}. "
                                              "Канал должен оставаться приватным.")
    await app.db.set_setting("backup_chat", {"id": chat_id, "title": title, "since": now()})
    await app.db.audit(None, "backup.chat", f"{chat_id} {title}")
    log.info("Канал бэкапов: %s (%s), проверочное сообщение %s", title, chat_id, msg.message_id)


# --- запасной хаб-бот ----------------------------------------------------------------------
async def active_hub_token(app_db, cfg) -> tuple[str, str]:
    """Какой токен хаба сейчас главный: ('main'|'reserve', токен)."""
    active = await app_db.get_setting("hub_active", "main")
    if active == "reserve" and cfg.hub_token_reserve:
        return "reserve", cfg.hub_token_reserve
    return "main", cfg.hub_token


async def reserve_username(app: "App") -> str:
    """Юзернейм запасного бота (для подсказок «нажмите Start в запасном боте»), с кэшем на сутки."""
    if not app.cfg.hub_token_reserve or app.hub_mode == "reserve":
        return ""
    cached = await app.db.get_setting("hub_reserve_info", None)
    if cached and now() - cached.get("at", 0) < 86400:
        return cached.get("username", "")
    bot = Bot(app.cfg.hub_token_reserve, session=make_session(app.cfg.proxy_url))
    try:
        me = await bot.get_me()
        await app.db.set_setting("hub_reserve_info", {"username": me.username or "", "at": now()})
        return me.username or ""
    except Exception as exc:
        log.warning("Запасной хаб-бот не отвечает: %s", exc)
        return ""
    finally:
        await bot.session.close()


async def failover_loop(app: "App") -> None:
    """Следит за хаб-ботом. Переключение только при явном отказе Telegram (401), не при сбоях сети."""
    fails = 0
    while True:
        await asyncio.sleep(60)
        try:
            await app.bot.get_me()
            fails = 0
            continue
        except TelegramUnauthorizedError:
            fails += 1
            log.error("Telegram отклонил токен хаб-бота (%s/%s)", fails, FAILOVER_CHECKS)
        except (TelegramNetworkError, asyncio.TimeoutError, OSError):
            continue
        except Exception:
            log.exception("Проверка хаб-бота")
            continue
        if fails < FAILOVER_CHECKS:
            continue
        await switch_to_reserve(app)
        return


async def switch_to_reserve(app: "App") -> None:
    old = app.hub_username
    if app.hub_mode == "reserve" or not app.cfg.hub_token_reserve:
        text = (f"🔴 Хаб-бот @{old} недоступен: Telegram отклоняет токен, а запасного бота "
                + ("тоже нет в рабочем состоянии." if app.hub_mode == "reserve" else "не задано (HUB_BOT_TOKEN_RESERVE).")
                + " Создайте нового бота в @BotFather и пропишите его токен в .env, затем перезапустите платформу.")
        log.error(text)
        await notify_via_client_bots(app, text, only_owners=True)
        await app.db.set_setting("site_notice", "Бот управления временно недоступен, ваши боты работают. "
                                                "Скоро здесь появится новая ссылка.")
        return
    await app.db.set_setting("hub_active", "reserve")
    await app.db.set_setting("hub_failover", {"old": old, "at": now(), "announced": False})
    await app.db.audit(None, "hub.failover", f"@{old}")
    log.error("Переключаюсь на запасной хаб-бот и перезапускаю платформу")
    app.request_stop()


async def announce_after_failover(app: "App") -> int:
    """После перезапуска на запасном боте: всем рассказать о новом боте управления."""
    info = await app.db.get_setting("hub_failover", None)
    if not info or info.get("announced"):
        return 0
    new = app.hub_username
    old = info.get("old", "")
    await app.db.set_setting("site_notice", f"Новый бот управления: @{new}. Прежний (@{old}) больше не работает, "
                                            "ваши боты и баланс на месте.")
    text = (f"⚠️ <b>{app.cfg.brand}: новый бот управления @{new}</b>\n\n"
            f"Прежний бот @{old} заблокирован или удалён Telegram. Ваши боты работают как обычно, баланс на месте.\n"
            f"Откройте @{new} и нажмите «Запустить», чтобы снова получать уведомления и управлять ботами.")
    sent = await notify_via_client_bots(app, text)
    users = await app.db.all("SELECT id FROM users WHERE banned = 0")
    for u in users:  # дойдёт до тех, кто заранее нажал /start в запасном боте
        app.notifier.send(u["id"], text)
    info["announced"] = True
    await app.db.set_setting("hub_failover", info)
    app.notifier.owners(f"✅ Платформа работает на запасном хаб-боте @{new}. Сообщение о новом боте отправлено "
                        f"через {sent} клиентских ботов и разослано тем, кто открывал @{new}.\n"
                        "Создайте новый запасной бот и пропишите его в HUB_BOT_TOKEN_RESERVE, а в HUB_BOT_TOKEN - "
                        "токен @" + new + ". Не забудьте /setdomain в @BotFather для входа на сайте.")
    return sent


async def notify_via_client_bots(app: "App", text: str, only_owners: bool = False) -> int:
    """Пишет владельцам клиентских ботов от имени их же ботов. Работает, если владелец хоть раз открывал свой бот."""
    rows = await app.db.all("SELECT id, user_id, token_enc FROM instances WHERE deleted_at IS NULL AND token_enc IS NOT NULL "
                            "AND token_bad = 0 ORDER BY id")
    done: set[int] = set()
    sent = 0
    for row in rows:
        uid = row["user_id"]
        if uid in done or (only_owners and not app.is_owner(uid)):
            continue
        token = app.vault.decrypt(row["token_enc"])
        if not token:
            continue
        bot = Bot(token, session=make_session(app.cfg.proxy_url))
        try:
            await bot.send_message(uid, text, parse_mode="HTML", disable_web_page_preview=True)
            done.add(uid)
            sent += 1
        except Exception:
            pass  # владелец не открывал этого бота - попробуем через следующего его бота
        finally:
            await bot.session.close()
        await asyncio.sleep(0.05)
    return sent


# --- молчащие боты --------------------------------------------------------------------------
async def idle_check(app: "App") -> tuple[int, int]:
    """Предупреждает и ставит на паузу ботов, в которых 14 дней не было никакой активности."""
    warned = paused = 0
    ts = now()
    rows = await app.db.all(
        "SELECT * FROM instances WHERE deleted_at IS NULL AND paused = 0 AND frozen = 0 AND token_enc IS NOT NULL "
        "AND token_bad = 0 AND status IN ('running', 'waiting_bind') AND last_activity_at IS NOT NULL")
    for inst in rows:
        idle_days = (ts - inst["last_activity_at"]) / 86400
        name = f"@{inst['bot_username']}" if inst["bot_username"] else inst["product"]
        if idle_days >= IDLE_PAUSE_DAYS:
            await app.db.run("UPDATE instances SET paused = 1, idle_paused_at = ? WHERE id = ?", ts, inst["id"])
            await app.db.audit(None, "instance.idle_pause", str(inst["id"]))
            app.notifier.send(inst["user_id"], (
                f"⏸ <b>Бот {name} поставлен на паузу</b>: в нём {IDLE_PAUSE_DAYS} дней не было ни одного сообщения. "
                "Так сервер не тратит ресурсы на неиспользуемых ботов.\n\nВсе данные и настройки сохранены. Нужен - "
                "запустите кнопкой ниже, он сразу заработает. Подписка идёт как обычно."),
                InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                    text="▶️ Запустить", callback_data=f"resume:{inst['id']}", style="success")]]))
            paused += 1
        elif idle_days >= IDLE_WARN_DAYS and not inst["idle_warned_at"]:
            await app.db.run("UPDATE instances SET idle_warned_at = ? WHERE id = ?", ts, inst["id"])
            left = max(1, round(IDLE_PAUSE_DAYS - idle_days))
            app.notifier.send(inst["user_id"], (
                f"💤 <b>Бот {name} молчит {int(idle_days)} дней</b>: никто ему не пишет, и сам он ничего не делает.\n\n"
                f"Через {left} дн. поставим его на паузу, чтобы не тратить ресурсы сервера. Данные сохранятся, "
                "запустить можно будет одной кнопкой. Если бот нужен - нажмите «Оставить работать»."),
                InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                    text="✅ Оставить работать", callback_data=f"keep:{inst['id']}", style="primary")]]))
            warned += 1
    return warned, paused


async def touch(app: "App", instance_id: int) -> None:
    """Бот проявил активность: сбросить отсчёт тишины."""
    await app.db.run("UPDATE instances SET last_activity_at = ?, idle_warned_at = NULL WHERE id = ?", now(),
                     instance_id)


async def idle_loop(app: "App") -> None:
    while True:
        await asyncio.sleep(3600)
        try:
            await idle_check(app)
        except Exception:
            log.exception("Проверка молчащих ботов")


async def ensure_columns(db) -> None:
    """Колонки для учёта активности (добавляются один раз)."""
    cols = {r["name"] for r in await db.all("PRAGMA table_info(instances)")}
    for name in ("last_activity_at", "idle_warned_at", "idle_paused_at"):
        if name not in cols:
            await db.run(f"ALTER TABLE instances ADD COLUMN {name} INTEGER")
    # уже работающим ботам отсчёт начинается с момента обновления, а не с создания
    await db.run("UPDATE instances SET last_activity_at = ? WHERE last_activity_at IS NULL", now())
