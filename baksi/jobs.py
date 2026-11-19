"""Фоновые задачи: оплаты, автопродление, напоминания, чистка, бэкапы, здоровье сервера."""
from __future__ import annotations

import asyncio
import logging
import shutil
import time

from aiogram.types import FSInputFile

from .app import App
from .billing import money
from .catalog import plan_label
from .cryptopay import PaymentError
from .db import now
from .hub.ui import b, bot_card_kb, kb

log = logging.getLogger("baksi.jobs")


async def handle_paid(app: App, invoice: dict) -> None:
    """Зачисление: уведомить и выполнить покупку, ради которой пополняли."""
    app.notifier.send(invoice["user_id"], f"✅ Баланс пополнен на {money(invoice['amount'])}")
    if invoice.get("referrer") and invoice.get("bonus"):
        app.notifier.send(invoice["referrer"], f"🎁 Реферальный бонус: +{money(invoice['bonus'])}")
    instance_id = await app.billing.run_intent(invoice)
    if instance_id:
        inst = await app.db.one("SELECT * FROM instances WHERE id = ?", instance_id)
        module = app.catalog.get(inst["product"])
        app.notifier.send(invoice["user_id"], f"🎉 {module.title}: оплачено. "
                                              + ("Осталось подключить токен бота." if not inst["token_enc"]
                                                 else "Подписка продлена."), bot_card_kb(instance_id))
    app.sup.poke()


async def payments_loop(app: App) -> None:
    while True:
        await asyncio.sleep(15)
        try:
            for invoice in await app.pay.poll():
                await handle_paid(app, invoice)
        except PaymentError as exc:
            log.warning("Опрос CryptoBot: %s", exc)
        except Exception:
            log.exception("Ошибка опроса оплат")


async def billing_loop(app: App) -> None:
    while True:
        try:
            for ev in await app.billing.process_expired():
                inst = await app.db.one("SELECT * FROM instances WHERE id = ?", ev["instance"])
                module = app.catalog.get(inst["product"])
                name = f"{module.title if module else inst['product']} @{inst['bot_username'] or '—'}"
                if ev["kind"] == "renewed":
                    app.notifier.send(ev["user"], f"🔄 {name}: подписка продлена на {plan_label(ev['days'])}, "
                                                  f"списано {money(ev['amount'])}.")
                elif ev["kind"] == "renew_failed":
                    app.notifier.send(ev["user"], f"⛔ {name}: не хватило {money(ev['need'])} для автопродления. "
                                                  "Бот остановлен, данные сохранены. Пополните баланс и продлите.",
                                      kb([b("💳 Пополнить", "wallet:topup", style="success")],
                                         [b("🤖 Открыть бота", f"bot:{ev['instance']}")]))
                else:
                    text = "пробный период закончился" if ev.get("trial") else "подписка закончилась"
                    app.notifier.send(ev["user"], f"⛔ {name}: {text}. Бот остановлен, данные хранятся "
                                                  f"{app.cfg.purge_after_days} дней.", bot_card_kb(ev["instance"]))
            for r in await app.billing.upcoming_reminders():
                inst = r["instance"]
                module = app.catalog.get(inst["product"])
                when = "через 3 дня" if r["kind"] == "3d" else "завтра"
                name = f"{module.title if module else inst['product']} @{inst['bot_username'] or '—'}"
                if inst["trial"]:
                    text = f"⏳ {name}: пробный период закончится {when}. Выберите тариф, чтобы бот не остановился."
                elif inst["auto_renew"] and r["price"] is not None:
                    enough = inst["balance"] >= r["price"]
                    text = (f"⏳ {name}: подписка продлится {when}, спишем {money(r['price'])}." if enough else
                            f"⏳ {name}: подписка закончится {when}. Для автопродления нужно {money(r['price'])}, "
                            f"на балансе {money(inst['balance'])}.")
                else:
                    text = f"⏳ {name}: подписка закончится {when}. Автопродление выключено."
                app.notifier.send(inst["user_id"], text, bot_card_kb(inst["id"]))
            app.sup.poke()
        except Exception:
            log.exception("Ошибка биллинга")
        await asyncio.sleep(60)


