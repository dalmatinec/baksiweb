"""Хаб-бот: каталог, покупка, мои боты, кошелёк, рефералы, вход в веб, админ-команды.

Все экраны редактируют одно сообщение (без спама в чате). Токен, присланный пользователем, сразу удаляется из чата.
"""
from __future__ import annotations

import asyncio
import html
import logging
import time
from typing import Any
from urllib.parse import quote

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from ..app import App
from ..billing import BillingError, InsufficientFunds, money, parse_money
from ..catalog import plan_label
from ..crypto import new_token
from ..cryptopay import PaymentError
from ..db import now
from ..instances import InstanceError
from .ui import b, kb

log = logging.getLogger("baksi.hub")
router = Router(name="hub")

STATUS_EMOJI = {"running": "🟢", "waiting_bind": "🟡", "setup": "⚪️", "starting": "🔵", "restarting": "🔵",
                "crashed": "🔴", "token_invalid": "🔴", "stopped": "⏸", "frozen": "🧊", "expired": "⛔️",
                "queued": "🕓", "unavailable": "⚠️"}
TOPUP_PRESETS = (500, 1000, 2500, 5000, 10000)


class Form(StatesGroup):
    token = State()
    topup_amount = State()
    promo = State()
    balance_promo = State()
    broadcast = State()


def esc(text: Any) -> str:
    return html.escape(str(text or ""))


def fmt_date(ts: int | None) -> str:
    return time.strftime("%d.%m.%Y", time.localtime(ts)) if ts else "—"


async def render(target: Message | CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
        except TelegramBadRequest as exc:
            if "not modified" not in exc.message:
                await target.message.answer(text, reply_markup=markup, disable_web_page_preview=True)
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup, disable_web_page_preview=True)


async def user_of(app: App, event: Message | CallbackQuery, ref: int | None = None) -> Any:
    u = event.from_user
    row, _ = await app.billing.ensure_user(u.id, u.username, u.first_name, ref)
    return row


# --- главное меню ---------------------------------------------------------------
async def main_menu(app: App, target: Message | CallbackQuery) -> None:
    user = await user_of(app, target)
    bots = await app.instances.for_user(user["id"])
    running = sum(1 for i in bots if i["status"] in ("running", "waiting_bind"))
    text = (f"<b>{esc(app.cfg.brand)}</b>: боты для Telegram под ключ\n\n"
            f"💰 Баланс: <b>{money(user['balance'])}</b>\n"
            f"🤖 Ботов: <b>{len(bots)}</b>" + (f" · работают {running}" if bots else "") + "\n"
            f"🆔 ID: <code>{user['id']}</code>")
    support = await app.db.get_setting("support_url", "")
    rows = [[b("🛒 Каталог ботов", "cat", style="success")]]
    if app.miniapp_url:
        rows.append([b("✨ Открыть приложение", web_app=app.miniapp_url, style="primary")])
    rows.append([b("🤖 Мои боты", "my"), b("💳 Кошелёк", "wallet")])
    rows.append([b("🌐 Веб-кабинет", "web") if app.cfg.public_url else None, b("🎁 Рефералы", "ref")])
    if support:
        rows.append([b("💬 Поддержка", url=support)])
    if app.is_owner(user["id"]):
        rows.append([b("⚙️ Админка", "adm")])
    await render(target, text, kb(*rows))


@router.message(CommandStart())
async def start(message: Message, command: CommandObject, app: App, state: FSMContext) -> None:
    await state.clear()
    arg = (command.args or "").strip()
    ref = int(arg[1:]) if arg.startswith("r") and arg[1:].isdigit() else None
    user = await user_of(app, message, ref)
    if user["banned"]:
        await message.answer("Доступ к сервису ограничен. Если это ошибка, напишите в поддержку.")
        return
    if arg == "login":
        await web_login(message, app)
        return
    if arg.startswith("b") and app.catalog.get(arg[1:]):
        await product_card(message, app, arg[1:])
        return
    await main_menu(app, message)


@router.callback_query(F.data == "home")
async def home(call: CallbackQuery, app: App, state: FSMContext) -> None:
    await state.clear()
    await main_menu(app, call)


@router.callback_query(F.data == "noop")
async def noop(call: CallbackQuery) -> None:
    await call.answer()


