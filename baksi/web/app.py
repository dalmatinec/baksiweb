"""Веб: сайт, кабинет клиента, Mini App и админка владельца (один SPA) + JSON API."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from .. import design
from ..app import App
from ..billing import BillingError, InsufficientFunds, money
from ..catalog import plan_label
from ..cryptopay import PaymentError
from ..db import now
from ..instances import InstanceError
from .security import (SESSION_COOKIE, SESSION_TTL, RateLimiter, check_login_widget, check_webapp_init_data,
                       create_session, current_user, owner_user, session_token)

log = logging.getLogger("baksi.web")
STATIC = Path(__file__).parent / "static"
MAX_BODY = 1_000_000
MAX_UPLOAD = 60_000_000

CSP = ("default-src 'self'; script-src 'self' https://telegram.org; style-src 'self' 'unsafe-inline' "
       "https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data: https:; "
       "connect-src 'self'; frame-src https://oauth.telegram.org; "
       "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org; base-uri 'none'; form-action 'self'")


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def create_web(app: App) -> FastAPI:
    web = FastAPI(title=app.cfg.brand, docs_url=None, redoc_url=None, openapi_url=None)
    web.state.baksi = app
    limiter = RateLimiter()

    @web.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        ip = client_ip(request)
        try:
            if path.startswith("/api/"):
                limiter.check(ip, "api", 120, 10)
                size = int(request.headers.get("content-length") or 0)
                limit = MAX_UPLOAD if path.endswith("/restore") else MAX_BODY
                if size > limit:
                    raise HTTPException(413, "Слишком большой запрос")
                # Защита от CSRF: браузер не даст чужому сайту поставить этот заголовок
                if request.method not in ("GET", "HEAD", "OPTIONS") and path != "/api/cryptopay/webhook" \
                        and request.headers.get("x-baksi") != "1":
                    raise HTTPException(403, "Запрос отклонён")
        except HTTPException as exc:
            return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = CSP
        if path.startswith("/api/") and "cache-control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        return response

    @web.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    @web.exception_handler(Exception)
    async def any_error(_request: Request, exc: Exception):
        log.exception("Ошибка веба", exc_info=exc)
        return JSONResponse({"error": "Внутренняя ошибка, мы уже разбираемся"}, status_code=500)

    def set_cookie(resp: Response, token: str) -> None:
        resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL, httponly=True,
                        secure=app.cfg.secure_cookies, samesite="lax", path="/")

    def bad(exc: Exception, status: int = 400) -> HTTPException:
        return HTTPException(status, str(exc))

    # --- страницы -------------------------------------------------------------
    @web.get("/", include_in_schema=False)
    async def landing():
        return FileResponse(STATIC / "index.html")

    @web.get("/app", include_in_schema=False)
    async def spa():
        return FileResponse(STATIC / "app.html", headers={"Cache-Control": "no-cache"})

    @web.get("/healthz", include_in_schema=False)
    async def healthz():
        await app.db.value("SELECT 1")
        return {"ok": True, "uptime": int(time.time() - app.started_at)}

    web.mount("/static", StaticFiles(directory=STATIC), name="static")

    # --- вход -----------------------------------------------------------------
    @web.get("/auth/link", include_in_schema=False)
    async def auth_link(request: Request, t: str = Query(...), next: str = Query("/app")):
        limiter.check(client_ip(request), "auth", 20, 60)
        h = app.vault.hash(t)
        row = await app.db.one("SELECT * FROM login_tokens WHERE token_hash = ? AND expires_at > ?", h, now())
        if not row:
            return RedirectResponse("/app#/expired", status_code=303)
        await app.db.run("DELETE FROM login_tokens WHERE token_hash = ?", h)  # одноразовая
        token = await create_session(app, row["user_id"], request.headers.get("user-agent", ""))
        target = next if next.startswith("/app") and "//" not in next else "/app"
        resp = RedirectResponse(target, status_code=303)
        set_cookie(resp, token)
        return resp

    @web.post("/api/auth/webapp")
    async def auth_webapp(request: Request, body: dict = Body(...)):
        limiter.check(client_ip(request), "auth", 30, 60)
        tg = check_webapp_init_data(str(body.get("init_data", "")), app.cfg.hub_token)
        if not tg.get("id"):
            raise HTTPException(401, "Нет данных пользователя")
        user, _ = await app.billing.ensure_user(int(tg["id"]), tg.get("username"), tg.get("first_name"))
        token = await create_session(app, user["id"], request.headers.get("user-agent", ""))
        resp = JSONResponse({"ok": True, "token": token})
        set_cookie(resp, token)
        return resp

    @web.get("/auth/widget", include_in_schema=False)
    async def auth_widget_redirect(request: Request):
        """Telegram Login Widget в режиме data-auth-url: данные приходят в query."""
        limiter.check(client_ip(request), "auth", 20, 60)
        if not await app.db.get_setting("login_widget", False):
            return RedirectResponse("/app", status_code=303)
        try:
            tg = check_login_widget(dict(request.query_params), app.cfg.hub_token)
        except HTTPException:
            return RedirectResponse("/app#/expired", status_code=303)
        user, _ = await app.billing.ensure_user(tg["id"], tg["username"], tg["first_name"])
        token = await create_session(app, user["id"], request.headers.get("user-agent", ""))
        resp = RedirectResponse("/app", status_code=303)
        set_cookie(resp, token)
        return resp

    @web.post("/api/auth/widget")
    async def auth_widget(request: Request, body: dict = Body(...)):
        limiter.check(client_ip(request), "auth", 20, 60)
        tg = check_login_widget(body, app.cfg.hub_token)
        user, _ = await app.billing.ensure_user(tg["id"], tg["username"], tg["first_name"])
        token = await create_session(app, user["id"], request.headers.get("user-agent", ""))
        resp = JSONResponse({"ok": True})
        set_cookie(resp, token)
        return resp

    @web.post("/api/auth/logout")
    async def logout(request: Request):
        token = session_token(request)
        if token:
            await app.db.run("DELETE FROM sessions WHERE token_hash = ?", app.vault.hash(token))
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    @web.post("/api/auth/logout-all")
    async def logout_all(user=Depends(current_user)):
        await app.db.run("DELETE FROM sessions WHERE user_id = ?", user["id"])
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    # --- публичное ------------------------------------------------------------
    @web.get("/api/config")
    async def config():
        return {"brand": app.cfg.brand, "hub": app.hub_username, "payments": {"cryptobot": app.pay.enabled},
                "support_url": await app.db.get_setting("support_url", ""),
                "widget": bool(await app.db.get_setting("login_widget", False)) and bool(app.hub_username),
                "referral_percent": await app.db.get_setting("referral_percent", 0)}

    @web.get("/api/catalog")
    async def catalog():
        items = await app.catalog.products()
        for item in items:
            for p in item["plans"]:
                p["label"] = plan_label(p["days"])
        return {"items": items}

    # --- кабинет --------------------------------------------------------------
    def user_view(user: Any) -> dict:
        return {"id": user["id"], "username": user["username"], "first_name": user["first_name"],
                "balance": user["balance"], "ref_earned": user["ref_earned"],
                "is_owner": app.is_owner(user["id"]), "created_at": user["created_at"]}

    @web.get("/api/me")
    async def me(user=Depends(current_user)):
        bots = await app.instances.for_user(user["id"])
        return {"user": user_view(user), "bots": [app.instances.view(b) for b in bots]}

    @web.post("/api/quote")
    async def quote(body: dict = Body(...), user=Depends(current_user)):
        try:
            q = await app.billing.quote(user["id"], str(body.get("product")), int(body.get("days", -1)),
                                        body.get("promo"))
        except (BillingError, ValueError) as exc:
            raise bad(exc)
        trial = await app.billing.trial_available(user["id"], str(body.get("product")))
        return q.as_dict() | {"balance": user["balance"], "trial": trial}

    @web.post("/api/order")
    async def order(body: dict = Body(...), user=Depends(current_user)):
        product, promo = str(body.get("product")), body.get("promo") or None
        iid = int(body["instance_id"]) if body.get("instance_id") else None
        try:
            days = int(body.get("days", -1))
            instance_id = await app.billing.purchase(user["id"], product, days, promo, iid)
        except InsufficientFunds as exc:
            return JSONResponse({"error": "Недостаточно средств", "need": exc.need}, status_code=402)
        except (BillingError, ValueError) as exc:
            raise bad(exc)
        app.sup.poke()
        return {"ok": True, "instance_id": instance_id}

    @web.post("/api/trial")
    async def trial(body: dict = Body(...), user=Depends(current_user)):
        try:
            instance_id = await app.billing.start_trial(user["id"], str(body.get("product")))
        except BillingError as exc:
            raise bad(exc)
        return {"ok": True, "instance_id": instance_id}

    @web.post("/api/topup")
    async def topup(request: Request, body: dict = Body(...), user=Depends(current_user)):
        limiter.check(client_ip(request), "topup", 10, 60)
        try:
            amount = int(body.get("amount", 0))
            intent = body.get("intent") if isinstance(body.get("intent"), dict) else None
            if intent and intent.get("action") != "purchase":
                intent = None
            inv = await app.pay.create(user["id"], amount, intent)
        except (PaymentError, ValueError) as exc:
            raise bad(exc)
        return inv

    @web.get("/api/invoices/{invoice_id}")
    async def invoice(invoice_id: int, user=Depends(current_user)):
        row = await app.db.one("SELECT id, amount, status, pay_url, created_at, paid_at FROM invoices "
                               "WHERE id = ? AND user_id = ?", invoice_id, user["id"])
        if not row:
            raise HTTPException(404, "Счёт не найден")
        return dict(row)

    @web.get("/api/wallet")
    async def wallet(user=Depends(current_user), before: int | None = None):
        rows = await app.db.all("SELECT id, amount, balance_after, kind, note, created_at FROM ledger "
                                "WHERE user_id = ? AND id < ? ORDER BY id DESC LIMIT 50",
                                user["id"], before or 2 ** 62)
        return {"balance": user["balance"], "items": [dict(r) for r in rows]}

    @web.post("/api/promo/balance")
    async def promo_balance(request: Request, body: dict = Body(...), user=Depends(current_user)):
        limiter.check(client_ip(request), "promo", 10, 60)
        try:
            amount = await app.billing.redeem_balance_promo(user["id"], str(body.get("code", "")))
        except BillingError as exc:
            raise bad(exc)
        return {"ok": True, "amount": amount}

    @web.get("/api/referrals")
    async def referrals(user=Depends(current_user)):
        count = await app.db.value("SELECT COUNT(*) FROM users WHERE ref_by = ?", user["id"], default=0)
        return {"link": f"https://t.me/{app.hub_username}?start=r{user['id']}", "count": count,
                "earned": user["ref_earned"], "percent": await app.db.get_setting("referral_percent", 0)}

    # --- мои боты -------------------------------------------------------------
    async def own(instance_id: int, user: Any) -> Any:
        try:
            return await app.instances.get(instance_id, None if app.is_owner(user["id"]) else user["id"])
        except InstanceError as exc:
            raise HTTPException(404, str(exc))

    @web.get("/api/bots/{instance_id}")
    async def bot(instance_id: int, user=Depends(current_user)):
        data = app.instances.view(await own(instance_id, user), with_logs=app.is_owner(user["id"]))
        if not app.is_owner(user["id"]):
            data.pop("runtime", None)  # техническое: память, pid - только администрации
        return data

    @web.post("/api/bots/{instance_id}/token")
    async def bot_token(request: Request, instance_id: int, body: dict = Body(...), user=Depends(current_user)):
        limiter.check(client_ip(request), "token", 15, 60)
        inst = await own(instance_id, user)
        try:
            info = await app.instances.set_token(None, inst["id"], str(body.get("token", "")))
        except InstanceError as exc:
            raise bad(exc)
        return {"ok": True, "username": info.username}

    @web.post("/api/bots/{instance_id}/action")
    async def bot_action(instance_id: int, body: dict = Body(...), user=Depends(current_user)):
        inst = await own(instance_id, user)
        action = body.get("action")
        try:
            if action == "restart":
                await app.instances.restart(None, inst["id"])
            elif action in ("pause", "resume"):
                await app.instances.set_flag(None, inst["id"], "paused", action == "pause")
            elif action == "auto_renew":
                await app.instances.set_flag(None, inst["id"], "auto_renew", bool(body.get("value")))
            elif action == "plan":
                await app.instances.set_plan(inst["user_id"], inst["id"], int(body.get("days", -1)))
            elif action == "bind_code":
                await app.instances.new_bind_code(inst["user_id"], inst["id"])
            elif action == "delete":
                await app.instances.delete(None, inst["id"])
            else:
                raise HTTPException(400, "Неизвестное действие")
        except (InstanceError, ValueError) as exc:
            raise bad(exc)
        return {"ok": True}

    @web.post("/api/bots/{instance_id}/settings")
    async def bot_settings(instance_id: int, body: dict = Body(...), user=Depends(current_user)):
        inst = await own(instance_id, user)
        try:
            settings = await app.instances.update_settings(None, inst["id"], dict(body.get("values") or {}))
        except InstanceError as exc:
            raise bad(exc)
        return {"ok": True, "settings": settings}

    @web.get("/api/bots/{instance_id}/backup")
    async def bot_backup(instance_id: int, user=Depends(current_user)):
        inst = await own(instance_id, user)
        blob = await app.backups.instance(inst["id"])
        name = f"{inst['product']}-{inst['bot_username'] or inst['id']}-{time.strftime('%Y%m%d')}.zip"
        return Response(blob, media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @web.post("/api/bots/{instance_id}/restore")
    async def bot_restore(instance_id: int, file: UploadFile = File(...), user=Depends(current_user)):
        inst = await own(instance_id, user)
        blob = await file.read(MAX_UPLOAD + 1)
        if len(blob) > MAX_UPLOAD:
            raise HTTPException(413, "Файл слишком большой")
        proc = app.sup.procs.get(inst["id"])
        if proc and proc.running:
            await app.sup._stop(proc, "восстановление из бэкапа")
        try:
            await app.backups.restore_instance(inst["id"], blob)
        except (ValueError, KeyError, OSError) as exc:
            raise bad(f"Не удалось восстановить: {exc}")
        finally:
            app.sup.poke()
        await app.db.audit(user["id"], "restore", f"instance:{inst['id']}")
        return {"ok": True}

    # --- вебхук CryptoBot -----------------------------------------------------
    @web.post("/api/cryptopay/webhook")
    async def cryptopay_webhook(request: Request):
        body = await request.body()
        try:
            paid = await app.pay.webhook(body, request.headers.get("crypto-pay-api-signature", ""))
        except PaymentError:
            raise HTTPException(401, "bad signature")
        if paid:
            from ..jobs import handle_paid
            await handle_paid(app, paid)
        return {"ok": True}

    # --- админка --------------------------------------------------------------
    @web.get("/api/admin/stats")
    async def admin_stats(_=Depends(owner_user)):
        ts = now()
        day = ts - ts % 86400

        async def v(sql: str, *args):
            return await app.db.value(sql, *args, default=0)
        revenue_days = await app.db.all(
            "SELECT (created_at / 86400) AS d, SUM(amount) AS s FROM ledger WHERE kind = 'topup' AND created_at >= ? "
            "GROUP BY d ORDER BY d", ts - 30 * 86400)
        users_days = await app.db.all(
            "SELECT (created_at / 86400) AS d, COUNT(*) AS n FROM users WHERE created_at >= ? GROUP BY d ORDER BY d",
            ts - 30 * 86400)
        by_product = await app.db.all(
            "SELECT product, COUNT(*) AS n FROM instances WHERE deleted_at IS NULL AND (forever = 1 OR "
            "(expires_at > ? AND expired_at IS NULL)) GROUP BY product", ts)
        return {
            "users": await v("SELECT COUNT(*) FROM users"),
            "users_today": await v("SELECT COUNT(*) FROM users WHERE created_at >= ?", day),
            "active": await v("SELECT COUNT(*) FROM instances WHERE deleted_at IS NULL AND (forever = 1 OR "
                              "(expires_at > ? AND expired_at IS NULL)) AND trial = 0", ts),
            "trials": await v("SELECT COUNT(*) FROM instances WHERE deleted_at IS NULL AND trial = 1 AND "
                              "expires_at > ?", ts),
            "expiring": await v("SELECT COUNT(*) FROM instances WHERE deleted_at IS NULL AND forever = 0 AND "
                                "expired_at IS NULL AND expires_at BETWEEN ? AND ?", ts, ts + 3 * 86400),
            "expiring_no_renew": await v("SELECT COUNT(*) FROM instances i JOIN users u ON u.id = i.user_id "
                                         "WHERE i.deleted_at IS NULL AND i.forever = 0 AND i.expired_at IS NULL "
                                         "AND i.expires_at BETWEEN ? AND ? AND (i.auto_renew = 0 OR i.trial = 1)",
                                         ts, ts + 3 * 86400),
            "topup_today": await v("SELECT SUM(amount) FROM ledger WHERE kind = 'topup' AND created_at >= ?", day),
            "topup_month": await v("SELECT SUM(amount) FROM ledger WHERE kind = 'topup' AND created_at >= ?",
                                   ts - 30 * 86400),
            "sales_month": await v("SELECT -SUM(amount) FROM ledger WHERE kind IN ('purchase', 'renew') AND "
                                   "created_at >= ?", ts - 30 * 86400),
            "balances": await v("SELECT SUM(balance) FROM users"),
            "revenue_days": [{"day": r["d"] * 86400, "sum": r["s"]} for r in revenue_days],
            "users_days": [{"day": r["d"] * 86400, "n": r["n"]} for r in users_days],
            "by_product": {r["product"]: r["n"] for r in by_product},
            "runtime": app.sup.totals(),
            "uptime": int(time.time() - app.started_at),
        }

    @web.get("/api/admin/instances")
    async def admin_instances(_=Depends(owner_user), q: str = "", status: str = "", product: str = "",
                              problems: bool = False, limit: int = 100, offset: int = 0):
        where, args = ["i.deleted_at IS NULL"], []
        if problems:
            where.append("i.status IN ('crashed', 'token_invalid', 'queued', 'restarting', 'waiting_bind')")
        if status:
            where.append("i.status = ?")
            args.append(status)
        if product:
            where.append("i.product = ?")
            args.append(product)
        if q:
            like = f"%{q.lstrip('@')}%"
            where.append("(i.bot_username LIKE ? OR CAST(i.user_id AS TEXT) = ? OR CAST(i.id AS TEXT) = ? "
                         "OR u.username LIKE ?)")
            args += [like, q, q, like]
        rows = await app.db.all(f"SELECT i.*, u.username AS owner_username FROM instances i "
                                f"JOIN users u ON u.id = i.user_id WHERE {' AND '.join(where)} "
                                f"ORDER BY i.id DESC LIMIT ? OFFSET ?", *args, min(limit, 500), offset)
        return {"items": [app.instances.view(r) | {"owner_username": r["owner_username"]} for r in rows]}

    @web.post("/api/admin/instances/{instance_id}")
    async def admin_instance_action(instance_id: int, body: dict = Body(...), admin=Depends(owner_user)):
        action = body.get("action")
        try:
            if action in ("freeze", "unfreeze"):
                await app.instances.set_flag(admin["id"], instance_id, "frozen", action == "freeze")
            elif action == "gift":
                await app.instances.gift_days(admin["id"], instance_id, int(body.get("days", 0)))
            elif action == "restart":
                await app.instances.restart(None, instance_id)
            elif action == "delete":
                await app.instances.delete(admin["id"], instance_id)
            else:
                raise HTTPException(400, "Неизвестное действие")
        except (InstanceError, ValueError) as exc:
            raise bad(exc)
        return {"ok": True}

    @web.get("/api/admin/users")
    async def admin_users(_=Depends(owner_user), q: str = "", limit: int = 50, offset: int = 0):
        if q:
            like = f"%{q.lstrip('@')}%"
            rows = await app.db.all("SELECT * FROM users WHERE CAST(id AS TEXT) = ? OR username LIKE ? "
                                    "OR first_name LIKE ? ORDER BY id DESC LIMIT ? OFFSET ?",
                                    q, like, like, min(limit, 200), offset)
        else:
            rows = await app.db.all("SELECT * FROM users ORDER BY created_at DESC LIMIT ? OFFSET ?",
                                    min(limit, 200), offset)
        out = []
        for r in rows:
            n = await app.db.value("SELECT COUNT(*) FROM instances WHERE user_id = ? AND deleted_at IS NULL",
                                   r["id"], default=0)
            out.append(dict(r) | {"bots": n})
        return {"items": out}

    @web.get("/api/admin/users/{user_id}")
    async def admin_user(user_id: int, _=Depends(owner_user)):
        row = await app.db.one("SELECT * FROM users WHERE id = ?", user_id)
        if not row:
            raise HTTPException(404, "Клиент не найден")
        bots = await app.db.all("SELECT * FROM instances WHERE user_id = ? AND deleted_at IS NULL", user_id)
        ledger = await app.db.all("SELECT * FROM ledger WHERE user_id = ? ORDER BY id DESC LIMIT 100", user_id)
        return {"user": dict(row), "bots": [app.instances.view(b) for b in bots],
                "ledger": [dict(x) for x in ledger]}

    @web.post("/api/admin/users/{user_id}")
    async def admin_user_action(user_id: int, body: dict = Body(...), admin=Depends(owner_user)):
        action = body.get("action")
        try:
            if action == "balance":
                balance = await app.billing.admin_adjust(admin["id"], user_id, int(body.get("amount", 0)),
                                                         str(body.get("note", ""))[:200])
                if int(body.get("amount", 0)) > 0 and body.get("notify", True):
                    app.notifier.send(user_id, f"🎁 На ваш баланс зачислено {money(int(body['amount']))}")
                return {"ok": True, "balance": balance}
            if action in ("ban", "unban"):
                await app.db.run("UPDATE users SET banned = ? WHERE id = ?", int(action == "ban"), user_id)
                if action == "ban":
                    await app.db.run("DELETE FROM sessions WHERE user_id = ?", user_id)
                    await app.db.run("UPDATE instances SET frozen = 1 WHERE user_id = ? AND deleted_at IS NULL",
                                     user_id)
                else:
                    await app.db.run("UPDATE instances SET frozen = 0 WHERE user_id = ? AND deleted_at IS NULL",
                                     user_id)
                await app.db.audit(admin["id"], action, f"user:{user_id}")
                app.sup.poke()
                return {"ok": True}
        except (BillingError, ValueError) as exc:
            raise bad(exc)
        raise HTTPException(400, "Неизвестное действие")

    @web.get("/api/admin/catalog")
    async def admin_catalog(_=Depends(owner_user)):
        items = await app.catalog.products(include_hidden=True)
        for item in items:
            for p in item["plans"]:
                p["label"] = plan_label(p["days"])
        return {"items": items}

    @web.post("/api/admin/products/{slug}")
    async def admin_product(slug: str, body: dict = Body(...), admin=Depends(owner_user)):
        if not app.catalog.get(slug):
            raise HTTPException(404, "Нет такого модуля")
        if "enabled" in body:
            await app.db.run("UPDATE products SET enabled = ? WHERE slug = ?", int(bool(body["enabled"])), slug)
        if "trial_days" in body:
            days = max(0, min(30, int(body["trial_days"])))
            await app.db.run("UPDATE products SET trial_days = ? WHERE slug = ?", days, slug)
        if "sort" in body:
            await app.db.run("UPDATE products SET sort = ? WHERE slug = ?", int(body["sort"]), slug)
        for plan in body.get("plans", []):
            days = int(plan["days"])
            if days < 0 or days > 3650:
                raise HTTPException(400, "Неверный срок")
            price = plan.get("price")
            price = None if price in (None, "") else int(price)
            if price is not None and (price < 0 or price > 10_000_000):
                raise HTTPException(400, "Неверная цена")
            await app.db.run("INSERT INTO plans(product, days, price, enabled) VALUES (?, ?, ?, ?) "
                             "ON CONFLICT(product, days) DO UPDATE SET price = excluded.price, "
                             "enabled = excluded.enabled", slug, days, price, int(bool(plan.get("enabled", True))))
        for days in body.get("remove_plans", []):
            await app.db.run("DELETE FROM plans WHERE product = ? AND days = ?", slug, int(days))
        await app.db.audit(admin["id"], "product", slug, json.dumps(body, ensure_ascii=False))
        return {"ok": True}

    @web.get("/api/admin/promos")
    async def admin_promos(_=Depends(owner_user)):
        rows = await app.db.all("SELECT * FROM promocodes ORDER BY created_at DESC")
        return {"items": [dict(r) for r in rows]}

    @web.post("/api/admin/promos")
    async def admin_promo_create(body: dict = Body(...), admin=Depends(owner_user)):
        code = str(body.get("code", "")).strip().upper()
        kind = body.get("kind")
        if not code.replace("_", "").replace("-", "").isalnum() or len(code) > 32:
            raise HTTPException(400, "Код: латиница и цифры, до 32 символов")
        if kind not in ("percent", "amount", "balance"):
            raise HTTPException(400, "Неверный тип промокода")
        value = int(body.get("value", 0))
        if value <= 0 or (kind == "percent" and value > 100):
            raise HTTPException(400, "Неверное значение")
        product = body.get("product") or None
        if product and not app.catalog.get(product):
            raise HTTPException(400, "Нет такого бота")
        expires = int(body["expires_at"]) if body.get("expires_at") else None
        try:
            await app.db.run("INSERT INTO promocodes(code, kind, value, product, max_uses, expires_at, created_at) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?)", code, kind, value, product,
                             max(0, int(body.get("max_uses", 0))), expires, now())
        except Exception:
            raise HTTPException(400, "Такой промокод уже есть")
        await app.db.audit(admin["id"], "promo_create", code)
        return {"ok": True}

    @web.delete("/api/admin/promos/{code}")
    async def admin_promo_delete(code: str, admin=Depends(owner_user)):
        await app.db.run("DELETE FROM promocodes WHERE code = ?", code.upper())
        await app.db.audit(admin["id"], "promo_delete", code)
        return {"ok": True}

    @web.get("/api/admin/payments")
    async def admin_payments(_=Depends(owner_user), limit: int = 100):
        rows = await app.db.all("SELECT i.*, u.username FROM invoices i JOIN users u ON u.id = i.user_id "
                                "ORDER BY i.id DESC LIMIT ?", min(limit, 500))
        return {"items": [dict(r) for r in rows]}

    SETTINGS_KEYS = {"support_url": str, "referral_percent": int, "backup_to_telegram": bool,
                     "login_widget": bool}

    @web.get("/api/admin/settings")
    async def admin_settings(_=Depends(owner_user)):
        defaults = {"support_url": "", "referral_percent": 0, "backup_to_telegram": True, "login_widget": False}
        return {k: await app.db.get_setting(k, d) for k, d in defaults.items()}

    @web.post("/api/admin/settings")
    async def admin_settings_save(body: dict = Body(...), admin=Depends(owner_user)):
        for key, value in body.items():
            kind = SETTINGS_KEYS.get(key)
            if not kind:
                raise HTTPException(400, f"Неизвестная настройка {key}")
            value = kind(value)
            if key == "referral_percent" and not 0 <= value <= 50:
                raise HTTPException(400, "Процент рефералки: от 0 до 50")
            if key == "support_url" and value and not value.startswith(("https://", "tg://")):
                raise HTTPException(400, "Ссылка поддержки должна начинаться с https://")
            await app.db.set_setting(key, value)
        await app.db.audit(admin["id"], "settings", "", json.dumps(body, ensure_ascii=False))
        return {"ok": True}

    # --- оформление ботов -----------------------------------------------------
    @web.get("/api/admin/design")
    async def admin_design(_=Depends(owner_user)):
        return {"items": [t | {"values": await design.values(app, t["slug"])} for t in design.targets(app)]}

    @web.post("/api/admin/design/{slug}")
    async def admin_design_save(slug: str, body: dict = Body(...), admin=Depends(owner_user)):
        try:
            applied = await design.save(app, slug, body, admin["id"])
        except design.DesignError as exc:
            raise bad(exc)
        return {"ok": True, "applied": applied}

    @web.get("/api/admin/emoji/{emoji_id}")
    async def admin_emoji(emoji_id: str, _=Depends(owner_user)):
        found = await design.emoji_image(app, emoji_id)
        if not found:
            raise HTTPException(404, "Нет превью")
        data, mime = found
        return Response(data, media_type=mime, headers={"Cache-Control": "private, max-age=604800"})

    @web.get("/api/admin/backups")
    async def admin_backups(_=Depends(owner_user)):
        return {"items": app.backups.list()}

    @web.post("/api/admin/backups")
    async def admin_backup_now(admin=Depends(owner_user)):
        from ..jobs import run_backup
        name = await run_backup(app, send=False)
        if not name:
            raise HTTPException(500, "Бэкап не удался")
        await app.db.audit(admin["id"], "backup", name)
        return {"ok": True, "name": name}

    @web.get("/api/admin/backups/{name}")
    async def admin_backup_download(name: str, _=Depends(owner_user)):
        path = app.backups.path(name)
        if not path:
            raise HTTPException(404, "Нет такого бэкапа")
        return FileResponse(path, media_type="application/zip", filename=name)

    @web.get("/api/admin/audit")
    async def admin_audit(_=Depends(owner_user), limit: int = 200):
        rows = await app.db.all("SELECT * FROM audit ORDER BY id DESC LIMIT ?", min(limit, 1000))
        return {"items": [dict(r) for r in rows]}

    @web.post("/api/admin/broadcast")
    async def admin_broadcast(body: dict = Body(...), admin=Depends(owner_user)):
        text = str(body.get("text", "")).strip()
        if not text or len(text) > 4000:
            raise HTTPException(400, "Текст от 1 до 4000 символов")
        ids = [r["id"] for r in await app.db.all("SELECT id FROM users WHERE banned = 0 AND hub_blocked = 0")]
        for uid in ids:
            app.notifier.send(uid, text)
        await app.db.audit(admin["id"], "broadcast", "", text[:200])
        return {"ok": True, "queued": len(ids)}

    return web