async def cleanup_loop(app: App) -> None:
    while True:
        await asyncio.sleep(3600)
        try:
            ts = now()
            await app.db.run("DELETE FROM sessions WHERE expires_at < ?", ts)
            await app.db.run("DELETE FROM login_tokens WHERE expires_at < ?", ts)
            await app.db.run("DELETE FROM reminders WHERE expires_at < ?", ts - 86400)
            await app.db.run("DELETE FROM audit WHERE created_at < ?", ts - 180 * 86400)
            cutoff = ts - app.cfg.purge_after_days * 86400
            rows = await app.db.all(
                "SELECT id FROM instances WHERE (deleted_at IS NOT NULL AND deleted_at < ?) "
                "OR (deleted_at IS NULL AND expired_at IS NOT NULL AND expired_at < ?)", ts - 86400 * 3, cutoff)
            for row in rows:
                proc = app.sup.procs.get(row["id"])
                if proc and proc.running:
                    continue
                purged = app.cfg.backups_dir / "purged"
                purged.mkdir(parents=True, exist_ok=True)
                (purged / f"instance-{row['id']}.zip").write_bytes(await app.backups.instance(row["id"]))
                app.backups.purge_instance_data(row["id"])
                await app.db.run("UPDATE instances SET deleted_at = COALESCE(deleted_at, ?), token_enc = NULL, "
                                 "bot_id = NULL, status = 'deleted' WHERE id = ?", ts, row["id"])
                log.info("Данные копии %s удалены (архив в backups/purged)", row["id"])
            for old in (app.cfg.backups_dir / "purged").glob("*.zip") if (app.cfg.backups_dir / "purged").exists() else []:
                if old.stat().st_mtime < time.time() - 30 * 86400:
                    old.unlink(missing_ok=True)
        except Exception:
            log.exception("Ошибка чистки")


async def backup_loop(app: App) -> None:
    hour = 4
    while True:
        t = time.localtime()
        wait = ((hour - t.tm_hour) % 24) * 3600 - t.tm_min * 60 - t.tm_sec
        await asyncio.sleep(wait if wait > 0 else wait + 86400)
        await run_backup(app, send=True)


async def run_backup(app: App, send: bool) -> str | None:
    try:
        path = await app.backups.full()
    except Exception:
        log.exception("Бэкап не удался")
        app.notifier.owners("🔴 Ночной бэкап не удался, смотрите логи платформы.")
        return None
    if send and await app.db.get_setting("backup_to_telegram", True) and path.stat().st_size < 45_000_000:
        for oid in app.cfg.owner_ids:
            try:
                await app.bot.send_document(oid, FSInputFile(path), caption=f"💾 Бэкап {app.cfg.brand}: {path.name}",
                                            disable_notification=True)
            except Exception as exc:
                log.warning("Бэкап владельцу %s не отправлен: %s", oid, exc)
    return path.name


async def health_loop(app: App) -> None:
    alerted = 0.0
    while True:
        await asyncio.sleep(300)
        try:
            usage = shutil.disk_usage(app.cfg.data_dir)
            if usage.free < 1_000_000_000 and time.time() - alerted > 6 * 3600:
                alerted = time.time()
                app.notifier.owners(f"🟠 На сервере осталось {usage.free / 1e9:.1f} ГБ свободного места.")
        except Exception:
            log.exception("Ошибка проверки здоровья")


def start_jobs(app: App) -> list[asyncio.Task]:
    return [asyncio.create_task(coro(app), name=coro.__name__)
            for coro in (payments_loop, billing_loop, cleanup_loop, backup_loop, health_loop)]