# --- каталог --------------------------------------------------------------------
@router.callback_query(F.data == "cat")
async def catalog(call: CallbackQuery, app: App) -> None:
    products = await app.catalog.products()
    if not products:
        await render(call, "Каталог готовится: скоро здесь появятся боты.", kb([b("⬅️ Назад", "home")]))
        return
    lines = ["<b>Каталог ботов</b>\nСвой бот на своём токене. Запуск за пару минут.\n"]
    rows = []
    for p in products:
        lines.append(f"<b>{esc(p['title'])}</b> · {esc(p['tagline'])}\nот {money(p['min_price'])}")
        rows.append([b(f"{p['title']} · от {money(p['min_price'])}", f"p:{p['slug']}")])
    rows.append([b("⬅️ Назад", "home")])
    await render(call, "\n\n".join(lines), kb(*rows))


async def product_card(target: Message | CallbackQuery, app: App, slug: str, instance_id: int | None = None) -> None:
    products = {p["slug"]: p for p in await app.catalog.products()}
    p = products.get(slug)
    if not p:
        await render(target, "Этот бот сейчас недоступен.", kb([b("⬅️ Каталог", "cat")]))
        return
    user = await user_of(app, target)
    text = [f"<b>{esc(p['title'])}</b> · {esc(p['tagline'])}", "", esc(p["description"])]
    if p["how"]:
        text += ["", "<b>Как работает</b>"] + [f"{i}. {esc(s)}" for i, s in enumerate(p["how"], 1)]
    if p["features"]:
        text += ["", "<b>Возможности</b>"] + [f"• {esc(s)}" for s in p["features"]]
    text += ["", f"💰 Баланс: {money(user['balance'])}"]
    suffix = f":{instance_id}" if instance_id else ""
    plan_buttons = [b(f"{plan_label(pl['days'])} · {money(pl['price'])}", f"buy:{slug}:{pl['days']}{suffix}")
                    for pl in p["plans"]]
    rows = [plan_buttons[i:i + 2] for i in range(0, len(plan_buttons), 2)]
    if not instance_id and await app.billing.trial_available(user["id"], slug):
        rows.append([b(f"🎁 Попробовать {plan_label(p['trial_days'])} бесплатно", f"trial:{slug}", style="success")])
    rows.append([b("⬅️ Назад", f"bot:{instance_id}" if instance_id else "cat")])
    await render(target, "\n".join(text), kb(*rows))


@router.callback_query(F.data.startswith("p:"))
async def product(call: CallbackQuery, app: App) -> None:
    await product_card(call, app, call.data[2:])


@router.callback_query(F.data.startswith("renew:"))
async def renew(call: CallbackQuery, app: App) -> None:
    try:
        inst = await app.instances.get(int(call.data.split(":")[1]), call.from_user.id)
    except InstanceError as exc:
        await call.answer(str(exc), show_alert=True)
        return
    await product_card(call, app, inst["product"], inst["id"])


def _parse_buy(data: str) -> tuple[str, int, int | None]:
    parts = data.split(":")
    return parts[1], int(parts[2]), (int(parts[3]) if len(parts) > 3 and parts[3] else None)


async def checkout(target: Message | CallbackQuery, app: App, state: FSMContext, slug: str, days: int,
                   instance_id: int | None) -> None:
    user = await user_of(app, target)
    data = await state.get_data()
    promo = data.get("promo") if data.get("promo_for") == slug else None
    try:
        q = await app.billing.quote(user["id"], slug, days, promo)
    except BillingError as exc:
        await render(target, str(exc), kb([b("⬅️ Каталог", "cat")]))
        return
    module = app.catalog.get(slug)
    await state.update_data(checkout=[slug, days, instance_id])
    lines = [f"<b>{esc(module.title)} · {plan_label(days)}</b>", "",
             f"Тариф: {money(q.price)}"]
    if q.promo:
        lines.append(f"Промокод {esc(q.promo)}: −{money(q.discount)}")
    elif q.promo_error:
        lines.append(f"⚠️ {esc(q.promo_error)}")
    lines += [f"<b>К оплате: {money(q.total)}</b>", f"На балансе: {money(user['balance'])}"]
    suffix = f":{instance_id}" if instance_id else ""
    rows = []
    if user["balance"] >= q.total:
        rows.append([b(f"✅ Оплатить {money(q.total)} с баланса", f"pay:{slug}:{days}{suffix}", style="success")])
    else:
        need = q.total - user["balance"]
        lines.append(f"\nНе хватает {money(need)}.")
        if app.pay.enabled:
            amount = max(need, 100)
            rows.append([b(f"💳 Пополнить на {money(amount)} и оплатить", f"tpi:{slug}:{days}{suffix}",
                           style="success")])
    rows.append([b("🏷 Ввести промокод", "promo")])
    rows.append([b("⬅️ Назад", f"bot:{instance_id}" if instance_id else f"p:{slug}")])
    await render(target, "\n".join(lines), kb(*rows))


