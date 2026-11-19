// Baksi Cloud: кабинет клиента, Mini App и админка владельца. Без сборки и зависимостей.
"use strict";

const TG = window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.initData ? window.Telegram.WebApp : null;
const S = { cfg: null, me: null, bots: [], catalog: null, timer: null, token: sessionStorage.getItem("bc_token") };

// ---------- утилиты ----------
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "html") el.innerHTML = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
const $root = () => document.getElementById("root");
const money = (c) => (c == null ? "—" : (c < 0 ? "-$" : "$") + (Math.abs(c) / 100).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
function moneyBig(c) {
  const [a, b] = money(c).split(".");
  return h("span", {}, a, h("span", { class: "c" }, "." + (b || "00")));
}
const toCents = (v) => { const n = Math.round(parseFloat(String(v).replace(",", ".").replace("$", "")) * 100); return Number.isFinite(n) && n > 0 ? n : null; };
const date = (ts) => (ts ? new Date(ts * 1000).toLocaleDateString("ru-RU", { day: "numeric", month: "long", year: "numeric" }) : "—");
const dateTime = (ts) => (ts ? new Date(ts * 1000).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—");
const planLabel = (d) => (d === 0 ? "Навсегда" : d % 365 === 0 ? (d / 365 === 1 ? "1 год" : `${d / 365} года`) : `${d} дн.`);

function toast(text, bad) {
  document.querySelectorAll(".toast").forEach((t) => t.remove());
  const t = h("div", { class: "toast" + (bad ? " bad" : ""), role: "status" }, text);
  document.body.append(t);
  setTimeout(() => t.remove(), 3200);
  if (TG && TG.HapticFeedback) TG.HapticFeedback.notificationOccurred(bad ? "error" : "success");
}

class ApiError extends Error { constructor(msg, status, data) { super(msg); this.status = status; this.data = data; } }

async function api(method, path, body, raw) {
  const headers = { "X-Baksi": "1" };
  if (S.token) headers.Authorization = "Bearer " + S.token;
  let payload;
  if (body instanceof FormData) payload = body;
  else if (body !== undefined) { headers["Content-Type"] = "application/json"; payload = JSON.stringify(body); }
  const res = await fetch(path, { method, headers, body: payload, credentials: "same-origin" });
  if (raw && res.ok) return res;
  let data = {};
  try { data = await res.json(); } catch (e) { /* пусто */ }
  if (!res.ok) {
    if (res.status === 401 && !path.startsWith("/api/auth")) { S.me = null; renderLogin(); }
    throw new ApiError(data.error || "Ошибка запроса", res.status, data);
  }
  return data;
}

async function act(btn, fn, okText) {
  const was = btn && btn.disabled;
  if (btn) btn.disabled = true;
  try {
    const r = await fn();
    if (okText) toast(okText);
    return r;
  } catch (e) {
    if (e.status !== 401) toast(e.message, true);
    return undefined;
  } finally {
    if (btn) btn.disabled = was;
  }
}

function openLink(url) {
  if (!url) return;
  if (TG && /^https:\/\/t\.me\//.test(url)) TG.openTelegramLink(url);
  else if (TG) TG.openLink(url);
  else window.open(url, "_blank", "noopener");
}

async function copy(text) {
  try { await navigator.clipboard.writeText(text); toast("Скопировано"); } catch (e) { toast("Не получилось скопировать", true); }
}

const ICONS = {
  bots: '<rect x="4" y="8" width="16" height="12" rx="3"/><path d="M12 4v4M9 13h.01M15 13h.01"/>',
  catalog: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  wallet: '<rect x="3" y="6" width="18" height="14" rx="2"/><path d="M3 10h18M16 15h2"/>',
  gift: '<path d="M20 12v8H4v-8M2 7h20v5H2zM12 22V7"/>',
  chart: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
  users: '<circle cx="9" cy="8" r="4"/><path d="M2 21a7 7 0 0 1 14 0M17 4a4 4 0 0 1 0 8M22 21a7 7 0 0 0-4-6.3"/>',
  server: '<rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/>',
  tag: '<path d="M20.6 13.4 13.4 20.6a2 2 0 0 1-2.8 0L3 13V3h10l7.6 7.6a2 2 0 0 1 0 2.8z"/><path d="M7.5 7.5h.01"/>',
  card: '<rect x="2" y="5" width="20" height="14" rx="2"/><path d="M2 10h20"/>',
  cog: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
  back: '<path d="M15 18l-6-6 6-6"/>',
};
const icon = (name) => h("span", { "aria-hidden": "true", html: `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${ICONS[name] || ""}</svg>` });

const STATUS = {
  running: "ok", waiting_bind: "warn", starting: "warn", restarting: "warn", queued: "warn",
  crashed: "bad", token_invalid: "bad", expired: "bad", setup: "idle", stopped: "idle", frozen: "bad", unavailable: "bad",
};
const badge = (b) => h("span", { class: "badge " + (STATUS[b.status] || "idle") }, b.status_text);
const avatar = (b) => h("span", { class: "avatar", style: `background:${b.color}` }, (b.title || "?")[0]);

// ---------- каркас ----------
function navItems() {
  const items = [
    ["#/", "Мои боты", "bots"], ["#/catalog", "Каталог", "catalog"], ["#/wallet", "Кошелёк", "wallet"], ["#/referrals", "Рефералы", "gift"],
  ];
  const admin = [
    ["#/admin", "Обзор", "chart"], ["#/admin/instances", "Копии ботов", "server"], ["#/admin/users", "Клиенты", "users"],
    ["#/admin/catalog", "Тарифы", "tag"], ["#/admin/promos", "Промокоды", "gift"], ["#/admin/payments", "Платежи", "card"],
    ["#/admin/settings", "Настройки", "cog"],
  ];
  return { items, admin: S.me && S.me.is_owner ? admin : [] };
}

function isActive(href) {
  const cur = location.hash || "#/";
  if (href === "#/") return cur === "#/" || cur.startsWith("#/bot/");
  if (href === "#/admin") return cur === "#/admin";
  return cur === href || cur.startsWith(href + "/");
}

function shell(content) {
  const { items, admin } = navItems();
  const link = ([href, label, ic]) => h("a", { href, class: isActive(href) ? "on" : null }, icon(ic), label);
  const side = h("aside", { class: "side" },
    h("a", { class: "logo", href: "/" }, h("span", { class: "logo-mark", "aria-hidden": "true" }, "B"), S.cfg.brand),
    h("nav", { "aria-label": "Кабинет" }, items.map(link), admin.length ? [h("div", { class: "group" }, "Администрирование"), admin.map(link)] : null),
    h("div", { class: "me" },
      h("b", {}, S.me.first_name || (S.me.username ? "@" + S.me.username : "Аккаунт")),
      h("span", {}, "ID " + S.me.id),
      TG ? null : h("button", { class: "btn small ghost", onclick: logout }, "Выйти")));
  const tabs = h("nav", { class: "tabbar", "aria-label": "Разделы" },
    [...items, ...(admin.length ? [["#/admin", "Админка", "chart"]] : [])].map(([href, label, ic]) =>
      h("a", { href, class: isActive(href) || (href === "#/admin" && location.hash.startsWith("#/admin")) ? "on" : null }, icon(ic), label)));
  $root().replaceChildren(h("div", { class: "shell" }, side, h("main", { class: "main" }, h("div", { class: "wrap" }, content)), tabs));
  window.scrollTo(0, 0);
}

const head = (title, ...right) => h("div", { class: "page-head" }, h("h1", {}, title), right.length ? h("div", { class: "actions" }, right) : null);
const back = (href, text) => h("a", { class: "back", href }, icon("back"), text);

async function logout() {
  await act(null, () => api("POST", "/api/auth/logout"));
  S.token = null; sessionStorage.removeItem("bc_token");
  location.href = "/";
}

// ---------- вход ----------
function renderLogin(expired) {
  clearInterval(S.timer);
  const hub = S.cfg && S.cfg.hub;
  const box = h("div", { class: "box" },
    h("span", { class: "logo-mark", style: "width:56px;height:56px;font-size:26px;border-radius:16px" }, "B"),
    h("h1", {}, S.cfg ? S.cfg.brand : "Кабинет"),
    h("p", { class: "muted" }, expired ? "Ссылка для входа устарела или уже использована. Получите новую в боте." :
      "Вход без пароля: бот пришлёт одноразовую ссылку в Telegram."),
    hub ? h("a", { class: "btn primary wide", href: `https://t.me/${hub}?start=login` }, "Получить ссылку для входа") : null);
  if (hub && S.cfg.widget) {
    const w = document.createElement("script");
    w.async = true;
    w.src = "https://telegram.org/js/telegram-widget.js?22";
    w.setAttribute("data-telegram-login", hub);
    w.setAttribute("data-size", "large");
    w.setAttribute("data-auth-url", location.origin + "/auth/widget");
    w.setAttribute("data-request-access", "write");
    box.append(h("p", { class: "hint" }, "или"), w);
  }
  $root().replaceChildren(h("div", { class: "login" }, box));
}

// ---------- мои боты ----------
async function loadMe() {
  const r = await api("GET", "/api/me");
  S.me = r.user; S.bots = r.bots;
}

async function pageHome() {
  await loadMe();
  const problems = S.bots.filter((b) => ["token_invalid", "crashed", "expired", "setup", "waiting_bind"].includes(b.status));
  const running = S.bots.filter((b) => b.status === "running").length;
  const list = S.bots.length
    ? h("div", { class: "panel flush" }, h("div", { class: "list" }, S.bots.map((b) =>
      h("a", { class: "row", href: `#/bot/${b.id}` }, avatar(b),
        h("span", { class: "grow" },
          h("span", { class: "title" }, b.title, b.bot_username ? h("span", { class: "muted", style: "font-weight:400" }, "  @" + b.bot_username) : null),
          h("span", { class: "meta" }, b.forever ? "Навсегда" : (b.trial ? "Пробный до " : "До ") + date(b.expires_at))),
        badge(b)))))
    : h("div", { class: "panel empty" }, h("p", {}, "У вас пока нет ботов. Выберите первого в каталоге: запуск займёт пару минут."),
      h("a", { class: "btn primary", href: "#/catalog" }, "Открыть каталог"));
  shell([
    head("Мои боты", h("a", { class: "btn primary", href: "#/catalog" }, "Новый бот")),
    h("div", { class: "balance" },
      h("div", {}, h("div", { class: "k" }, "Баланс"), h("div", { class: "v" }, moneyBig(S.me.balance))),
      h("div", { class: "actions" }, h("a", { class: "btn primary", href: "#/wallet" }, "Пополнить"),
        h("span", { class: "muted" }, S.bots.length ? `Работают ${running} из ${S.bots.length}` : ""))),
    problems.length ? h("div", { class: "alert warn" }, `Требуют внимания: ${problems.map((b) => `${b.title}${b.bot_username ? " @" + b.bot_username : ""}`).join(", ")}. Откройте бота, там подсказка, что сделать.`) : null,
    list,
  ]);
}

// ---------- каталог ----------
async function loadCatalog() { S.catalog = (await api("GET", "/api/catalog")).items; }

async function pageCatalog() {
  await loadCatalog();
  shell([
    head("Каталог ботов"),
    h("p", { class: "muted" }, "Свой бот на своём токене. Пользователи видят только ваше имя и юзернейм."),
    S.catalog.length ? h("div", { class: "cat-list" }, S.catalog.map((p) =>
      h("a", { class: "cat-item", href: `#/product/${p.slug}` },
        h("span", { class: "avatar", style: `background:${p.color}` }, p.title[0]),
        h("div", {}, h("b", { style: "font-size:17px" }, p.title, h("span", { class: "muted", style: "font-weight:400" }, "  " + p.tagline)),
          h("p", {}, p.description)),
        h("div", { style: "text-align:right" }, h("b", {}, "от " + money(p.min_price)),
          p.trial_days ? h("div", { class: "hint" }, `${p.trial_days} дн. бесплатно`) : null))))
      : h("div", { class: "panel empty" }, "Каталог скоро откроется."),
  ]);
}

async function pageProduct(slug, instanceId) {
  await Promise.all([loadCatalog(), loadMe()]);
  const p = S.catalog.find((x) => x.slug === slug);
  if (!p) { shell([back("#/catalog", "Каталог"), h("div", { class: "panel empty" }, "Этот бот сейчас недоступен.")]); return; }
  let days = p.plans[Math.min(1, p.plans.length - 1)].days;
  let promo = "";
  const summary = h("div", { class: "kv" });
  const payBtn = h("button", { class: "btn primary wide" }, "Оплатить");
  const plans = h("div", { class: "plans", role: "group", "aria-label": "Срок подписки" });
  const promoInput = h("input", { type: "text", placeholder: "Промокод", "aria-label": "Промокод", autocomplete: "off" });

  async function refresh() {
    plans.replaceChildren(...p.plans.map((pl) => h("button", {
      class: "plan", type: "button", "aria-pressed": String(pl.days === days),
      onclick: () => { days = pl.days; refresh(); },
    }, h("b", {}, pl.label), h("span", {}, money(pl.price)))));
    try {
      const q = await api("POST", "/api/quote", { product: slug, days, promo });
      const lack = q.total - q.balance;
      summary.replaceChildren(...[
        h("div", {}, h("span", {}, "Тариф"), h("span", {}, `${q.label} · ${money(q.price)}`)),
        q.promo ? h("div", {}, h("span", {}, `Промокод ${q.promo}`), h("span", {}, "−" + money(q.discount))) : null,
        q.promo_error ? h("div", {}, h("span", {}, "Промокод"), h("span", { style: "color:var(--coral)" }, q.promo_error)) : null,
        h("div", {}, h("span", {}, "К оплате"), h("b", {}, money(q.total))),
        h("div", {}, h("span", {}, "На балансе"), h("span", {}, money(q.balance)))].filter(Boolean));
      payBtn.textContent = lack > 0 ? `Пополнить на ${money(Math.max(lack, 100))} и оплатить` : `Оплатить ${money(q.total)} с баланса`;
      payBtn.onclick = () => (lack > 0 ? topupAndBuy(payBtn, Math.max(lack, 100)) : buy(payBtn));
      payBtn.disabled = lack > 0 && !S.cfg.payments.cryptobot;
    } catch (e) { toast(e.message, true); }
  }
  async function buy(btn) {
    const r = await act(btn, () => api("POST", "/api/order", { product: slug, days, promo, instance_id: instanceId || null }));
    if (r) { toast("Оплачено"); location.hash = `#/bot/${r.instance_id}`; }
  }
  async function topupAndBuy(btn, amount) {
    const inv = await act(btn, () => api("POST", "/api/topup", { amount, intent: { action: "purchase", product: slug, days, promo: promo || null, instance_id: instanceId || null } }));
    if (inv) watchInvoice(inv, () => { location.hash = "#/"; });
  }
  const trialAvailable = !instanceId && p.trial_days > 0 && (await api("POST", "/api/quote", { product: slug, days }).then((q) => q.trial).catch(() => false));
  shell([
    back(instanceId ? `#/bot/${instanceId}` : "#/catalog", instanceId ? "К боту" : "Каталог"),
    h("div", { class: "page-head" }, h("div", { style: "display:flex;gap:14px;align-items:center" },
      h("span", { class: "avatar", style: `background:${p.color};width:56px;height:56px;font-size:22px` }, p.title[0]),
      h("div", {}, h("h1", {}, instanceId ? `Продление: ${p.title}` : p.title), h("div", { class: "muted" }, p.tagline)))),
    h("div", { class: "grid2" },
      h("div", { class: "panel" }, h("p", {}, p.description),
        p.how.length ? [h("h2", { style: "margin:18px 0 10px;font-size:16px" }, "Как работает"), h("ol", { style: "margin:0;padding-left:20px;display:grid;gap:6px;color:#cfd5c9" }, p.how.map((s) => h("li", {}, s)))] : null,
        p.features.length ? [h("h2", { style: "margin:18px 0 10px;font-size:16px" }, "Возможности"), h("ul", { style: "margin:0;padding-left:20px;display:grid;gap:6px;color:#cfd5c9" }, p.features.map((s) => h("li", {}, s)))] : null),
      h("div", { class: "panel", style: "display:grid;gap:16px" },
        h("h2", { style: "margin:0" }, "Срок подписки"), plans,
        h("div", { style: "display:flex;gap:8px" }, promoInput,
          h("button", { class: "btn", onclick: () => { promo = promoInput.value.trim(); refresh(); } }, "Применить")),
        summary, payBtn,
        trialAvailable ? h("button", { class: "btn wide", onclick: async (e) => {
          const r = await act(e.currentTarget, () => api("POST", "/api/trial", { product: slug }));
          if (r) { toast("Пробный период активирован"); location.hash = `#/bot/${r.instance_id}`; }
        } }, `Попробовать ${p.trial_days} дн. бесплатно`) : null,
        !S.cfg.payments.cryptobot ? h("p", { class: "hint" }, "Пополнение пока недоступно. Напишите в поддержку.") : null)),
  ]);
  refresh();
}

function watchInvoice(inv, onPaid) {
  openLink(inv.url);
  toast(`Счёт на ${money(inv.amount)} открыт в @CryptoBot`);
  let tries = 0;
  const t = setInterval(async () => {
    tries += 1;
    if (tries > 200) { clearInterval(t); return; }
    try {
      const r = await api("GET", `/api/invoices/${inv.id}`);
      if (r.status === "paid") { clearInterval(t); toast(`Баланс пополнен на ${money(r.amount)}`); onPaid && onPaid(); }
      if (r.status === "expired") clearInterval(t);
    } catch (e) { clearInterval(t); }
  }, 4000);
}

// ---------- карточка бота ----------
async function pageBot(id) {
  const b = await api("GET", `/api/bots/${id}`);
  const tokenForm = () => {
    const input = h("input", { type: "text", placeholder: "123456789:AA…", autocomplete: "off", spellcheck: "false", "aria-label": "Токен бота" });
    const btn = h("button", { class: "btn primary", type: "submit" }, "Подключить");
    return h("form", { style: "display:grid;gap:10px", onsubmit: async (e) => {
      e.preventDefault();
      const r = await act(btn, () => api("POST", `/api/bots/${id}/token`, { token: input.value.trim() }));
      if (r) { toast(`Бот @${r.username} подключён`); setTimeout(() => route(), 1200); }
    } }, h("p", { class: "hint" }, "Откройте @BotFather → /newbot (или выберите своего бота → API Token) и вставьте токен целиком."),
    h("div", { style: "display:flex;gap:8px" }, input, btn));
  };

  const alerts = [];
  if (!b.has_token) alerts.push(h("div", { class: "panel", style: "display:grid;gap:12px;border-color:var(--lime)" }, h("h2", { style: "margin:0" }, "Подключите своего бота"), tokenForm()));
  if (b.status === "token_invalid") alerts.push(h("div", { class: "alert bad" }, h("b", {}, "Telegram отклонил токен"), "Бота удалили или сбросили токен в @BotFather. Данные и настройки сохранены: вставьте новый токен.", tokenForm()));
  if (b.status === "expired") alerts.push(h("div", { class: "alert bad" }, h("b", {}, "Подписка закончилась"), `Бот остановлен, данные хранятся ещё некоторое время.`, h("a", { class: "btn primary", href: `#/renew/${b.product}/${b.id}` }, "Продлить")));
  if (b.status === "crashed") alerts.push(h("div", { class: "alert bad" }, h("b", {}, "Бот несколько раз упал при запуске"), b.status_note || "Мы уже получили уведомление. Попробуйте перезапустить или посмотрите события ниже."));
  if (b.needs_bind && b.has_token && !b.group) alerts.push(h("div", { class: "alert warn" }, h("b", {}, "Привяжите группу"), b.bind_hint || "Добавьте бота в группу и отправьте туда команду:",
    h("div", { class: "code" }, `/bind ${b.bind_code}`, h("button", { class: "btn small", onclick: () => copy(`/bind ${b.bind_code}`) }, "Скопировать"))));

  const settingsPanel = b.settings_schema.length ? (() => {
    const fields = b.settings_schema.map((s) => {
      const val = b.settings[s.key] ?? s.default;
      if (s.type === "bool") {
        const inp = h("input", { type: "checkbox", "data-key": s.key, "data-type": s.type });
        inp.checked = !!val;
        return h("label", { style: "display:flex;gap:14px;align-items:center;justify-content:space-between" }, h("span", {}, s.label), h("span", { class: "switch" }, inp, h("span", {})));
      }
      if (s.type === "choice") return h("label", { class: "field" }, h("span", {}, s.label),
        h("select", { "data-key": s.key, "data-type": s.type }, s.options.map(([v, l]) => h("option", { value: v, selected: v === val }, l))), s.hint ? h("span", {}, s.hint) : null);
      const shown = Array.isArray(val) ? val.join(", ") : (val ?? "");
      return h("label", { class: "field" }, h("span", {}, s.label), h("input", { type: s.type === "int" ? "number" : "text", value: shown, "data-key": s.key, "data-type": s.type }), s.hint ? h("span", {}, s.hint) : null);
    });
    const btn = h("button", { class: "btn primary", type: "submit" }, "Сохранить");
    return h("form", { class: "panel", style: "display:grid;gap:14px", onsubmit: async (e) => {
      e.preventDefault();
      const values = {};
      e.currentTarget.querySelectorAll("[data-key]").forEach((el) => {
        values[el.dataset.key] = el.dataset.type === "bool" ? el.checked : el.dataset.type === "ids" ? el.value.split(/[\s,]+/).filter(Boolean) : el.value;
      });
      await act(btn, () => api("POST", `/api/bots/${id}/settings`, { values }), "Сохранено, бот перезапускается");
    } }, h("h2", { style: "margin:0" }, "Настройки"), fields, h("div", {}, btn));
  })() : null;

  const autoRenew = h("input", { type: "checkbox", onchange: (e) => act(null, () => api("POST", `/api/bots/${id}/action`, { action: "auto_renew", value: e.target.checked }), "Сохранено") });
  autoRenew.checked = b.auto_renew;
  const logsBox = h("pre", { class: "logs" }, (b.logs || []).join("\n") || "Событий пока нет.");
  const restoreInput = h("input", { type: "file", accept: ".zip", class: "sr", onchange: async (e) => {
    const f = e.target.files[0];
    if (!f || !confirm("Заменить данные бота данными из бэкапа? Бот перезапустится.")) return;
    const fd = new FormData(); fd.append("file", f);
    await act(null, () => api("POST", `/api/bots/${id}/restore`, fd), "Данные восстановлены");
  } });
  const rt = b.runtime || {};

  shell([
    back(S.me && S.me.is_owner && b.user_id !== S.me.id ? "#/admin/instances" : "#/", "Назад"),
    h("div", { class: "page-head" },
      h("div", { style: "display:flex;gap:14px;align-items:center;min-width:0" }, h("span", { class: "avatar", style: `background:${b.color};width:56px;height:56px;font-size:22px` }, b.title[0]),
        h("div", { style: "min-width:0" }, h("h1", {}, b.title), h("div", { class: "muted" }, b.bot_username ? "@" + b.bot_username : "токен не подключён"))),
      badge(b)),
    alerts,
    h("div", { class: "grid2" },
      h("div", { class: "panel" }, h("h2", {}, "Подписка"),
        h("div", { class: "kv" },
          h("div", {}, h("span", {}, b.trial ? "Пробный период до" : "Действует до"), h("span", {}, b.forever ? "Навсегда" : date(b.expires_at))),
          !b.forever && !b.trial ? h("div", {}, h("span", {}, "Автопродление" + (b.plan_label ? ` (${b.plan_label})` : "")), h("span", { class: "switch" }, autoRenew, h("span", {}))) : null,
          b.group ? h("div", {}, h("span", {}, "Группа"), h("span", {}, b.group.title)) : null),
        !b.forever ? h("a", { class: "btn primary wide", style: "margin-top:14px", href: `#/renew/${b.product}/${b.id}` }, b.trial ? "Выбрать тариф" : "Продлить") : null),
      h("div", { class: "panel" }, h("h2", {}, "Управление"),
        h("div", { class: "actions" },
          b.bot_username ? h("a", { class: "btn", href: `https://t.me/${b.bot_username}`, target: "_blank", rel: "noopener" }, "Открыть бота") : null,
          b.has_token ? h("button", { class: "btn", onclick: (e) => act(e.currentTarget, () => api("POST", `/api/bots/${id}/action`, { action: "restart" }), "Перезапускаю").then(() => setTimeout(route, 1500)) }, "Перезапустить") : null,
          b.has_token ? h("button", { class: "btn", onclick: (e) => act(e.currentTarget, () => api("POST", `/api/bots/${id}/action`, { action: b.paused ? "resume" : "pause" }), b.paused ? "Запускаю" : "Остановлен").then(() => setTimeout(route, 800)) }, b.paused ? "Запустить" : "Остановить") : null,
          b.needs_bind ? h("button", { class: "btn", onclick: (e) => act(e.currentTarget, () => api("POST", `/api/bots/${id}/action`, { action: "bind_code" }), "Новый код создан").then(route) }, "Новый код /bind") : null),
        b.has_token && b.status !== "token_invalid" ? h("details", { style: "margin-top:16px" }, h("summary", { style: "cursor:pointer;color:var(--mist)" }, "Сменить токен"), h("div", { style: "margin-top:12px" }, tokenForm())) : null,
        rt.running ? h("p", { class: "hint", style: "margin-top:14px" }, `Работает ${Math.floor(rt.uptime / 3600)} ч ${Math.floor(rt.uptime % 3600 / 60)} мин · память ${rt.rss_mb} из ${rt.mem_limit_mb} МБ`) : null)),
    settingsPanel,
    h("div", { class: "panel" }, h("h2", {}, "События", h("button", { class: "btn small", onclick: async () => { const r = await api("GET", `/api/bots/${id}`); logsBox.textContent = (r.logs || []).join("\n") || "Событий пока нет."; logsBox.scrollTop = logsBox.scrollHeight; } }, "Обновить")), logsBox),
    h("div", { class: "panel" }, h("h2", {}, "Бэкап"),
      h("p", { class: "muted", style: "margin-bottom:14px" }, "Все данные бота одним архивом: пользователи, настройки, тексты. Мы делаем бэкап каждую ночь, а здесь можно скачать свой."),
      h("div", { class: "actions" },
        h("button", { class: "btn", onclick: async (e) => {
          const res = await act(e.currentTarget, () => api("GET", `/api/bots/${id}/backup`, undefined, true));
          if (!res) return;
          const blob = await res.blob();
          const a = h("a", { href: URL.createObjectURL(blob), download: `backup-${b.bot_username || id}.zip` });
          document.body.append(a); a.click(); a.remove();
        } }, "Скачать бэкап"),
        h("label", { class: "btn" }, "Восстановить из файла", restoreInput))),
    h("div", { class: "panel" }, h("h2", {}, "Удаление"),
      h("p", { class: "muted", style: "margin-bottom:14px" }, "Бот остановится сразу, оставшиеся дни не возвращаются. Данные удаляются через 3 дня."),
      h("button", { class: "btn danger", onclick: async (e) => {
        if (!confirm("Удалить бота?")) return;
        const r = await act(e.currentTarget, () => api("POST", `/api/bots/${id}/action`, { action: "delete" }));
        if (r) { toast("Бот удалён"); location.hash = "#/"; }
      } }, "Удалить бота")),
  ]);
  logsBox.scrollTop = logsBox.scrollHeight;
  // статус меняется сам (запуск, привязка группы) - обновляем тихо
  S.timer = setInterval(async () => {
    if (document.hidden || document.activeElement && ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)) return;
    try {
      const fresh = await api("GET", `/api/bots/${id}`);
      if (fresh.status !== b.status || !!fresh.group !== !!b.group || fresh.has_token !== b.has_token) route();
    } catch (e) { /* молча */ }
  }, 5000);
}

// ---------- кошелёк и рефералы ----------
async function pageWallet() {
  const [w] = await Promise.all([api("GET", "/api/wallet"), loadMe()]);
  const custom = h("input", { type: "text", inputmode: "decimal", placeholder: "Своя сумма, $", "aria-label": "Сумма пополнения" });
  const topup = async (btn, cents) => {
    if (!cents) { toast("Укажите сумму", true); return; }
    const inv = await act(btn, () => api("POST", "/api/topup", { amount: cents }));
    if (inv) watchInvoice(inv, () => route());
  };
  const promoInput = h("input", { type: "text", placeholder: "Промокод", "aria-label": "Промокод", autocomplete: "off" });
  shell([
    head("Кошелёк"),
    h("div", { class: "balance" }, h("div", {}, h("div", { class: "k" }, "Баланс"), h("div", { class: "v" }, moneyBig(w.balance)))),
    h("div", { class: "grid2" },
      h("div", { class: "panel", style: "display:grid;gap:14px" }, h("h2", { style: "margin:0" }, "Пополнить"),
        S.cfg.payments.cryptobot ? [
          h("div", { class: "actions" }, [500, 1000, 2500, 5000, 10000].map((c) => h("button", { class: "btn", onclick: (e) => topup(e.currentTarget, c) }, money(c).replace(".00", "")))),
          h("div", { style: "display:flex;gap:8px" }, custom, h("button", { class: "btn primary", onclick: (e) => topup(e.currentTarget, toCents(custom.value)) }, "Пополнить")),
          h("p", { class: "hint" }, "Оплата в @CryptoBot: USDT, TON, BTC и другие. Зачисление автоматически в течение минуты."),
        ] : h("p", { class: "muted" }, "Пополнение пока недоступно, напишите в поддержку.")),
      h("div", { class: "panel", style: "display:grid;gap:14px" }, h("h2", { style: "margin:0" }, "Промокод на баланс"),
        h("div", { style: "display:flex;gap:8px" }, promoInput, h("button", { class: "btn", onclick: async (e) => {
          const r = await act(e.currentTarget, () => api("POST", "/api/promo/balance", { code: promoInput.value }));
          if (r) { toast(`Зачислено ${money(r.amount)}`); route(); }
        } }, "Активировать")))),
    h("div", { class: "panel flush" }, h("h2", { style: "padding:18px 18px 0" }, "История"),
      w.items.length ? h("div", { class: "scroll" }, h("table", { class: "t" }, h("tbody", {}, w.items.map((o) => h("tr", {},
        h("td", { class: "muted" }, dateTime(o.created_at)), h("td", {}, o.note),
        h("td", { style: `text-align:right;font-weight:600;color:${o.amount > 0 ? "var(--lime)" : "var(--paper)"}` }, (o.amount > 0 ? "+" : "") + money(o.amount)))))))
        : h("div", { class: "empty" }, "Операций пока нет.")),
  ]);
}

async function pageReferrals() {
  const r = await api("GET", "/api/referrals");
  shell([
    head("Рефералы"),
    h("div", { class: "panel", style: "display:grid;gap:14px" },
      h("p", {}, r.percent ? `Приглашайте друзей и получайте ${r.percent}% с каждого их пополнения на свой баланс.` : "Ваша ссылка-приглашение:"),
      h("div", { class: "code", style: "font-size:15px;font-weight:500;overflow:hidden" }, h("span", { style: "overflow:hidden;text-overflow:ellipsis" }, r.link), h("button", { class: "btn small", onclick: () => copy(r.link) }, "Скопировать"))),
    h("div", { class: "tiles" },
      h("div", { class: "tile" }, h("span", { class: "k" }, "Приглашено"), h("span", { class: "v" }, r.count)),
      h("div", { class: "tile" }, h("span", { class: "k" }, "Заработано"), h("span", { class: "v" }, moneyBig(r.earned)))),
  ]);
}

// ---------- админка ----------
async function pageAdmin() {
  const [s, probs] = await Promise.all([api("GET", "/api/admin/stats"), api("GET", "/api/admin/instances?problems=1&limit=20")]);
  const byDay = Object.fromEntries(s.revenue_days.map((d) => [d.day, d.sum]));
  const today = Math.floor(Date.now() / 86400000) * 86400;
  const days = Array.from({ length: 30 }, (_, i) => { const day = today - (29 - i) * 86400; return { day, sum: byDay[day] || 0 }; });
  const max = Math.max(1, ...days.map((d) => d.sum));
  const tile = (k, v, sub) => h("div", { class: "tile" }, h("span", { class: "k" }, k), h("span", { class: "v" }, v), sub ? h("span", { class: "s" }, sub) : null);
  shell([
    head("Обзор"),
    h("div", { class: "tiles" },
      tile("Пополнения сегодня", moneyBig(s.topup_today), `за 30 дней ${money(s.topup_month)}`),
      tile("Продажи за 30 дней", moneyBig(s.sales_month), `на балансах клиентов ${money(s.balances)}`),
      tile("Активные подписки", s.active, `пробных ${s.trials}`),
      tile("Истекают за 3 дня", s.expiring, `без автопродления ${s.expiring_no_renew}`),
      tile("Клиенты", s.users, `сегодня +${s.users_today}`),
      tile("Процессы", s.runtime.running, `память ${s.runtime.rss_mb} МБ`)),
    h("div", { class: "panel" }, h("h2", {}, "Пополнения за 30 дней"),
      s.revenue_days.length ? h("div", { class: "bars", role: "img", "aria-label": "Пополнения по дням за 30 дней" }, days.map((d) => h("div", { style: `height:${d.sum ? Math.max(4, d.sum / max * 100) : 1}%;${d.sum ? "" : "opacity:.25"}`, title: `${date(d.day)}: ${money(d.sum)}` })))
        : h("p", { class: "muted" }, "Пополнений пока не было.")),
    h("div", { class: "panel flush" }, h("h2", { style: "padding:18px 18px 0" }, "Требуют внимания"),
      probs.items.length ? h("div", { class: "scroll" }, h("table", { class: "t" },
        h("thead", {}, h("tr", {}, h("th", {}, "Бот"), h("th", {}, "Клиент"), h("th", {}, "Статус"), h("th", {}, ""))),
        h("tbody", {}, probs.items.map((b) => h("tr", {},
          h("td", {}, h("a", { href: `#/bot/${b.id}` }, `${b.title} ${b.bot_username ? "@" + b.bot_username : "#" + b.id}`)),
          h("td", {}, h("a", { href: `#/admin/user/${b.user_id}` }, b.owner_username ? "@" + b.owner_username : b.user_id)),
          h("td", {}, badge(b), b.status_note ? h("div", { class: "hint" }, b.status_note) : null),
          h("td", { style: "text-align:right" }, h("button", { class: "btn small", onclick: (e) => act(e.currentTarget, () => api("POST", `/api/admin/instances/${b.id}`, { action: "restart" }), "Перезапускаю") }, "Перезапустить")))))))
        : h("div", { class: "empty" }, "Всё работает.")),
  ]);
}

async function pageAdminInstances() {
  const q = h("input", { type: "search", placeholder: "@бот, ID клиента или копии", "aria-label": "Поиск" });
  const status = h("select", { "aria-label": "Статус" }, [["", "Все статусы"], ["running", "Работает"], ["waiting_bind", "Ждёт группу"], ["setup", "Нужен токен"], ["crashed", "Сбой"], ["token_invalid", "Токен недействителен"], ["expired", "Истекла"], ["stopped", "Остановлен"], ["frozen", "Заморожен"]].map(([v, l]) => h("option", { value: v }, l)));
  const body = h("tbody");
  async function load() {
    const r = await api("GET", `/api/admin/instances?q=${encodeURIComponent(q.value)}&status=${status.value}&limit=200`);
    body.replaceChildren(...r.items.map((b) => h("tr", {},
      h("td", {}, h("a", { href: `#/bot/${b.id}` }, `#${b.id} ${b.title}`), h("div", { class: "hint" }, b.bot_username ? "@" + b.bot_username : "без токена")),
      h("td", {}, h("a", { href: `#/admin/user/${b.user_id}` }, b.owner_username ? "@" + b.owner_username : b.user_id)),
      h("td", {}, badge(b)),
      h("td", {}, b.forever ? "навсегда" : date(b.expires_at)),
      h("td", { style: "text-align:right;white-space:nowrap" },
        h("button", { class: "btn small", onclick: async (e) => { const d = prompt("Подарить дней:", "7"); if (d) { await act(e.currentTarget, () => api("POST", `/api/admin/instances/${b.id}`, { action: "gift", days: +d }), "Готово"); load(); } } }, "+ дни"), " ",
        h("button", { class: "btn small", onclick: async (e) => { await act(e.currentTarget, () => api("POST", `/api/admin/instances/${b.id}`, { action: b.frozen ? "unfreeze" : "freeze" }), "Готово"); load(); } }, b.frozen ? "Разморозить" : "Заморозить")))));
    if (!r.items.length) body.replaceChildren(h("tr", {}, h("td", { colspan: 5, class: "muted" }, "Ничего не найдено")));
  }
  q.addEventListener("input", () => { clearTimeout(q._t); q._t = setTimeout(load, 300); });
  status.addEventListener("change", load);
  shell([head("Копии ботов"), h("div", { style: "display:flex;gap:8px;flex-wrap:wrap" }, h("div", { style: "flex:1 1 260px" }, q), h("div", { style: "flex:0 1 220px" }, status)),
    h("div", { class: "panel flush scroll" }, h("table", { class: "t" }, h("thead", {}, h("tr", {}, h("th", {}, "Бот"), h("th", {}, "Клиент"), h("th", {}, "Статус"), h("th", {}, "До"), h("th", {}, ""))), body))]);
  load();
}

async function pageAdminUsers() {
  const q = h("input", { type: "search", placeholder: "ID, @username или имя", "aria-label": "Поиск клиента" });
  const body = h("tbody");
  async function load() {
    const r = await api("GET", `/api/admin/users?q=${encodeURIComponent(q.value)}`);
    body.replaceChildren(...r.items.map((u) => h("tr", {},
      h("td", {}, h("a", { href: `#/admin/user/${u.id}` }, u.first_name || u.id), h("div", { class: "hint" }, (u.username ? "@" + u.username + " · " : "") + u.id)),
      h("td", {}, money(u.balance)), h("td", {}, u.bots), h("td", { class: "muted" }, date(u.created_at)),
      h("td", {}, u.banned ? h("span", { class: "badge bad" }, "Бан") : null))));
  }
  q.addEventListener("input", () => { clearTimeout(q._t); q._t = setTimeout(load, 300); });
  shell([head("Клиенты"), q, h("div", { class: "panel flush scroll" }, h("table", { class: "t" }, h("thead", {}, h("tr", {}, h("th", {}, "Клиент"), h("th", {}, "Баланс"), h("th", {}, "Ботов"), h("th", {}, "С нами с"), h("th", {}, ""))), body))]);
  load();
}

async function pageAdminUser(id) {
  const r = await api("GET", `/api/admin/users/${id}`);
  const u = r.user;
  const amount = h("input", { type: "text", inputmode: "decimal", placeholder: "Сумма, $ (минус - списать)", "aria-label": "Сумма" });
  const note = h("input", { type: "text", placeholder: "Комментарий", "aria-label": "Комментарий" });
  shell([
    back("#/admin/users", "Клиенты"),
    head(u.first_name || String(u.id), h("button", { class: u.banned ? "btn" : "btn danger", onclick: async (e) => {
      if (!u.banned && !confirm("Забанить клиента? Его боты будут заморожены.")) return;
      await act(e.currentTarget, () => api("POST", `/api/admin/users/${id}`, { action: u.banned ? "unban" : "ban" }), "Готово"); route();
    } }, u.banned ? "Разбанить" : "Забанить")),
    h("div", { class: "grid2" },
      h("div", { class: "panel" }, h("div", { class: "kv" },
        h("div", {}, h("span", {}, "Telegram ID"), h("span", {}, u.id)),
        h("div", {}, h("span", {}, "Юзернейм"), h("span", {}, u.username ? "@" + u.username : "—")),
        h("div", {}, h("span", {}, "Баланс"), h("b", {}, money(u.balance))),
        h("div", {}, h("span", {}, "Пригласил"), h("span", {}, u.ref_by || "—")),
        h("div", {}, h("span", {}, "С нами с"), h("span", {}, date(u.created_at))))),
      h("div", { class: "panel", style: "display:grid;gap:10px" }, h("h2", { style: "margin:0" }, "Изменить баланс"), amount, note,
        h("button", { class: "btn primary", onclick: async (e) => {
          const raw = amount.value.trim(); const neg = raw.startsWith("-"); const c = toCents(raw.replace("-", ""));
          if (!c) { toast("Укажите сумму", true); return; }
          const res = await act(e.currentTarget, () => api("POST", `/api/admin/users/${id}`, { action: "balance", amount: neg ? -c : c, note: note.value }));
          if (res) { toast(`Баланс: ${money(res.balance)}`); route(); }
        } }, "Применить"))),
    h("div", { class: "panel flush" }, h("h2", { style: "padding:18px 18px 0" }, "Боты"),
      r.bots.length ? h("div", { class: "list" }, r.bots.map((b) => h("a", { class: "row", href: `#/bot/${b.id}` }, avatar(b), h("span", { class: "grow" }, h("span", { class: "title" }, `${b.title} ${b.bot_username ? "@" + b.bot_username : ""}`), h("span", { class: "meta" }, b.forever ? "навсегда" : "до " + date(b.expires_at))), badge(b))))
        : h("div", { class: "empty" }, "Ботов нет.")),
    h("div", { class: "panel flush scroll" }, h("h2", { style: "padding:18px 18px 0" }, "Операции"),
      h("table", { class: "t" }, h("tbody", {}, r.ledger.map((o) => h("tr", {}, h("td", { class: "muted" }, dateTime(o.created_at)), h("td", {}, o.note), h("td", { style: "text-align:right" }, money(o.amount)), h("td", { class: "muted", style: "text-align:right" }, money(o.balance_after))))))),
  ]);
}

async function pageAdminCatalog() {
  const r = await api("GET", "/api/admin/catalog");
  shell([head("Тарифы"),
    h("p", { class: "muted" }, "Тариф без цены не продаётся. Бот появляется в каталоге, когда включён и у него есть хотя бы один тариф с ценой."),
    r.items.map((p) => {
      const enabled = h("input", { type: "checkbox" }); enabled.checked = p.enabled;
      const trial = h("input", { type: "number", min: "0", max: "30", value: p.trial_days, "aria-label": "Дней пробного периода", style: "max-width:110px" });
      const rows = p.plans.map((pl) => {
        const price = h("input", { type: "text", inputmode: "decimal", value: pl.price == null ? "" : (pl.price / 100).toFixed(2), placeholder: "не продаётся", "aria-label": `Цена ${pl.label}` });
        const on = h("input", { type: "checkbox" }); on.checked = pl.enabled;
        return { pl, price, on, el: h("tr", {}, h("td", {}, pl.label), h("td", {}, price), h("td", {}, h("span", { class: "switch" }, on, h("span", {})))) };
      });
      const newDays = h("input", { type: "number", min: "1", placeholder: "Новый срок, дней", "aria-label": "Новый срок в днях" });
      const save = h("button", { class: "btn primary" }, "Сохранить");
      save.onclick = async () => {
        const plans = rows.map(({ pl, price, on }) => ({ days: pl.days, price: price.value.trim() === "" ? null : Math.round(parseFloat(price.value.replace(",", ".")) * 100), enabled: on.checked }));
        if (newDays.value) plans.push({ days: +newDays.value, price: null, enabled: true });
        if (plans.some((x) => x.price != null && !Number.isFinite(x.price))) { toast("Проверьте цены", true); return; }
        const ok = await act(save, () => api("POST", `/api/admin/products/${p.slug}`, { enabled: enabled.checked, trial_days: +trial.value, plans }), "Сохранено");
        if (ok && newDays.value) route();
      };
      return h("div", { class: "panel", style: "display:grid;gap:14px" },
        h("h2", { style: "margin:0" }, h("span", { style: "display:flex;gap:10px;align-items:center" }, h("span", { class: "avatar", style: `background:${p.color};width:32px;height:32px;font-size:14px;border-radius:10px` }, p.title[0]), p.title),
          h("label", { style: "display:flex;gap:10px;align-items:center;font-weight:400;font-size:14px;color:var(--mist)" }, "В продаже", h("span", { class: "switch" }, enabled, h("span", {})))),
        h("label", { style: "display:flex;gap:12px;align-items:center;color:var(--mist)" }, "Пробный период, дней", trial),
        h("div", { class: "scroll" }, h("table", { class: "t" }, h("thead", {}, h("tr", {}, h("th", {}, "Срок"), h("th", {}, "Цена, $"), h("th", {}, "Вкл"))), h("tbody", {}, rows.map((x) => x.el)))),
        h("div", { style: "display:flex;gap:8px;flex-wrap:wrap" }, h("div", { style: "flex:0 1 220px" }, newDays), save));
    })]);
}

async function pageAdminPromos() {
  const [r, cat] = await Promise.all([api("GET", "/api/admin/promos"), api("GET", "/api/admin/catalog")]);
  const code = h("input", { type: "text", placeholder: "Код, например START", "aria-label": "Код" });
  const kind = h("select", { "aria-label": "Тип" }, h("option", { value: "percent" }, "Скидка, %"), h("option", { value: "amount" }, "Скидка, $"), h("option", { value: "balance" }, "На баланс, $"));
  const value = h("input", { type: "text", inputmode: "decimal", placeholder: "Значение", "aria-label": "Значение" });
  const product = h("select", { "aria-label": "Бот" }, h("option", { value: "" }, "На все боты"), cat.items.map((p) => h("option", { value: p.slug }, p.title)));
  const uses = h("input", { type: "number", min: "0", placeholder: "Лимит (0 - без лимита)", "aria-label": "Лимит использований" });
  const until = h("input", { type: "date", "aria-label": "Действует до" });
  const create = h("button", { class: "btn primary" }, "Создать");
  create.onclick = async () => {
    const v = kind.value === "percent" ? parseInt(value.value, 10) : toCents(value.value);
    const ok = await act(create, () => api("POST", "/api/admin/promos", { code: code.value, kind: kind.value, value: v, product: product.value || null, max_uses: +uses.value || 0, expires_at: until.value ? Math.floor(new Date(until.value + "T23:59:59").getTime() / 1000) : null }), "Промокод создан");
    if (ok) route();
  };
  const kindText = (p) => (p.kind === "percent" ? `−${p.value}%` : p.kind === "amount" ? `−${money(p.value)}` : `+${money(p.value)} на баланс`);
  shell([head("Промокоды"),
    h("div", { class: "panel", style: "display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(180px,1fr))" }, code, kind, value, product, uses, until, create),
    h("div", { class: "panel flush scroll" }, r.items.length ? h("table", { class: "t" },
      h("thead", {}, h("tr", {}, h("th", {}, "Код"), h("th", {}, "Что даёт"), h("th", {}, "Использован"), h("th", {}, "До"), h("th", {}, ""))),
      h("tbody", {}, r.items.map((p) => h("tr", {}, h("td", {}, h("b", {}, p.code), p.product ? h("div", { class: "hint" }, p.product) : null), h("td", {}, kindText(p)),
        h("td", {}, `${p.used}${p.max_uses ? " из " + p.max_uses : ""}`), h("td", {}, p.expires_at ? date(p.expires_at) : "бессрочно"),
        h("td", { style: "text-align:right" }, h("button", { class: "btn small danger", onclick: async (e) => { if (confirm(`Удалить ${p.code}?`)) { await act(e.currentTarget, () => api("DELETE", `/api/admin/promos/${p.code}`), "Удалён"); route(); } } }, "Удалить"))))))
      : h("div", { class: "empty" }, "Промокодов пока нет.")),
  ]);
}

async function pageAdminPayments() {
  const r = await api("GET", "/api/admin/payments");
  const st = { paid: ["ok", "Оплачен"], active: ["warn", "Ждёт оплаты"], expired: ["idle", "Истёк"] };
  shell([head("Платежи"), h("div", { class: "panel flush scroll" }, r.items.length ? h("table", { class: "t" },
    h("thead", {}, h("tr", {}, h("th", {}, "№"), h("th", {}, "Клиент"), h("th", {}, "Сумма"), h("th", {}, "Статус"), h("th", {}, "Создан"), h("th", {}, "Оплачен"))),
    h("tbody", {}, r.items.map((i) => h("tr", {}, h("td", {}, i.id), h("td", {}, h("a", { href: `#/admin/user/${i.user_id}` }, i.username ? "@" + i.username : i.user_id)), h("td", {}, money(i.amount)),
      h("td", {}, h("span", { class: "badge " + (st[i.status] || ["idle"])[0] }, (st[i.status] || [0, i.status])[1])), h("td", { class: "muted" }, dateTime(i.created_at)), h("td", { class: "muted" }, dateTime(i.paid_at))))))
    : h("div", { class: "empty" }, "Платежей пока нет."))]);
}

async function pageAdminSettings() {
  const [s, b] = await Promise.all([api("GET", "/api/admin/settings"), api("GET", "/api/admin/backups")]);
  const support = h("input", { type: "text", value: s.support_url, placeholder: "https://t.me/ваш_аккаунт" });
  const ref = h("input", { type: "number", min: "0", max: "50", value: s.referral_percent });
  const toTg = h("input", { type: "checkbox" }); toTg.checked = s.backup_to_telegram;
  const widget = h("input", { type: "checkbox" }); widget.checked = s.login_widget;
  const save = h("button", { class: "btn primary" }, "Сохранить");
  save.onclick = () => act(save, () => api("POST", "/api/admin/settings", { support_url: support.value.trim(), referral_percent: +ref.value || 0, backup_to_telegram: toTg.checked, login_widget: widget.checked }), "Сохранено");
  const text = h("textarea", { placeholder: "Текст рассылки всем клиентам хаб-бота. Можно HTML: <b>жирный</b>, <a href=\"…\">ссылка</a>", "aria-label": "Текст рассылки" });
  const send = h("button", { class: "btn" }, "Отправить всем");
  send.onclick = async () => { if (!text.value.trim() || !confirm("Отправить всем клиентам?")) return; const r = await act(send, () => api("POST", "/api/admin/broadcast", { text: text.value })); if (r) { toast(`В очереди: ${r.queued}`); text.value = ""; } };
  const backupNow = h("button", { class: "btn" }, "Сделать бэкап сейчас");
  backupNow.onclick = async () => { const r = await act(backupNow, () => api("POST", "/api/admin/backups"), "Бэкап готов"); if (r) route(); };
  const sw = (inp, label, hint) => h("label", { style: "display:flex;gap:14px;align-items:center;justify-content:space-between" }, h("span", { style: "display:grid;gap:2px" }, h("span", {}, label), hint ? h("span", { class: "hint" }, hint) : null), h("span", { class: "switch" }, inp, h("span", {})));
  shell([head("Настройки"),
    h("div", { class: "grid2" },
      h("div", { class: "panel", style: "display:grid;gap:14px" }, h("h2", { style: "margin:0" }, "Платформа"),
        h("label", { class: "field" }, h("span", {}, "Ссылка на поддержку"), support),
        h("label", { class: "field" }, h("span", {}, "Реферальный процент"), ref, h("span", {}, "С каждого пополнения приглашённого. 0 - выключено.")),
        sw(toTg, "Присылать ночной бэкап в Telegram", "Если архив меньше 45 МБ"),
        sw(widget, "Вход на сайте через Telegram-виджет", "Нужен домен в @BotFather → /setdomain"),
        h("div", {}, save)),
      h("div", { class: "panel", style: "display:grid;gap:12px" }, h("h2", { style: "margin:0" }, "Рассылка клиентам"), text, h("div", {}, send))),
    h("div", { class: "panel flush" }, h("h2", { style: "padding:18px 18px 0" }, "Бэкапы", h("span", {}, backupNow)),
      b.items.length ? h("div", { class: "scroll" }, h("table", { class: "t" }, h("tbody", {}, b.items.map((x) => h("tr", {},
        h("td", {}, x.name), h("td", { class: "muted" }, (x.size / 1e6).toFixed(1) + " МБ"), h("td", { class: "muted" }, dateTime(x.created_at)),
        h("td", { style: "text-align:right" }, h("button", { class: "btn small", onclick: async (e) => {
          const res = await act(e.currentTarget, () => api("GET", `/api/admin/backups/${x.name}`, undefined, true));
          if (!res) return;
          const a = h("a", { href: URL.createObjectURL(await res.blob()), download: x.name }); document.body.append(a); a.click(); a.remove();
        } }, "Скачать")))))))
        : h("div", { class: "empty" }, "Бэкапов пока нет. Первый появится ночью или по кнопке."),
      h("p", { class: "hint", style: "padding:0 18px 18px" }, "Восстановление всей платформы: остановите её и выполните python -m baksi restore <файл> (подробно в README).")),
  ]);
}

// ---------- маршруты ----------
async function route() {
  clearInterval(S.timer);
  const hash = location.hash || "#/";
  const parts = hash.slice(2).split("/");
  try {
    if (hash === "#/expired") { renderLogin(true); return; }
    if (!S.me) await loadMe();
    const admin = S.me.is_owner;
    if (parts[0] === "") await pageHome();
    else if (parts[0] === "catalog") await pageCatalog();
    else if (parts[0] === "product") await pageProduct(parts[1]);
    else if (parts[0] === "renew") await pageProduct(parts[1], +parts[2]);
    else if (parts[0] === "bot") await pageBot(+parts[1]);
    else if (parts[0] === "wallet") await pageWallet();
    else if (parts[0] === "referrals") await pageReferrals();
    else if (parts[0] === "admin" && admin) {
      const sub = parts[1] || "";
      await ({ "": pageAdmin, instances: pageAdminInstances, users: pageAdminUsers, user: () => pageAdminUser(+parts[2]), catalog: pageAdminCatalog, promos: pageAdminPromos, payments: pageAdminPayments, settings: pageAdminSettings }[sub] || pageAdmin)();
    } else { location.hash = "#/"; }
  } catch (e) {
    if (e.status === 401) return;
    if (!S.me) { renderLogin(); return; }
    shell([h("div", { class: "panel empty" }, h("p", {}, e.message || "Что-то пошло не так"), h("button", { class: "btn", onclick: route }, "Повторить"))]);
  }
}

async function boot() {
  try { S.cfg = await (await fetch("/api/config")).json(); } catch (e) { S.cfg = { brand: "Baksi Cloud", payments: {} }; }
  document.title = `Кабинет · ${S.cfg.brand}`;
  if (TG) {
    TG.ready(); TG.expand();
    try { TG.setHeaderColor("#000000"); TG.setBackgroundColor("#000000"); } catch (e) { /* старые клиенты */ }
    try {
      const r = await api("POST", "/api/auth/webapp", { init_data: TG.initData });
      S.token = r.token; sessionStorage.setItem("bc_token", r.token);
    } catch (e) { toast(e.message, true); }
  }
  window.addEventListener("hashchange", route);
  try { await loadMe(); } catch (e) { if (e.status === 401 || location.hash === "#/expired") { renderLogin(location.hash === "#/expired"); return; } }
  route();
}
boot();
