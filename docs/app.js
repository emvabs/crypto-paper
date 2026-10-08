// Dashboard: reads the state.json each run commits. Only the Trade section
// (trade.js) calls OKX, for a live price when you pick a coin.
"use strict";

const STALE_MS = 60 * 60 * 1000;
const REFRESH_MS = 5 * 60 * 1000;

// Where state.json lives: ?state=<url> wins; on GitHub Pages it is read from
// the repository (raw.githubusercontent.com allows cross-origin reads), so the
// page shows each run's commit without a Pages rebuild; locally, ../data/.
function stateUrl() {
  const q = new URLSearchParams(location.search).get("state");
  if (q) return q;
  if (location.hostname.endsWith(".github.io")) {
    const owner = location.hostname.split(".")[0];
    const repo = location.pathname.split("/").filter(Boolean)[0];
    return `https://raw.githubusercontent.com/${owner}/${repo}/main/data/state.json`;
  }
  return "../data/state.json";
}

// ------------------------------------------------------------ formatting

const eurFmt = new Intl.NumberFormat("en-IE", { style: "currency", currency: "EUR" });
const eur = (x) => (x == null ? "–" : eurFmt.format(x));
const eurSigned = (x) => (x == null ? "–" : (x > 0 ? "+" : x < 0 ? "−" : "") + eurFmt.format(Math.abs(x)));
const pct = (x, d = 1) => (x == null ? "–" : `${(x * 100).toFixed(d)}%`);
const pctSigned = (x, d = 1) => (x == null ? "–" : `${x > 0 ? "+" : x < 0 ? "−" : ""}${Math.abs(x * 100).toFixed(d)}%`);
function price(x) {
  if (x == null) return "–";
  if (x >= 100) return x.toLocaleString("en-IE", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return Number(x.toPrecision(6)).toLocaleString("en-IE", { maximumSignificantDigits: 6 });
}
const compact = new Intl.NumberFormat("en-IE", { notation: "compact", maximumFractionDigits: 1 });
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function ago(iso) {
  const ms = Date.now() - Date.parse(iso);
  const m = Math.round(ms / 60000);
  if (m < 1) return "just now";
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 48) return `${h} h ago`;
  return `${Math.round(h / 24)} days ago`;
}
const localTime = (iso) => new Date(iso).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

// ------------------------------------------------------------ icons

const ICONS = {
  check: '<path d="M3.5 8.5l3 3 6-7"/>',
  alert: '<path d="M8 2.5l6 11H2z"/><path d="M8 7v3"/><path d="M8 12h.01"/>',
  pause: '<path d="M6 3.5v9"/><path d="M10 3.5v9"/>',
  up: '<path d="M8 13V3"/><path d="M4 7l4-4 4 4"/>',
  out: '<circle cx="8" cy="8" r="5.5"/><path d="M8 5v3.5"/><path d="M8 11h.01"/>',
  dot: '<circle cx="8" cy="8" r="3" fill="currentColor"/>',
};
const icon = (name) =>
  `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name]}</svg>`;
const status = (kind, iconName, label) => `<span class="status ${kind}">${icon(iconName)}${esc(label)}</span>`;

// ------------------------------------------------------------ rendering

const $ = (id) => document.getElementById(id);

function renderHeader(s) {
  const stale = Date.now() - Date.parse(s.updated_at) > STALE_MS;
  $("updated").textContent = `Updated ${ago(s.updated_at)} · ${localTime(s.updated_at)}`;
  const banners = [];
  if (stale) {
    banners.push(`<div class="banner critical">${status("critical", "alert", "")}<div><strong>Data is more than an hour old.</strong> The scheduled run may be delayed or switched off — check the Actions tab.</div></div>`);
  }
  if (s.core_stale) {
    banners.push(`<div class="banner">${status("warning", "alert", "")}<div>Core prices failed this run; core figures are from an earlier run.</div></div>`);
  }
  if (s.problems && s.problems.length) {
    banners.push(`<div class="banner">${status("warning", "alert", "")}<div><strong>Data problems this run</strong><ul>${s.problems.map((p) => `<li>${esc(p)}</li>`).join("")}</ul></div></div>`);
  }
  if (s.pending_alerts) {
    banners.push(`<div class="banner">${status("warning", "alert", "")}<div>${s.pending_alerts} alert(s) failed to send to Telegram and will be retried.</div></div>`);
  }
  $("banners").innerHTML = banners.join("");
}