@router.callback_query(F.data.startswith("buy:"))
async def buy(call: CallbackQuery, app: App, state: FSMContext) -> None:
    slug, days, iid = _parse_buy(call.data)
    await checkout(call, app, state, slug, days, iid)


@router.callback_query(F.data == "promo")
async def promo_ask(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Form.promo)
    await call.message.answer("Отправьте промокод одним сообщением.")
    await call.answer()


@router.message(Form.promo, F.text)
async def promo_enter(message: Message, app: App, state: FSMContext) -> None:
    data = await state.get_data()
    co = data.get("checkout")
    await state.set_state(None)
    if not co:
        await main_menu(app, message)
        return
    await state.update_data(promo=message.text.strip().upper(), promo_for=co[0])
    await checkout(message, app, state, co[0], co[1], co[2])


@router.callback_query(F.data.startswith("pay:"))
async def pay(call: CallbackQuery, app: App, state: FSMContext) -> None:
    slug, days, iid = _parse_buy(call.data)
    data = await state.get_data()
    promo = data.get("promo") if data.get("promo_for") == slug else None
    try:
        instance_id = await app.billing.purchase(call.from_user.id, slug, days, promo, iid)
    except InsufficientFunds:
        await call.answer("Не хватает средств на балансе", show_alert=True)
        return
    except BillingError as exc:
        await call.answer(str(exc), show_alert=True)
        return
    await state.update_data(promo=None, promo_for=None, checkout=None)
    app.sup.poke()
    await call.answer("Оплачено ✅")
    await bot_card(call, app, instance_id, state)


@router.callback_query(F.data.startswith("tpi:"))
async def topup_and_buy(call: CallbackQuery, app: App, state: FSMContext) -> None:
    slug, days, iid = _parse_buy(call.data)
    data = await state.get_data()
    promo = data.get("promo") if data.get("promo_for") == slug else None
    user = await user_of(app, call)
    try:
        q = await app.billing.quote(user["id"], slug, days, promo)
        need = max(q.total - user["balance"], 100)
        inv = await app.pay.create(user["id"], need, {"action": "purchase", "product": slug, "days": days,
                                                      "promo": q.promo, "instance_id": iid})
    except (BillingError, PaymentError) as exc:
        await call.answer(str(exc), show_alert=True)
        return
    await render(call, f"Счёт на <b>{money(inv['amount'])}</b> создан.\n\nОплатите его в @CryptoBot: "
                       "как только оплата пройдёт, бот оплатится автоматически и придёт уведомление.",
                 kb([b(f"💳 Оплатить {money(inv['amount'])}", url=inv["url"], style="success")],
                    [b("⬅️ Назад", f"buy:{slug}:{days}" + (f":{iid}" if iid else ""))]))


@router.callback_query(F.data.startswith("trial:"))
async def trial(call: CallbackQuery, app: App, state: FSMContext) -> None:
    try:
        instance_id = await app.billing.start_trial(call.from_user.id, call.data.split(":", 1)[1])
    except BillingError as exc:
        await call.answer(str(exc), show_alert=True)
        return
    await call.answer("Пробный период активирован 🎁")
    await bot_card(call, app, instance_id, state)


