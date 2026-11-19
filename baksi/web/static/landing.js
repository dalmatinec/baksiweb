// Сайт: подставляет бренд, ссылку на хаб-бота и каталог с реальными ценами.
(function () {
  // Открыли из Telegram (кнопка бота, Mini App): сразу в кабинет, вход там автоматический
  if (/tgWebAppData|tgWebAppVersion/.test(location.hash + location.search)) {
    location.replace("/app" + location.search + location.hash);
    return;
  }
  // Уже вошли: вместо «Войти» и «Регистрация» - одна кнопка в кабинет
  fetch("/api/me", { credentials: "same-origin", headers: { "X-Baksi": "1" } }).then((r) => {
    if (!r.ok) return;
    const box = document.querySelector(".site-actions");
    if (!box) return;
    box.querySelectorAll("a[href^='/app']").forEach((a) => a.remove());
    const go = document.createElement("a");
    go.className = "btn primary small";
    go.href = "/app";
    go.textContent = "Мой кабинет";
    box.prepend(go);
  }).catch(() => {});
  const money = (c) => c == null ? "" : "$" + (c / 100).toFixed(2).replace(/\.00$/, "");
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));

  Promise.all([fetch("/api/config").then((r) => r.json()), fetch("/api/catalog").then((r) => r.json())])
    .then(([cfg, cat]) => {
      document.querySelectorAll("[data-brand]").forEach((el) => (el.textContent = cfg.brand));
      document.title = `${cfg.brand}: свои Telegram-боты по подписке`;
      const hub = cfg.hub ? `https://t.me/${cfg.hub}` : "/app";
      document.querySelectorAll("[data-hub]").forEach((el) => (el.href = hub));
      if (cfg.notice) {  // например, после переключения на запасной хаб-бот
        const bar = document.createElement("div");
        bar.className = "site-notice";
        bar.setAttribute("role", "status");
        bar.textContent = cfg.notice;
        document.body.prepend(bar);
      }
      if (cfg.support_url) {
        const s = document.querySelector("[data-support]");
        s.href = cfg.support_url;
        s.hidden = false;
      }
      const box = document.getElementById("bot-rows");
      if (!cat.items.length) {
        box.innerHTML = '<p class="muted" style="padding:28px 0">Каталог скоро откроется.</p>';
        return;
      }
      box.innerHTML = cat.items.map((p) => `
        <article class="bot-row">
          <div>
            <h3><span class="dot" style="background:${esc(p.color)}"></span>${esc(p.title)}</h3>
            <div class="tag">${esc(p.tagline)}</div>
          </div>
          <div>
            <p>${esc(p.description)}</p>
            <ul>${p.features.slice(0, 4).map((f) => `<li>${esc(f)}</li>`).join("")}</ul>
          </div>
          <div class="price">
            <b>${p.month_price ? money(p.month_price) + " / месяц" : "от " + money(p.min_price)}</b>
            ${p.trial_days ? `<span class="muted">${p.trial_days} дн. бесплатно</span>` : ""}
            <a class="btn small" href="${cfg.hub ? `https://t.me/${cfg.hub}?start=b${esc(p.slug)}` : "/app#/catalog"}">Подключить</a>
          </div>
        </article>`).join("");
    })
    .catch(() => {
      document.getElementById("bot-rows").innerHTML = '<p class="muted" style="padding:28px 0">Не удалось загрузить каталог. Обновите страницу.</p>';
    });
})();