function renderHero(s) {
  const core = s.core ? s.core.total_eur : 0;
  const sat = s.satellite;
  const satValue = sat.balance_eur + (sat.unrealized_eur || 0);
  $("hero-value").textContent = eur(core + satValue);
  $("hero-sub").textContent = `Core ${eur(core)} · Satellite ${eur(satValue)} incl. open trades`;

  const open = sat.open_trades.length;
  const max = 3;
  const bagStatus = {
    ok: status("good", "check", "Within limits"),
    pause: status("critical", "pause", "Below floor — paused"),
    sweep: status("warning", "up", "Above sweep level"),
  }[sat.status];
  const tiles = [
    ["Satellite balance", eur(sat.balance_eur), bagStatus],
    ["Open trades", `${open} of ${max}`, open ? `Unrealized ${eurSigned(sat.unrealized_eur)}` : "No open trades"],
    ["Win rate", sat.win_rate == null ? "–" : pct(sat.win_rate, 0), `${sat.closed_trades} closed trade${sat.closed_trades === 1 ? "" : "s"}`],
    ["Realized P&L", eurSigned(sat.realized_eur), `Floor ${eur(sat.floor_eur)} · sweep above ${eur(sat.sweep_above_eur)}`],
  ];
  $("tiles").innerHTML = tiles
    .map(([l, v, sub]) => `<div class="tile"><p class="label">${l}</p><p class="value">${v}</p><p class="sub">${sub}</p></div>`)
    .join("");
}

function renderCore(s) {
  const core = s.core;
  if (!core) {
    $("core").innerHTML = '<p class="empty">No core prices yet.</p>';
    return;
  }
  if (!core.total_eur) {
    $("core-note").textContent = "";
    $("core").innerHTML = '<p class="empty">No holdings yet. Buy from the Trade section, or add quantities to <code>data/core_holdings.json</code>.</p>';
    return;
  }
  $("core-note").textContent = core.rebalance_due ? `Quarterly rebalance due (${core.rebalance_due})` : `Value ${eur(core.total_eur)}`;
  // one scale for every asset, so strips compare
  const top = Math.max(0.6, ...core.assets.map((a) => Math.max(a.weight, a.high) + 0.05));
  const x = (w) => `${(Math.min(w, top) / top) * 100}%`;
  const ticks = [0, 0.25, 0.5, 0.75, 1].filter((t) => t <= top + 1e-9);

  $("core").innerHTML = core.assets
    .map((a) => {
      const action = Math.abs(a.rebalance_eur) < 0.01 ? "On target"
        : `${a.rebalance_eur > 0 ? "Buy" : "Sell"} ${eur(Math.abs(a.rebalance_eur))} to reach target`;
      const state = a.out_of_band
        ? status("serious", "out", `Outside band · ${action.toLowerCase()}`)
        : status("good", "check", `In band · drift ${pctSigned(a.weight - a.target)}`);
      const tip = `${a.asset} ${pct(a.weight)}\nTarget ${pct(a.target, 0)} · band ${pct(a.low, 0)}–${pct(a.high, 0)}\n${action}`;
      return `
        <div class="asset">
          <div class="asset-head">
            <span class="asset-name">${esc(a.asset)} <span class="num">${pct(a.weight)}</span></span>
            <span class="asset-meta num">${eur(a.value_eur)} · ${price(a.quantity)} @ ${eur(a.price_eur)}</span>
          </div>
          <div class="strip" aria-hidden="true">
            <div class="track"></div>
            <div class="band" style="left:${x(a.low)};width:calc(${x(a.high)} - ${x(a.low)})"></div>
            <div class="target" style="left:${x(a.target)}"></div>
            <div class="dot${a.out_of_band ? " out" : ""}" style="left:${x(a.weight)}"></div>
            <div class="hit" style="left:${x(a.weight)}" data-tip="${esc(tip)}"></div>
          </div>
          <div class="scale" aria-hidden="true">${ticks.map((t) => `<span style="left:${x(t)}">${pct(t, 0)}</span>`).join("")}</div>
          <div class="asset-foot"><span>${state}</span><span>Target ${pct(a.target, 0)} · band ${pct(a.low, 0)}–${pct(a.high, 0)}</span></div>
        </div>`;
    })
    .join("");
}