# --- мои боты -------------------------------------------------------------------
@router.callback_query(F.data == "my")
async def my_bots(call: CallbackQuery, app: App, state: FSMContext) -> None:
    await state.set_state(None)
    bots = await app.instances.for_user(call.from_user.id)
    if not bots:
        await render(call, "У вас пока нет ботов. Выберите первого в каталоге.",
                     kb([b("🛒 Каталог ботов", "cat", style="success")], [b("⬅️ Назад", "home")]))
        return
    rows, lines = [], ["<b>Мои боты</b>\n"]
    for inst in bots:
        v = app.instances.view(inst)
        until = "навсегда" if v["forever"] else f"до {fmt_date(v['expires_at'])}"
        name = f"@{v['bot_username']}" if v["bot_username"] else "без токена"
        lines.append(f"{STATUS_EMOJI.get(v['status'], '•')} <b>{esc(v['title'])}</b> {esc(name)} · "
                     f"{esc(v['status_text'])} · {until}")
        rows.append([b(f"{STATUS_EMOJI.get(v['status'], '•')} {v['title']} · {name}", f"bot:{inst['id']}")])
    rows.append([b("➕ Новый бот", "cat", style="success")])
    rows.append([b("⬅️ Назад", "home")])
    await render(call, "\n".join(lines), kb(*rows))


async def bot_card(target: Message | CallbackQuery, app: App, instance_id: int, state: FSMContext) -> None:
    await state.set_state(None)
    uid = target.from_user.id
    try:
        inst = await app.instances.get(instance_id, uid)
    except InstanceError as exc:
        await render(target, str(exc), kb([b("⬅️ Мои боты", "my")]))
        return
    v = app.instances.view(inst)
    name = f"@{esc(v['bot_username'])}" if v["bot_username"] else "токен не подключён"
    if v["forever"]:
        sub = "навсегда"
    elif v["trial"]:
        sub = f"пробный до {fmt_date(v['expires_at'])}"
    else:
        sub = f"до {fmt_date(v['expires_at'])}"
    lines = [f"<b>{esc(v['title'])}</b> · {name}",
             f"{STATUS_EMOJI.get(v['status'], '•')} {esc(v['status_text'])}" +
             (f": {esc(v['status_note'])}" if v["status_note"] and v["status"] != "running" else ""),
             f"📅 Подписка: {sub}"]
    if not v["forever"] and not v["trial"]:
        lines.append(f"🔄 Автопродление: {'вкл' if v['auto_renew'] else 'выкл'}"
                     + (f" ({esc(v['plan_label'])})" if v["plan_label"] else ""))
    rows: list[list] = []
    if not v["has_token"]:
        lines += ["", "<b>Подключите бота</b>",
                  "1. Откройте @BotFather → /newbot, придумайте имя и юзернейм.",
                  "2. Скопируйте токен и отправьте сюда кнопкой ниже."]
        rows.append([b("🔑 Вставить токен", f"tok:{instance_id}", style="success")])
    elif v["status"] == "token_invalid":
        lines += ["", "Telegram отклонил токен. Данные сохранены: просто вставьте новый токен."]
        rows.append([b("🔑 Вставить новый токен", f"tok:{instance_id}", style="danger")])
    if v["needs_bind"] and v["has_token"]:
        if v["group"]:
            lines.append(f"👥 Группа: {esc(v['group']['title'])}")
        else:
            lines += ["", f"<b>Привяжите группу</b>\n{esc(v['bind_hint'] or 'Добавьте бота в группу и отправьте туда:')}",
                      f"<code>/bind {v['bind_code']}</code>"]
        rows.append([b(f"📋 Скопировать /bind {v['bind_code']}", copy=f"/bind {v['bind_code']}")])
    if v["has_token"] and v["bot_username"]:
        rows.append([b("↗️ Открыть бота", url=f"https://t.me/{v['bot_username']}")])
    rows.append([b("💳 Продлить" if not v["forever"] else "♾ Бессрочно", f"renew:{instance_id}" if not v["forever"]
                   else "noop", style="success" if v["status"] == "expired" else None),
                 b("🔄 Автопродление: " + ("вкл" if v["auto_renew"] else "выкл"), f"ar:{instance_id}")
                 if not v["forever"] and not v["trial"] else None])
    if v["has_token"]:
        rows.append([b("▶️ Запустить", f"resume:{instance_id}", style="success") if v["paused"]
                     else b("⏸ Остановить", f"pause:{instance_id}"),
                     b("🔁 Перезапустить", f"restart:{instance_id}")])
        rows.append([b("🔑 Сменить токен", f"tok:{instance_id}"), b("📜 Логи", f"logs:{instance_id}")])
    bool_settings = [s for s in v["settings_schema"] if s["type"] == "bool"]
    for s in bool_settings:
        on = v["settings"].get(s["key"], s["default"])
        rows.append([b(f"{'✅' if on else '⬜️'} {s['label']}", f"set:{instance_id}:{s['key']}")])
    if app.cfg.public_url and len(bool_settings) < len(v["settings_schema"]):
        lines.append("\n⚙️ Остальные настройки - в веб-кабинете.")
    rows.append([b("🗑 Удалить", f"del:{instance_id}", style="danger"), b("⬅️ Мои боты", "my")])
    await render(target, "\n".join(lines), kb(*rows))


