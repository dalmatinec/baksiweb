"""Baksi Cloud.

python -m baksi                 - запуск платформы (хаб-бот, веб, супервизор, фоновые задачи)
python -m baksi backup          - полный бэкап сейчас
python -m baksi restore FILE    - восстановление из полного бэкапа (платформа должна быть остановлена)
python -m baksi check           - проверить настройки и модули, ничего не запуская
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
from pathlib import Path

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.types import BotCommand, ErrorEvent, MenuButtonWebApp, WebAppInfo

from .app import App
from .backup import Backups, restore_full
from .billing import Billing
from .catalog import Catalog, load_modules
from .config import Config, load_config
from .crypto import Vault
from .cryptopay import CryptoPay
from .db import Database
from .hub.handlers import router as hub_router
from .hub.middleware import Guard
from .instances import Instances
from .jobs import start_jobs
from .notify import Notifier
from .supervisor import Supervisor
from .tg import make_session
from .web.app import create_web

log = logging.getLogger("baksi")


async def build(cfg: Config) -> App:
    db = Database(cfg.db_path)
    await db.connect()
    vault = Vault(cfg.secret_key)
    catalog = Catalog(db, load_modules(cfg.modules_dir, cfg.instance_mem_mb))
    await catalog.sync()
    billing = Billing(db, catalog)
    from .resilience import active_hub_token, ensure_columns
    hub_mode, hub_token = await active_hub_token(db, cfg, vault)
    await ensure_columns(db)
    bot = Bot(hub_token, session=make_session(cfg.proxy_url),
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    notifier = Notifier(bot, db, cfg.owner_ids)
    sup = Supervisor(cfg, db, catalog, vault)
    instances = Instances(cfg, db, catalog, vault, sup, notifier)
    sup.on_event = instances.on_event
    sup.on_problem = instances.on_problem
    pay = CryptoPay(cfg.cryptopay_token, cfg.cryptopay_testnet, db, billing, cfg.brand)
    app = App(cfg=cfg, db=db, vault=vault, catalog=catalog, billing=billing, sup=sup, instances=instances,
              notifier=notifier, pay=pay, backups=Backups(cfg, db), bot=bot)
    await app.load_design()
    app.hub_mode, app.hub_token = hub_mode, hub_token
    from .accounts import Accounts
    app.accounts = Accounts(app)
    await app.accounts.migrate()
    from .usdtpay import UsdtPay
    app.usdt = UsdtPay(db, billing, notifier)
    await app.usdt.migrate()
    return app


async def run(cfg: Config) -> None:
    app = await build(cfg)
    stop = asyncio.Event()
    app.request_stop = stop.set
    try:
        me = await app.bot.get_me()
    except TelegramUnauthorizedError:
        # хаб-бот уже заблокирован или удалён: переключаемся на запасной и перезапускаемся
        if app.hub_mode != "reserve" and cfg.hub_token_reserve:
            log.error("Telegram отклоняет токен хаб-бота при запуске, переключаюсь на запасной")
            await app.db.set_setting("hub_active", "reserve")
            await app.db.set_setting("hub_failover", {"old": "", "at": int(time.time()), "announced": False})
            await app.db.close()
            raise SystemExit(3)
        raise SystemExit("Telegram отклоняет токен хаб-бота, а запасного нет (HUB_BOT_TOKEN_RESERVE)")
    app.hub_username = me.username or ""
    app.instances.hub_bot_id = me.id
    log.info("%s: хаб-бот @%s, модулей: %s (%s)", cfg.brand, me.username, len(app.catalog.modules),
             ", ".join(app.catalog.modules))

    dp = Dispatcher(storage=MemoryStorage(), app=app)
    guard = Guard(app)
    dp.message.outer_middleware(guard)
    dp.callback_query.outer_middleware(guard)
    from .hub.security import router as security_router
    dp.include_router(security_router)  # раньше основного: /recover и ввод ключа важнее общих обработчиков
    from .hub.ops import router as ops_router
    dp.include_router(ops_router)
    dp.include_router(hub_router)

    @dp.errors()
    async def on_error(event: ErrorEvent) -> bool:
        log.exception("Ошибка хаб-бота", exc_info=event.exception)
        return True

    await app.bot.set_my_commands([BotCommand(command="start", description="Главное меню")])
    if app.miniapp_url:
        await app.bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
            text="Открыть", web_app=WebAppInfo(url=app.miniapp_url)))

    web = create_web(app)
    server = uvicorn.Server(uvicorn.Config(web, host=cfg.web_host, port=cfg.web_port, log_level="warning",
                                           proxy_headers=True, forwarded_allow_ips="*", access_log=False,
                                           server_header=False))
    server.install_signal_handlers = lambda: None

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    tasks = [
        asyncio.create_task(app.sup.run(), name="supervisor"),
        asyncio.create_task(app.notifier.run(), name="notifier"),
        asyncio.create_task(dp.start_polling(app.bot, handle_signals=False,
                                             allowed_updates=dp.resolve_used_update_types()), name="hub"),
        asyncio.create_task(server.serve(), name="web"),
        *start_jobs(app),
    ]
    from .resilience import announce_after_failover, old_hub_redirect
    tasks.append(asyncio.create_task(announce_after_failover(app), name="announce"))  # после смены хаб-бота
    tasks.append(asyncio.create_task(old_hub_redirect(app), name="old_hub"))
    app.notifier.owners(f"✅ {cfg.brand} запущен. Модули: {', '.join(app.catalog.modules) or 'нет'}")
    log.info("Веб: http://%s:%s%s", cfg.web_host, cfg.web_port,
             f" (публично {cfg.public_url})" if cfg.public_url else "")

    async def crashed() -> None:
        done, _ = await asyncio.wait([t for t in tasks if t.get_name() in ("supervisor", "hub", "web")],
                                     return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            if t.exception():
                log.error("Компонент %s упал: %r", t.get_name(), t.exception())
        stop.set()

    watcher = asyncio.create_task(crashed())
    await stop.wait()
    log.info("Остановка…")
    watcher.cancel()
    server.should_exit = True
    await dp.stop_polling()
    await app.sup.shutdown()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await app.pay.close()
    await app.usdt.close()
    await app.bot.session.close()
    await app.db.close()
    log.info("Остановлено")


async def backup_now(cfg: Config) -> None:
    db = Database(cfg.db_path)
    await db.connect()
    path = await Backups(cfg, db).full()
    await db.close()
    print(path)


def check(cfg: Config) -> None:
    modules = load_modules(cfg.modules_dir, cfg.instance_mem_mb)
    print(f"Бренд: {cfg.brand}\nДанные: {cfg.data_dir}\nМодули: {', '.join(modules) or 'нет'}")
    print(f"Веб: {cfg.public_url or 'домен не задан (бот работает, веб-вход и Mini App выключены)'}")
    print(f"CryptoBot: {'подключён' if cfg.cryptopay_token else 'не подключён'}")


def main() -> None:
    cfg = load_config()
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        try:
            import uvloop
            uvloop.install()
        except ImportError:
            pass
        asyncio.run(run(cfg))
    elif cmd == "backup":
        asyncio.run(backup_now(cfg))
    elif cmd == "restore" and len(sys.argv) > 2:
        restore_full(cfg, Path(sys.argv[2]))
    elif cmd == "check":
        check(cfg)
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