function ladder(t) {
  const levels = [
    { key: "stop", label: "Stop", v: t.stop, done: false },
    { key: "entry", label: "Entry", v: t.entry_price, done: false },
    { key: "t1", label: "T1", v: t.target1, done: t.thirds_sold >= 1 },
    { key: "t2", label: "T2", v: t.target2, done: t.thirds_sold >= 2 },
  ];
  const trailActive = t.thirds_sold >= 2;
  const vals = [t.stop, t.target2, t.price ?? t.entry_price, t.trailing_high];
  const lo = Math.min(...vals) * 0.985;
  const hi = Math.max(...vals) * 1.015;
  const x = (v) => `${((v - lo) / (hi - lo)) * 100}%`;
  const p = t.price;
  const reachedFrom = Math.min(t.entry_price, p ?? t.entry_price);
  const reachedTo = Math.max(t.entry_price, p ?? t.entry_price);

  let html = `<div class="ladder" aria-hidden="true">
    <div class="track"></div>
    <div class="reached" style="left:${x(reachedFrom)};width:calc(${x(reachedTo)} - ${x(reachedFrom)})"></div>`;
  for (const l of levels) {
    html += `<div class="lvl" style="left:${x(l.v)}"></div>
      <div class="lvl-label${l.done ? " done" : ""}" style="left:${x(l.v)}">${l.label}<br><span class="num">${price(l.v)}</span></div>`;
  }
  if (trailActive) {
    html += `<div class="lvl trail" style="left:${x(t.trailing_level)}"></div>
      <div class="lvl-label trail" style="left:${x(t.trailing_level)}">Trail<br><span class="num">${price(t.trailing_level)}</span></div>`;
  }
  if (p != null) {
    const tip = `Now ${price(p)} (${pctSigned(p / t.entry_price - 1)} vs entry)\nStop ${price(t.stop)} · T1 ${price(t.target1)} · T2 ${price(t.target2)}` +
      (trailActive ? `\nTrailing stop ${price(t.trailing_level)} (high ${price(t.trailing_high)})` : "");
    html += `<div class="price-label num" style="left:${x(p)}">${price(p)}</div>
      <div class="dot" style="left:${x(p)}"></div>
      <div class="hit" style="left:${x(p)}" data-tip="${esc(tip)}"></div>`;
  }
  return html + "</div>";
}

function renderSatellite(s) {
  const sat = s.satellite;
  const block = s.daily && s.daily.entry_block;
  $("sat-note").innerHTML = block
    ? status("warning", "pause", `New entries paused: ${block}`)
    : status("good", "check", "New entries allowed");
  if (!sat.open_trades.length) {
    $("trades").innerHTML = '<div class="card"><p class="empty">No open paper trades. Buy from the Trade section, or add a row to <code>data/paper_trades.csv</code>, when you take a breakout.</p></div>';
    return;
  }
  $("trades").innerHTML = `<div class="trades">${sat.open_trades
    .map((t) => {
      const total = (t.unrealized_eur ?? 0) + t.realized_eur;
      const cls = total > 0 ? "up" : total < 0 ? "down" : "";
      const move = t.price != null ? pctSigned(t.price / t.entry_price - 1) : "–";
      return `<article class="card">
        <div class="trade-head">
          <span class="pair">${esc(t.pair)}</span>
          <span class="pnl num ${cls}">${eurSigned(total)}</span>
        </div>
        <p class="trade-sub">Trade ${esc(t.id)} · opened ${esc(t.date_opened)} · ${eur(t.size_eur)} · ${t.thirds_sold} of 3 thirds sold</p>
        ${ladder(t)}
        <dl class="facts num">
          <div><dt>Entry</dt><dd>${price(t.entry_price)}</dd></div>
          <div><dt>Now</dt><dd>${price(t.price)} <span class="note">${move}</span></dd></div>
          <div><dt>Stop</dt><dd>${price(t.stop)}</dd></div>
          <div><dt>Realized</dt><dd>${eurSigned(t.realized_eur)}</dd></div>
          <div><dt>Unrealized</dt><dd>${eurSigned(t.unrealized_eur)}</dd></div>
          <div><dt>High since entry</dt><dd>${price(t.trailing_high)}</dd></div>
        </dl>
      </article>`;
    })
    .join("")}</div>`;
}

// Level labels sit under the ladder in rows: each label takes the first row
// where it clears the previous one, and is kept inside the card's edges.
const LABEL_ROW = 30;
const LABEL_TOP = 40;
function layoutLadders() {
  for (const lad of document.querySelectorAll(".ladder")) {
    const width = lad.clientWidth;
    const labels = [...lad.querySelectorAll(".lvl-label")];
    labels.forEach((el) => { el.style.transform = ""; el.style.top = ""; });
    const placed = labels
      .map((el) => ({ el, center: el.offsetLeft, w: el.offsetWidth }))
      .sort((a, b) => a.center - b.center);
    const rowEnds = [];
    for (const p of placed) {
      let left = Math.min(Math.max(p.center - p.w / 2, -4), width - p.w + 4);
      let row = rowEnds.findIndex((end) => left >= end + 6);
      if (row === -1) { row = rowEnds.length; rowEnds.push(0); }
      rowEnds[row] = left + p.w;
      p.el.style.transform = `translateX(${left - p.center}px)`;
      p.el.style.top = `${LABEL_TOP + row * LABEL_ROW}px`;
    }
    lad.style.height = `${LABEL_TOP + Math.max(1, rowEnds.length) * LABEL_ROW}px`;
  }
}