@router.callback_query(F.data.startswith("bot:"))
async def bot_open(call: CallbackQuery, app: App, state: FSMContext) -> None:
    await bot_card(call, app, int(call.data.split(":")[1]), state)


async def _action(call: CallbackQuery, app: App, state: FSMContext, coro, ok: str) -> None:
    iid = int(call.data.split(":")[1])
    try:
        await coro(iid)
    except (InstanceError, BillingError) as exc:
        await call.answer(str(exc), show_alert=True)
        return
    await call.answer(ok)
    await asyncio.sleep(0.6)  # дать супервизору обновить статус
    await bot_card(call, app, iid, state)


@router.callback_query(F.data.startswith("ar:"))
async def toggle_autorenew(call: CallbackQuery, app: App, state: FSMContext) -> None:
    uid = call.from_user.id

    async def go(iid: int) -> None:
        inst = await app.instances.get(iid, uid)
        await app.instances.set_flag(uid, iid, "auto_renew", not inst["auto_renew"])
    await _action(call, app, state, go, "Готово")


@router.callback_query(F.data.startswith("pause:"))
async def pause(call: CallbackQuery, app: App, state: FSMContext) -> None:
    await _action(call, app, state, lambda iid: app.instances.set_flag(call.from_user.id, iid, "paused", True),
                  "Бот остановлен")


@router.callback_query(F.data.startswith("resume:"))
async def resume(call: CallbackQuery, app: App, state: FSMContext) -> None:
    await _action(call, app, state, lambda iid: app.instances.set_flag(call.from_user.id, iid, "paused", False),
                  "Бот запускается")


@router.callback_query(F.data.startswith("restart:"))
async def restart(call: CallbackQuery, app: App, state: FSMContext) -> None:
    await _action(call, app, state, lambda iid: app.instances.restart(call.from_user.id, iid), "Перезапускаю")


@router.callback_query(F.data.startswith("set:"))
async def toggle_setting(call: CallbackQuery, app: App, state: FSMContext) -> None:
    _, iid_raw, key = call.data.split(":", 2)
    iid = int(iid_raw)
    try:
        inst = await app.instances.get(iid, call.from_user.id)
        view = app.instances.view(inst)
        schema = {s["key"]: s for s in view["settings_schema"]}
        current = view["settings"].get(key, schema.get(key, {}).get("default"))
        await app.instances.update_settings(call.from_user.id, iid, {key: not current})
    except InstanceError as exc:
        await call.answer(str(exc), show_alert=True)
        return
    await call.answer("Сохранено")
    await bot_card(call, app, iid, state)


@router.callback_query(F.data.startswith("logs:"))
async def logs(call: CallbackQuery, app: App) -> None:
    iid = int(call.data.split(":")[1])
    try:
        await app.instances.get(iid, call.from_user.id)
    except InstanceError as exc:
        await call.answer(str(exc), show_alert=True)
        return
    lines = app.sup.logs(iid, 25)
    body = esc("\n".join(line[-160:] for line in lines))[-3500:] or "Логов пока нет."
    await render(call, f"<b>Последние события</b>\n<pre>{body}</pre>",
                 kb([b("🔄 Обновить", f"logs:{iid}"), b("⬅️ Назад", f"bot:{iid}")]))


@router.callback_query(F.data.startswith("del:"))
async def delete_ask(call: CallbackQuery, app: App) -> None:
    iid = int(call.data.split(":")[1])
    await render(call, "Удалить бота? Он остановится сразу, оставшиеся дни подписки не возвращаются. "
                       "Данные хранятся 3 дня, потом удаляются.",
                 kb([b("🗑 Да, удалить", f"delok:{iid}", style="danger"), b("Отмена", f"bot:{iid}")]))


