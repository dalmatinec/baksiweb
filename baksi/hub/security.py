"""Хаб: безопасность аккаунта - ключ восстановления, запасной Telegram, пароль для сайта, перенос, /recover."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from ..accounts import METHODS, AccountError, fmt_time
from ..app import App
from .ui import b, kb

router = Router(name="hub_security")


class SecForm(StatesGroup):
    key = State()


def _h():
    from . import handlers  # поздний импорт: handlers импортирует этот модуль
    return handlers


async def security_view(app: App, user_id: int) -> tuple[str, object]:
    h = _h()
    sec = await app.accounts.security(user_id)
    lines = ["🔐 <b>Безопасность аккаунта</b>",
             "Если потеряете этот Telegram, доступ к ботам и балансу можно вернуть одним из способов ниже.\n"]
    lines.append(("✅" if sec["has_key"] else "⚪️") + " <b>Ключ восстановления</b>: " +
                 (f"создан {fmt_time(sec['key_at'])}" if sec["has_key"] else "не создан"))
    lines.append(("✅" if sec["reserve_id"] else "⚪️") + " <b>Запасной Telegram</b>: " +
                 (h.esc(sec["reserve_name"] or sec["reserve_id"]) if sec["reserve_id"] else "не указан"))
    lines.append(("✅" if sec["login"] else "⚪️") + " <b>Вход на сайт по паролю</b>: " +
                 (f"логин <code>{h.esc(sec['login'])}</code>" if sec["login"] else "не настроен"))
    if sec["transfer"]:
        t = sec["transfer"]
        lines.append(f"\n⚠️ <b>Запрошен перенос аккаунта</b> на Telegram <code>{t['new_id']}</code> "
                     f"({METHODS.get(t['method'], t['method'])}), состоится {fmt_time(t['execute_at'])}.")
    lines.append("\nПотеряли Telegram? С нового аккаунта откройте этого бота и отправьте /recover.")
    rows = [[b("🔑 Новый ключ восстановления" if sec["has_key"] else "🔑 Получить ключ восстановления", "sec:key",
               style=None if sec["has_key"] else "primary")],
            [b("👥 Сменить запасной Telegram" if sec["reserve_id"] else "👥 Указать запасной Telegram", "sec:rs")],
            [b("🔒 Пароль для сайта", "sec:pw")] if app.cfg.public_url else None]
    if sec["reserve_id"]:
        rows.append([b("✖️ Убрать запасной Telegram", "sec:rsx")])
    if sec["transfer"]:
        rows.append([b("🚫 Отменить перенос", f"trc:{sec['transfer']['id']}", style="danger")])
    rows.append([b("⬅️ Назад", "home")])
    return "\n".join(lines), kb(*[r for r in rows if r])


@router.callback_query(F.data == "sec")
async def security(call: CallbackQuery, app: App, state: FSMContext) -> None:
    await state.clear()
    text, markup = await security_view(app, call.from_user.id)
    await _h().render(call, text, markup)


@router.callback_query(F.data == "sec:key")
async def new_key(call: CallbackQuery, app: App) -> None:
    key = await app.accounts.issue_recovery_key(call.from_user.id)
    await call.answer()
    await call.message.answer(
        "🔑 <b>Ваш ключ восстановления</b>\n\n"
        f"<code>{key}</code>\n\n"
        "Сохраните его вне Telegram: в менеджере паролей или на бумаге. С ним можно вернуть аккаунт на новый "
        "Telegram, даже если этот потерян. Прежний ключ больше не действует.\n\n"
        "После сохранения удалите это сообщение.",
        reply_markup=kb([b("📋 Скопировать ключ", copy=key)], [b("🗑 Я сохранил, удалить", "sec:del")]))


@router.callback_query(F.data == "sec:del")
async def delete_message(call: CallbackQuery) -> None:
    try:
        await call.message.delete()
    except TelegramBadRequest:
        pass
    await call.answer("Удалено")


@router.callback_query(F.data == "sec:rs")
async def reserve(call: CallbackQuery, app: App) -> None:
    link = await app.accounts.reserve_link(call.from_user.id)
    await _h().render(call, (
        "👥 <b>Запасной Telegram</b>\n\n"
        "Откройте эту ссылку со второго своего аккаунта (или аккаунта человека, которому доверяете) и нажмите "
        "«Запустить». Ссылка действует сутки.\n\n"
        f"<code>{link}</code>\n\n"
        "Если потеряете этот аккаунт, с запасного можно будет перенести боты и баланс. Перенос всегда идёт с "
        "задержкой 24 часа и предупреждением, так что тайком забрать аккаунт не получится."),
        kb([b("📋 Скопировать ссылку", copy=link)], [b("⬅️ Назад", "sec")]))


@router.callback_query(F.data == "sec:rsx")
async def reserve_remove(call: CallbackQuery, app: App) -> None:
    await app.accounts.remove_reserve(call.from_user.id)
    await call.answer("Запасной Telegram убран")
    text, markup = await security_view(app, call.from_user.id)
    await _h().render(call, text, markup)


@router.callback_query(F.data == "sec:pw")
async def password(call: CallbackQuery, app: App) -> None:
    url = await _h().login_link(app, call.from_user.id, "/app#/security")
    await _h().render(call, "🔒 <b>Пароль для сайта</b>\n\nЗадать или сменить логин и пароль можно в веб-кабинете, "
                            "в разделе «Безопасность». Кнопка ниже сразу откроет его (ссылка одноразовая, 10 минут).",
                      kb([b("🔒 Открыть «Безопасность»", url=url, style="primary")], [b("⬅️ Назад", "sec")]))


@router.callback_query(F.data.startswith("trc:"))
async def cancel_transfer(call: CallbackQuery, app: App) -> None:
    try:
        row = await app.accounts.cancel_transfer(int(call.data.split(":")[1]), call.from_user.id)
    except AccountError as exc:
        await call.answer(str(exc), show_alert=True)
        return
    await call.answer("Перенос отменён")
    extra = ""
    if row["method"] == "key" and call.from_user.id == row["old_id"]:
        key = await app.accounts.issue_recovery_key(row["old_id"])
        extra = (f"\n\nКлюч восстановления мог попасть в чужие руки, поэтому он заменён. Новый ключ:\n<code>{key}</code>\n"
                 "Сохраните его вне Telegram.")
    try:
        await call.message.edit_text("✅ <b>Перенос аккаунта отменён.</b> Аккаунт остаётся на прежнем Telegram." + extra)
    except TelegramBadRequest:
        pass


# --- восстановление с нового аккаунта -------------------------------------------------------
@router.message(Command("recover"))
async def recover(message: Message, app: App, state: FSMContext) -> None:
    h = _h()
    await h.user_of(app, message)
    reserved = await app.accounts.accounts_reserved_by(message.from_user.id)
    rows = [[b(f"↩️ Перенести аккаунт {('@' + r['username']) if r['username'] else (r['first_name'] or r['user_id'])} сюда",
               f"rcv:{r['user_id']}")] for r in reserved]
    rows.append([b("Отмена", "home")])
    await state.set_state(SecForm.key)
    await message.answer(
        "♻️ <b>Восстановление доступа</b>\n\n"
        "Отправьте ключ восстановления: он выглядит так <code>BAKSI-XXXX-XXXX-XXXX-XXXX</code>. "
        "Сообщение с ключом я сразу удалю.\n\n"
        + ("Этот Telegram указан запасным для аккаунта ниже: перенести его можно и без ключа.\n\n" if reserved else "")
        + "Перенос выполняется через 24 часа: прежний аккаунт получит предупреждение и сможет отменить, если это не он.",
        reply_markup=kb(*rows))


@router.callback_query(F.data.startswith("rcv:"))
async def recover_by_reserve(call: CallbackQuery, app: App, state: FSMContext) -> None:
    old_id = int(call.data.split(":")[1])
    reserved = {r["user_id"] for r in await app.accounts.accounts_reserved_by(call.from_user.id)}
    if old_id not in reserved:
        await call.answer("Этот Telegram больше не запасной для аккаунта", show_alert=True)
        return
    await state.clear()
    await _transfer_requested(call, app, lambda: app.accounts.request_transfer(old_id, call.from_user.id, "reserve"))


async def _transfer_requested(target, app: App, make) -> None:
    h = _h()
    try:
        row = await make()
    except AccountError as exc:
        text = f"⚠️ {exc}"
        if isinstance(target, CallbackQuery):
            await target.answer(str(exc), show_alert=True)
        else:
            await target.answer(text)
        return
    text = ("✅ <b>Запрос на перенос принят</b>\n\n"
            f"Аккаунт перейдёт на этот Telegram <b>{fmt_time(row['execute_at'])}</b>, через 24 часа. "
            "Прежний аккаунт и запасной Telegram получили предупреждение: если это не их владелец, они отменят перенос.\n\n"
            "Когда перенос выполнится, я пришлю сюда сообщение и новый ключ восстановления.")
    if isinstance(target, CallbackQuery):
        await h.render(target, text, kb([b("⬅️ В меню", "home")]))
    else:
        await target.answer(text, reply_markup=kb([b("⬅️ В меню", "home")]))


@router.message(SecForm.key, F.text)
async def recovery_key_input(message: Message, app: App, state: FSMContext) -> None:
    key = message.text or ""
    try:
        await message.delete()  # ключ не должен оставаться в чате
    except TelegramBadRequest:
        pass
    await state.clear()
    await _transfer_requested(message, app, lambda: app.accounts.recover_with_key(message.from_user.id, key))


# --- ссылки из сайта и от владельца аккаунта -----------------------------------------------
async def account_link(message: Message, app: App, kind: str, token: str) -> None:
    h = _h()
    uid = message.from_user.id
    try:
        if kind == "su":
            login = await app.accounts.signup_confirm(token, uid)
            await message.answer(f"✅ <b>Регистрация завершена.</b> Логин <code>{h.esc(login)}</code> привязан к этому "
                                 "Telegram. Вернитесь на сайт: вход выполнится сам.\n\n"
                                 "Совет: в разделе «🔐 Безопасность» получите ключ восстановления и укажите запасной "
                                 "Telegram, чтобы не потерять доступ.",
                                 reply_markup=kb([b("🔐 Безопасность", "sec")], [b("🏠 Главное меню", "home")]))
        elif kind == "rs":
            await app.accounts.reserve_confirm(token, uid, message.from_user.full_name)
            await message.answer("✅ <b>Этот Telegram теперь запасной</b> для аккаунта, который прислал ссылку.\n\n"
                                 "Если владелец потеряет свой аккаунт, отсюда можно будет перенести его боты и баланс: "
                                 "команда /recover. Перенос всегда с задержкой 24 часа и предупреждением.")
        elif kind == "mv":
            row = await app.accounts.get_token("mv", token)
            if not row:
                raise AccountError("Ссылка для переноса устарела. Создайте новую в веб-кабинете.")
            await app.db.run("DELETE FROM auth_tokens WHERE token_hash = ?", row["token_hash"])
            await _transfer_requested(message, app,
                                      lambda: app.accounts.request_transfer(row["user_id"], uid, "password"))
    except AccountError as exc:
        await message.answer(f"⚠️ {exc}", reply_markup=kb([b("🏠 Главное меню", "home")]))