function renderWatchlist(s) {
  const d = s.daily || {};
  const wl = d.watchlist || [];
  const reg = d.regime;
  let note = d.candle_date ? `Daily close ${d.candle_date}` : "";
  if (reg && reg.enabled) {
    note += ` · regime filter ${reg.allows_entries === false ? "blocking (BTC below its average)" : "on, BTC above its average"}`;
  }
  $("wl-note").textContent = note;
  if (!wl.length) {
    $("watchlist").innerHTML = '<p class="empty" style="padding:16px">The watchlist appears after the first daily scan.</p>';
    return;
  }
  const span = Math.max(0.1, ...wl.map((w) => Math.abs(Math.min(0, w.distance_from_high ?? 0))));
  const rows = wl
    .map((w) => {
      const dist = w.distance_from_high;
      let cell;
      if (w.breakout) {
        cell = status("good", "up", `Breakout ${pctSigned(dist)}`);
      } else {
        const width = dist == null ? 0 : (Math.abs(Math.min(0, dist)) / span) * 100;
        cell = `<div class="dist" data-tip="${esc(`${w.pair}: ${pctSigned(dist)} vs its 20-day high`)}">
          <div class="bar-track"><div class="bar" style="width:${width}%"></div><div class="zero"></div></div>
          <span class="val num">${pctSigned(dist)}</span></div>`;
      }
      return `<tr>
        <td><strong>${esc(w.pair.replace("-USDC", ""))}</strong></td>
        <td class="r num">${price(w.close)}</td>
        <td>${cell}</td>
        <td class="r num hide-sm">${compact.format(w.avg_quote_volume)}</td>
        <td class="r num hide-sm">${pct(w.spread, 2)}</td>
      </tr>`;
    })
    .join("");
  $("watchlist").innerHTML = `<table>
    <thead><tr><th>Coin</th><th class="r">Close (USDC)</th><th>vs 20-day high</th><th class="r hide-sm">Avg volume 7d</th><th class="r hide-sm">Spread</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

function renderAlerts(s) {
  const list = s.recent_alerts || [];
  $("alerts").innerHTML = list.length
    ? list.map((a) => `<li><time datetime="${esc(a.time)}">${localTime(a.time)}</time><p class="text">${esc(a.text)}</p></li>`).join("")
    : '<li class="empty">No alerts yet.</li>';
}

function render(s) {
  renderHeader(s);
  renderHero(s);
  renderCore(s);
  renderSatellite(s);
  layoutLadders();
  renderWatchlist(s);
  renderAlerts(s);
  if (typeof renderTrade === "function") renderTrade(s);
}

async function load() {
  const url = stateUrl();
  try {
    const sep = url.includes("?") ? "&" : "?";
    const r = await fetch(`${url}${sep}t=${Date.now()}`, { cache: "no-store" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    render(await r.json());
  } catch (e) {
    $("updated").textContent = "Could not load data";
    $("banners").innerHTML = `<div class="banner critical">${status("critical", "alert", "")}<div>Could not load <code>${esc(url)}</code> (${esc(e.message)}).</div></div>`;
  }
}

// ------------------------------------------------------------ tooltip

function initTooltip() {
  const tip = $("tip");
  let current = null;
  const show = (el, x, y) => {
    tip.textContent = el.dataset.tip;
    tip.hidden = false;
    const r = tip.getBoundingClientRect();
    tip.style.left = `${Math.max(8, Math.min(x + 12, innerWidth - r.width - 8))}px`;
    tip.style.top = `${Math.max(8, y - r.height - 12)}px`;
  };
  const hide = () => { tip.hidden = true; current = null; };
  document.addEventListener("pointermove", (e) => {
    const el = e.target.closest("[data-tip]");
    if (el) { current = el; show(el, e.clientX, e.clientY); } else if (current) hide();
  });
  document.addEventListener("pointerdown", (e) => {
    const el = e.target.closest("[data-tip]");
    if (el && e.pointerType !== "mouse") { current = el; show(el, e.clientX, e.clientY); } else if (!el) hide();
  });
  addEventListener("scroll", hide, { passive: true });
}

// ------------------------------------------------------------ theme

function initTheme() {
  const btn = $("theme");
  const order = ["auto", "light", "dark"];
  const label = { auto: "Auto", light: "Light", dark: "Dark" };
  let mode = document.documentElement.dataset.theme || "auto";
  const apply = () => {
    if (mode === "auto") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = mode;
    btn.textContent = label[mode];
    btn.setAttribute("aria-label", `Colour theme: ${label[mode]}`);
    try { mode === "auto" ? localStorage.removeItem("theme") : localStorage.setItem("theme", mode); } catch (e) {}
  };
  btn.addEventListener("click", () => { mode = order[(order.indexOf(mode) + 1) % order.length]; apply(); });
  apply();
}

initTheme();
initTooltip();
addEventListener("resize", layoutLadders);
load();
setInterval(load, REFRESH_MS);
document.addEventListener("visibilitychange", () => { if (!document.hidden) load(); });