@router.callback_query(F.data.startswith("delok:"))
async def delete_ok(call: CallbackQuery, app: App, state: FSMContext) -> None:
    iid = int(call.data.split(":")[1])
    try:
        await app.instances.delete(call.from_user.id, iid)
    except InstanceError as exc:
        await call.answer(str(exc), show_alert=True)
        return
    await call.answer("Бот удалён")
    await my_bots(call, app, state)


@router.callback_query(F.data.startswith("tok:"))
async def token_ask(call: CallbackQuery, app: App, state: FSMContext) -> None:
    iid = int(call.data.split(":")[1])
    await state.set_state(Form.token)
    await state.update_data(token_for=iid)
    await call.message.answer("Отправьте токен бота из @BotFather одним сообщением.\n"
                              "Сообщение с токеном я сразу удалю из чата.",
                              reply_markup=kb([b("Отмена", f"bot:{iid}")]))
    await call.answer()


@router.message(Form.token, F.text)
async def token_enter(message: Message, app: App, state: FSMContext) -> None:
    data = await state.get_data()
    iid = data.get("token_for")
    token = message.text.strip()
    try:
        await message.delete()
    except TelegramBadRequest:
        pass
    if not iid:
        await state.clear()
        return
    wait = await message.answer("Проверяю токен…")
    try:
        info = await app.instances.set_token(message.from_user.id, iid, token)
    except InstanceError as exc:
        await wait.edit_text(f"❌ {esc(exc)}\n\nОтправьте токен ещё раз.", reply_markup=kb([b("Отмена", f"bot:{iid}")]))
        return
    await state.set_state(None)
    await wait.edit_text(f"✅ Бот @{esc(info.username)} подключён и запускается.")
    await bot_card(message, app, iid, state)


# --- кошелёк --------------------------------------------------------------------
@router.callback_query(F.data.in_({"wallet", "wallet:topup"}))
async def wallet(call: CallbackQuery, app: App, state: FSMContext) -> None:
    await state.set_state(None)
    user = await user_of(app, call)
    ops = await app.db.all("SELECT * FROM ledger WHERE user_id = ? ORDER BY id DESC LIMIT 6", user["id"])
    lines = [f"<b>Кошелёк</b>\n\n💰 Баланс: <b>{money(user['balance'])}</b>"]
    if ops:
        lines.append("\n<b>Последние операции</b>")
        lines += [f"{'+' if o['amount'] > 0 else ''}{money(o['amount'])} · {esc(o['note'])} · "
                  f"{fmt_date(o['created_at'])}" for o in ops]
    rows = []
    if app.pay.enabled:
        presets = [b(money(c).replace(".00", ""), f"top:{c}") for c in TOPUP_PRESETS]
        rows += [presets[:3], presets[3:] + [b("Другая сумма", "top:custom")]]
    else:
        lines.append("\nПополнение пока недоступно, напишите в поддержку.")
    rows.append([b("🏷 Промокод на баланс", "bpromo")])
    rows.append([b("⬅️ Назад", "home")])
    await render(call, "\n".join(lines), kb(*rows))


async def _send_invoice(target: Message | CallbackQuery, app: App, cents: int) -> None:
    try:
        inv = await app.pay.create(target.from_user.id, cents)
    except PaymentError as exc:
        if isinstance(target, CallbackQuery):
            await target.answer(str(exc), show_alert=True)
        else:
            await target.answer(f"❌ {esc(exc)}")
        return
    await render(target, f"Счёт на <b>{money(inv['amount'])}</b>. Оплатите в @CryptoBot (USDT, TON, BTC и др.): "
                         "зачисление придёт автоматически в течение минуты.",
                 kb([b(f"💳 Оплатить {money(inv['amount'])}", url=inv["url"], style="success")],
                    [b("⬅️ Кошелёк", "wallet")]))


@router.callback_query(F.data.startswith("top:"))
async def topup(call: CallbackQuery, app: App, state: FSMContext) -> None:
    arg = call.data.split(":")[1]
    if arg == "custom":
        await state.set_state(Form.topup_amount)
        await call.message.answer("Введите сумму в долларах, например 15 или 7.5")
        await call.answer()
        return
    await _send_invoice(call, app, int(arg))


@router.message(Form.topup_amount, F.text)
async def topup_amount(message: Message, app: App, state: FSMContext) -> None:
    cents = parse_money(message.text)
    if not cents:
        await message.answer("Не понял сумму. Напишите число, например 20")
        return
    await state.set_state(None)
    await _send_invoice(message, app, cents)


@router.callback_query(F.data == "bpromo")
async def balance_promo_ask(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Form.balance_promo)
    await call.message.answer("Отправьте промокод одним сообщением.")
    await call.answer()


@router.message(Form.balance_promo, F.text)
async def balance_promo(message: Message, app: App, state: FSMContext) -> None:
    await state.set_state(None)
    try:
        amount = await app.billing.redeem_balance_promo(message.from_user.id, message.text)
    except BillingError as exc:
        await message.answer(f"❌ {esc(exc)}", reply_markup=kb([b("⬅️ Кошелёк", "wallet")]))
        return
    await message.answer(f"✅ На баланс зачислено {money(amount)}", reply_markup=kb([b("⬅️ Кошелёк", "wallet")]))


# --- рефералы и веб -------------------------------------------------------------
@router.callback_query(F.data == "ref")
async def referrals(call: CallbackQuery, app: App) -> None:
    user = await user_of(app, call)
    count = await app.db.value("SELECT COUNT(*) FROM users WHERE ref_by = ?", user["id"], default=0)
    percent = await app.db.get_setting("referral_percent", 0)
    link = f"https://t.me/{app.hub_username}?start=r{user['id']}"
    text = [f"<b>Реферальная программа</b>\n", f"Ваша ссылка:\n<code>{link}</code>\n",
            f"👥 Приглашено: {count}", f"💰 Заработано: {money(user['ref_earned'])}"]
    if percent:
        text.insert(1, f"Получайте {percent}% с каждого пополнения приглашённых на свой баланс.\n")
    await render(call, "\n".join(text), kb([b("📋 Скопировать ссылку", copy=link)], [b("⬅️ Назад", "home")]))


async def login_link(app: App, user_id: int, next_path: str = "/app") -> str:
    token = new_token(24)
    await app.db.run("INSERT INTO login_tokens(token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                     app.vault.hash(token), user_id, now() + 600)
    return app.web_url(f"/auth/link?t={token}&next={quote(next_path, safe='/')}")


async def web_login(target: Message | CallbackQuery, app: App) -> None:
    if not app.cfg.public_url:
        await render(target, "Веб-кабинет ещё не подключён.", kb([b("⬅️ Назад", "home")]))
        return
    url = await login_link(app, target.from_user.id)
    await render(target, "Ссылка для входа в веб-кабинет. Она одноразовая и действует 10 минут, "
                         "никому её не пересылайте.",
                 kb([b("🌐 Войти в кабинет", url=url, style="primary")], [b("⬅️ Назад", "home")]))


@router.callback_query(F.data == "web")
async def web(call: CallbackQuery, app: App) -> None:
    await web_login(call, app)


# --- админка владельца ----------------------------------------------------------
async def admin_stats(app: App) -> str:
    ts = now()
    day = ts - ts % 86400
    month = ts - 30 * 86400
    users = await app.db.value("SELECT COUNT(*) FROM users", default=0)
    new_today = await app.db.value("SELECT COUNT(*) FROM users WHERE created_at >= ?", day, default=0)
    active = await app.db.value("SELECT COUNT(*) FROM instances WHERE deleted_at IS NULL AND (forever = 1 OR "
                                "(expires_at > ? AND expired_at IS NULL))", ts, default=0)
    trials = await app.db.value("SELECT COUNT(*) FROM instances WHERE deleted_at IS NULL AND trial = 1 AND "
                                "expires_at > ?", ts, default=0)
    topup_day = await app.db.value("SELECT SUM(amount) FROM ledger WHERE kind = 'topup' AND created_at >= ?", day,
                                   default=0)
    topup_month = await app.db.value("SELECT SUM(amount) FROM ledger WHERE kind = 'topup' AND created_at >= ?",
                                     month, default=0)
    problems = await app.db.value("SELECT COUNT(*) FROM instances WHERE deleted_at IS NULL AND status IN "
                                  "('crashed', 'token_invalid', 'queued')", default=0)
    t = app.sup.totals()
    return (f"<b>Админка {esc(app.cfg.brand)}</b>\n\n"
            f"👥 Клиентов: {users} (сегодня +{new_today})\n"
            f"🤖 Активных подписок: {active} (пробных {trials})\n"
            f"💵 Пополнения: сегодня {money(topup_day)}, за 30 дней {money(topup_month)}\n"
            f"⚙️ Процессов: {t['running']}, память {t['rss_mb']} МБ\n"
            f"🔴 Требуют внимания: {problems}\n\n"
            f"Команды: <code>/give ID сумма [комментарий]</code> - начислить (минус - списать)")


@router.callback_query(F.data == "adm")
async def admin(call: CallbackQuery, app: App) -> None:
    if not app.is_owner(call.from_user.id):
        await call.answer()
        return
    rows = []
    if app.cfg.public_url:
        rows.append([b("🌐 Веб-админка", "admweb", style="primary")])
    rows.append([b("💾 Бэкап сейчас", "admbackup"), b("📣 Рассылка", "admcast")])
    rows.append([b("🔄 Обновить", "adm"), b("⬅️ Назад", "home")])
    await render(call, await admin_stats(app), kb(*rows))


@router.callback_query(F.data == "admweb")
async def admin_web(call: CallbackQuery, app: App) -> None:
    if not app.is_owner(call.from_user.id):
        return
    url = await login_link(app, call.from_user.id, "/app#/admin")
    await render(call, "Одноразовая ссылка в веб-админку (10 минут).",
                 kb([b("🌐 Открыть", url=url, style="primary")], [b("⬅️ Назад", "adm")]))


@router.callback_query(F.data == "admbackup")
async def admin_backup(call: CallbackQuery, app: App) -> None:
    if not app.is_owner(call.from_user.id):
        return
    await call.answer("Делаю бэкап, пришлю файл сюда")
    from ..jobs import run_backup
    name = await run_backup(app, send=True)
    if not name:
        await call.message.answer("❌ Бэкап не удался, смотрите логи.")


@router.callback_query(F.data == "admcast")
async def admin_cast_ask(call: CallbackQuery, app: App, state: FSMContext) -> None:
    if not app.is_owner(call.from_user.id):
        return
    await state.set_state(Form.broadcast)
    await call.message.answer("Пришлите сообщение для рассылки всем клиентам (текст, фото, видео - что угодно). "
                              "Оно уйдёт копией.", reply_markup=kb([b("Отмена", "adm")]))
    await call.answer()


@router.message(Form.broadcast)
async def admin_cast(message: Message, app: App, state: FSMContext, bot: Bot) -> None:
    if not app.is_owner(message.from_user.id):
        return
    await state.set_state(None)
    ids = [r["id"] for r in await app.db.all("SELECT id FROM users WHERE banned = 0 AND hub_blocked = 0")]
    status = await message.answer(f"Рассылка на {len(ids)} человек началась.")

    async def run() -> None:
        sent = failed = 0
        for uid in ids:
            try:
                await bot.copy_message(uid, message.chat.id, message.message_id)
                sent += 1
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 1)
            except TelegramForbiddenError:
                failed += 1
                await app.db.run("UPDATE users SET hub_blocked = 1 WHERE id = ?", uid)
            except Exception:
                failed += 1
            await asyncio.sleep(0.05)
        await status.edit_text(f"Рассылка завершена: доставлено {sent}, не доставлено {failed}.")
    asyncio.create_task(run())


@router.message(Command("give"))
async def give(message: Message, command: CommandObject, app: App) -> None:
    if not app.is_owner(message.from_user.id):
        return
    parts = (command.args or "").split(maxsplit=2)
    if len(parts) < 2 or not parts[0].lstrip("-").isdigit():
        await message.answer("Формат: /give ID сумма [комментарий]\nНапример: /give 123456789 10 бонус")
        return
    raw = parts[1]
    negative = raw.startswith("-")
    cents = parse_money(raw.lstrip("-"))
    if not cents:
        await message.answer("Не понял сумму")
        return
    cents = -cents if negative else cents
    try:
        balance = await app.billing.admin_adjust(message.from_user.id, int(parts[0]), cents,
                                                 parts[2] if len(parts) > 2 else "")
    except BillingError as exc:
        await message.answer(f"❌ {esc(exc)}")
        return
    await message.answer(f"Готово. Баланс клиента: {money(balance)}")
    if cents > 0:
        app.notifier.send(int(parts[0]), f"🎁 На ваш баланс зачислено {money(cents)}")


@router.message(F.chat.type == "private")
async def fallback(message: Message, app: App, state: FSMContext) -> None:
    if await state.get_state() is None:
        await main_menu(app, message)
