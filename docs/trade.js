// Trade section: paper buys (core and satellite) and core sells, committed
// straight to the repository with a GitHub token kept in this browser.
// Uses the helpers in app.js ($, eur, esc, price, status, stateUrl).
"use strict";

const OKX_HOSTS = ["https://www.okx.com", "https://eea.okx.com"]; // eea.okx.com sends no CORS headers
const BRANCH = "main";
const FILES = {
  holdings: "data/core_holdings.json",
  trades: "data/paper_trades.csv",
  log: "data/trade_log.csv",
};
const TRADE_COLUMNS = ["id", "date_opened", "pair", "entry_price", "size_eur",
  "exit1_price", "exit1_time", "exit2_price", "exit2_time", "exit3_price", "exit3_time",
  "exit3_reason", "trailing_high", "date_closed", "status", "notes"];
const LOG_COLUMNS = ["date", "bag", "pair", "side", "quantity", "price", "fee", "notes"];
const QUOTE_MAX_AGE_MS = 60 * 1000; // older quotes are re-fetched before saving

const store = {
  get(k) { try { return localStorage.getItem(k) || ""; } catch (e) { return ""; } },
  set(k, v) { try { v ? localStorage.setItem(k, v) : localStorage.removeItem(k); } catch (e) {} },
};

const T = {
  state: null,
  config: null,
  bag: store.get("trade-bag") || "core",
  side: "buy",
  coin: null,   // { asset, pair } selected
  quote: null,  // fetched prices for the selected coin
  amount: null, // typed EUR amount, kept across redraws
  sellAll: false,
  override: false, // "Buy anyway" ticked
  busy: false,
  message: null, // html of the last save's result
  // Saves the bot hasn't picked up yet: state.json lags by up to a run.
  saved: { holdings: null, satPairs: [], at: null },
};

// ------------------------------------------------------------ pure helpers

const round = (x, d = 10) => Number(x.toFixed(d));
const nowIso = () => new Date().toISOString().slice(0, 16) + "Z";
const today = () => new Date().toISOString().slice(0, 10);

function csvField(v) {
  const s = v == null ? "" : String(v);
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

function appendCsv(text, columns, row) {
  const line = columns.map((c) => csvField(row[c])).join(",");
  let body = (text || "").replace(/\s+$/, "");
  if (!body) body = columns.join(",");
  return `${body}\n${line}\n`;
}

function csvIds(text) {
  const lines = (text || "").split(/\r?\n/).slice(1).filter((l) => l.trim());
  return lines.map((l) => l.split(",")[0].replace(/^"|"$/g, "").trim());
}

function nextTradeId(text) {
  const nums = csvIds(text).map((id) => parseInt(id, 10)).filter(Number.isFinite);
  return String(nums.length ? Math.max(...nums) + 1 : 1);
}

// EUR price of one unit of `asset`, and quantity/fee for an EUR amount.
// Buys fill at the ask, sells at the bid; the fee is taken in EUR.
function priceEur(q, side) {
  const k = side === "buy" ? "ask" : "bid";
  return q.isEurPair ? q[k] : q[k] * q.usdcEur[k];
}

// `cash` is the change in the core's USDC: BTC and ETH trade against it like
// on the USDC pairs, so a buy spends USDC and a sell's proceeds land in it.
// USDC itself (the EUR pair) moves EUR in or out of the core: no cash leg.
function computeOrder({ side, amountEur, q, feeRate, holding, sellAll }) {
  const px = priceEur(q, side);
  if (side === "buy") {
    const fee = amountEur * feeRate;
    const cash = q.isEurPair ? 0 : -amountEur / q.usdcEur.ask;
    return { px, fee, quantity: (amountEur - fee) / px, amountEur, cash };
  }
  const quantity = sellAll ? holding : amountEur / px;
  const gross = sellAll ? holding * px : amountEur;
  const fee = gross * feeRate;
  const cash = q.isEurPair ? 0 : (gross - fee) / q.usdcEur.bid;
  return { px, fee, quantity, amountEur: gross, cash };
}

// ------------------------------------------------------------ data

function repoInfo() {
  const saved = store.get("trade-repo");
  if (saved) return saved;
  if (location.hostname.endsWith(".github.io")) {
    const owner = location.hostname.split(".")[0];
    const repo = location.pathname.split("/").filter(Boolean)[0];
    return `${owner}/${repo}`;
  }
  return "";
}

async function loadConfig() {
  if (T.config) return T.config;
  const url = defaultStateUrl().replace(/data\/state\.json$/, "config.json");
  try {
    const r = await fetch(`${url}?t=${Date.now()}`, { cache: "no-store" });
    if (r.ok) T.config = await r.json();
  } catch (e) {}
  return T.config;
}

async function okxTicker(instId) {
  let last;
  for (const host of OKX_HOSTS) {
    try {
      const r = await fetch(`${host}/api/v5/market/ticker?instId=${encodeURIComponent(instId)}`, { cache: "no-store" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const j = await r.json();
      const d = j.data && j.data[0];
      if (j.code !== "0" || !d) throw new Error(j.msg || "no data");
      const ask = parseFloat(d.askPx), bid = parseFloat(d.bidPx);
      if (!(ask > 0 && bid > 0)) throw new Error("no bid/ask");
      return { ask, bid, askStr: d.askPx, bidStr: d.bidPx, ts: Number(d.ts) };
    } catch (e) { last = e; }
  }
  throw last;
}

// Live prices for a pair plus USDC-EUR; falls back to the last run's prices.
async function fetchQuote(pair) {
  const eurPair = (T.config && T.config.valuation && T.config.valuation.eur_pair) || "USDC-EUR";
  const isEurPair = pair === eurPair;
  try {
    const [p, u] = await Promise.all([okxTicker(pair), isEurPair ? null : okxTicker(eurPair)]);
    return { pair, ...p, usdcEur: u || p, isEurPair, live: true, time: new Date(p.ts).toISOString(), fetchedAt: Date.now() };
  } catch (e) {
    const s = T.state;
    const wl = ((s.daily && s.daily.watchlist) || []).find((w) => w.pair === pair);
    const px = (s.prices && s.prices[pair]) ?? (wl && wl.close);
    const usdc = s.prices && s.prices[eurPair];
    if (px == null || usdc == null) throw new Error(`no price for ${pair} (${e.message})`);
    const pxStr = String(px);
    return {
      pair, ask: px, bid: px, askStr: pxStr, bidStr: pxStr, isEurPair,
      usdcEur: { ask: usdc, bid: usdc }, live: false, time: s.updated_at, error: e.message, fetchedAt: Date.now(),
    };
  }
}

// ------------------------------------------------------------ GitHub

async function gh(path, opts = {}) {
  const token = store.get("trade-token");
  const r = await fetch(`https://api.github.com/repos/${repoInfo()}${path}`, {
    ...opts,
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${token}`,
      "X-GitHub-Api-Version": "2022-11-28",
      ...(opts.body ? { "Content-Type": "application/json" } : {}),
    },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    try { msg += `: ${(await r.json()).message}`; } catch (e) {}
    const err = new Error(msg);
    err.status = r.status;
    throw err;
  }
  return r.status === 204 ? null : r.json();
}

function b64decode(b64) {
  const bin = atob(b64.replace(/\n/g, ""));
  return new TextDecoder().decode(Uint8Array.from(bin, (c) => c.charCodeAt(0)));
}

async function readFile(path, ref) {
  try {
    const f = await gh(`/contents/${path}?ref=${ref}`);
    return b64decode(f.content);
  } catch (e) {
    if (e.status === 404) return "";
    throw e;
  }
}

// Reads `paths` at the branch head, lets `change` rewrite them, and commits
// all of them in one commit. If a bot run pushed in between, starts again
// from the new head so neither side's change is lost.
async function commitFiles(paths, change, message) {
  for (let attempt = 1; attempt <= 4; attempt++) {
    const ref = await gh(`/git/ref/heads/${BRANCH}`);
    const head = ref.object.sha;
    const base = await gh(`/git/commits/${head}`);
    const current = {};
    for (const p of paths) current[p] = await readFile(p, head);
    const updated = change(current);
    const tree = await gh("/git/trees", {
      method: "POST",
      body: {
        base_tree: base.tree.sha,
        tree: Object.entries(updated).map(([path, content]) => ({ path, mode: "100644", type: "blob", content })),
      },
    });
    const commit = await gh("/git/commits", { method: "POST", body: { message, tree: tree.sha, parents: [head] } });
    try {
      await gh(`/git/refs/heads/${BRANCH}`, { method: "PATCH", body: { sha: commit.sha, force: false } });
      return commit;
    } catch (e) {
      if (e.status !== 422 || attempt === 4) throw e;
      await new Promise((res) => setTimeout(res, attempt * 1500));
    }
  }
}

// ------------------------------------------------------------ rules

function cashAsset() {
  return (T.config && T.config.valuation && T.config.valuation.quote) || "USDC";
}

function coreAssets() {
  const pairs = (T.config && T.config.core.pairs) || { BTC: "BTC-USDC", ETH: "ETH-USDC" };
  const v = (T.config && T.config.valuation) || { quote: "USDC", eur_pair: "USDC-EUR" };
  return [...Object.entries(pairs).map(([asset, pair]) => ({ asset, pair })), { asset: v.quote, pair: v.eur_pair }];
}

function satelliteAssets() {
  const wl = (T.state.daily && T.state.daily.watchlist) || [];
  return wl.map((w) => ({ asset: w.pair.split("-")[0], pair: w.pair, breakout: w.breakout, dist: w.distance_from_high }));
}

function holdingOf(asset) {
  if (T.saved.holdings) return Number(T.saved.holdings[asset]) || 0;
  const a = T.state.core && T.state.core.assets.find((x) => x.asset === asset);
  return a ? Number(a.quantity) || 0 : 0;
}

// Satellite rules that a buy breaks; each is shown and needs a confirm.
function satelliteWarnings(amountEur) {
  const sat = T.state.satellite;
  const cfg = (T.config && T.config.satellite) || {};
  const out = [];
  const max = cfg.max_open_trades ?? sat.max_open_trades ?? 3;
  const openPairs = [...sat.open_trades.map((t) => t.pair), ...T.saved.satPairs];
  if (cfg.trade_size_eur && Math.abs(amountEur - cfg.trade_size_eur) > 1e-9) {
    out.push(`Trade size is ${eur(cfg.trade_size_eur)} in the rules, not ${eur(amountEur)}.`);
  }
  if (openPairs.length >= max) out.push(`Already ${openPairs.length} open trades (max ${max}).`);
  if (sat.balance_eur < sat.floor_eur) out.push(`Satellite balance ${eur(sat.balance_eur)} is below the ${eur(sat.floor_eur)} floor.`);
  const reg = T.state.daily && T.state.daily.regime;
  if (reg && reg.enabled && reg.allows_entries === false) out.push("Regime filter is blocking: BTC is below its moving average.");
  if (T.coin && openPairs.includes(T.coin.pair)) out.push(`${T.coin.pair} already has an open trade.`);
  const wl = ((T.state.daily && T.state.daily.watchlist) || []).find((w) => T.coin && w.pair === T.coin.pair);
  const days = (T.state.daily && T.state.daily.lookback_days) || cfg.breakout_lookback_days || 20;
  if (wl && !wl.breakout) out.push(`${T.coin.asset} is not breaking out (${pctSigned(wl.distance_from_high)} vs its ${days}-day high).`);
  return out;
}

// ------------------------------------------------------------ rendering

function renderTrade(s) {
  T.state = s;
  // a run that started after the last save has it in state.json
  if (T.saved.at && Date.parse(s.updated_at) > T.saved.at + 60000) T.saved = { holdings: null, satPairs: [], at: null };
  loadConfig().then(drawTrade);
}

function drawTrade() {
  if (!T.state) return;
  // the 5-minute refresh redraws the card: keep the cursor in the amount box
  const typing = document.activeElement && document.activeElement.id === "trade-amount";
  const token = store.get("trade-token");
  $("trade-note").innerHTML = token && repoInfo()
    ? status("good", "check", `Saves to ${repoInfo()}`)
    : status("neutral", "dot", "Read only");

  const assets = T.bag === "core" ? coreAssets() : satelliteAssets();
  if (T.coin && !assets.some((a) => a.pair === T.coin.pair)) { T.coin = null; T.quote = null; }
  if (T.bag === "satellite") T.side = "buy";

  const tabs = ["core", "satellite"].map((b) =>
    `<button type="button" class="seg${T.bag === b ? " on" : ""}" data-bag="${b}" aria-pressed="${T.bag === b}">${b === "core" ? "Core" : "Satellite"}</button>`).join("");
  const chips = assets.length
    ? assets.map((a) => {
      const held = T.bag === "core" ? holdingOf(a.asset) : 0;
      const sub = T.bag === "core" ? (held ? price(held) : "none held") : a.breakout ? "breakout" : `${pctSigned(a.dist)} vs high`;
      return `<button type="button" class="chip${T.coin && T.coin.pair === a.pair ? " on" : ""}" data-pair="${esc(a.pair)}" data-asset="${esc(a.asset)}">
        <strong>${esc(a.asset)}</strong><span class="num">${esc(sub)}</span></button>`;
    }).join("")
    : '<p class="empty">The watchlist appears after the first daily scan.</p>';

  $("trade").innerHTML = `
    <div class="trade-top"><div class="segs" role="group" aria-label="Bag">${tabs}</div>
      <button type="button" class="link" id="trade-settings">${token ? "GitHub token ✓" : "Connect GitHub"}</button></div>
    <div class="chips">${chips}</div>
    <div id="trade-panel"></div>`;
  drawPanel();
  if (typing && $("trade-amount")) $("trade-amount").focus();
}

function drawPanel() {
  const el = $("trade-panel");
  if (!el) return;
  if (!T.coin) {
    el.innerHTML = `<p class="empty">Pick a coin to fetch its price.</p>${messageHtml()}`;
    return;
  }
  const q = T.quote;
  if (!q || q.loading) {
    el.innerHTML = `<p class="empty">Fetching ${esc(T.coin.pair)}…</p>`;
    return;
  }
  if (q.failed) {
    el.innerHTML = `<p class="empty">${status("critical", "alert", `Could not get a price: ${q.failed}`)}</p>`;
    return;
  }
  const cfg = T.config || {};
  const isCore = T.bag === "core";
  const holding = isCore ? holdingOf(T.coin.asset) : 0;
  const px = priceEur(q, T.side);
  const native = T.side === "buy" ? q.askStr : q.bidStr;
  const quoteCcy = T.coin.pair.split("-")[1];

  const sides = isCore ? `<div class="segs small" role="group" aria-label="Side">
      ${["buy", "sell"].map((sd) => `<button type="button" class="seg${T.side === sd ? " on" : ""}" data-side="${sd}" aria-pressed="${T.side === sd}">${sd === "buy" ? "Buy" : "Sell"}</button>`).join("")}
    </div>` : "";
  const defaultAmt = !isCore && cfg.satellite ? cfg.satellite.trade_size_eur : "";
  if (T.sellAll) T.amount = (holding * px).toFixed(2);
  const amtValue = T.amount ?? String(defaultAmt);
  const cashNote = isCore && q.isEurPair
    ? `<p class="note">${esc(cashAsset())} is the core's cash. Buying it adds EUR to the core, selling takes EUR out; BTC and ETH buys are paid from it.</p>`
    : "";

  el.innerHTML = `
    <div class="quote">
      <div><span class="pair">${esc(T.coin.pair)}</span>
        <span class="num">${T.side === "buy" ? "Ask" : "Bid"} ${price(parseFloat(native))} ${esc(quoteCcy)}</span>
        <span class="note num">≈ ${eur(px)}</span></div>
      <div class="note">${q.live ? `Live from OKX · ${localTime(q.time)}` : status("warning", "alert", `OKX unreachable, last run's price (${localTime(q.time)})`)}
        <button type="button" class="link" id="trade-refresh">Refresh</button></div>
    </div>
    ${sides}
    ${cashNote}
    <label class="field"><span class="label">Amount in EUR</span>
      <span class="amount-row"><input id="trade-amount" type="number" inputmode="decimal" min="0" step="0.01" value="${esc(amtValue)}" placeholder="0.00">
      ${isCore && T.side === "sell" ? `<button type="button" class="link" id="trade-max">All (${price(holding)})</button>` : ""}</span>
    </label>
    <div id="trade-calc"></div>
    ${messageHtml()}`;
  drawCalc();
}

function currentOrder() {
  const input = $("trade-amount");
  const amountEur = input ? parseFloat(input.value) : NaN;
  const feeRate = (T.config && T.config.satellite && T.config.satellite.fee_rate) ?? 0.001;
  const holding = T.bag === "core" ? holdingOf(T.coin.asset) : 0;
  if (!T.sellAll && !(amountEur > 0)) return { amountEur, holding, invalid: "Enter an amount." };
  const o = computeOrder({ side: T.side, amountEur, q: T.quote, feeRate, holding, sellAll: T.sellAll });
  if (T.side === "sell" && !holding) return { ...o, holding, invalid: `No ${T.coin.asset} held.` };
  if (T.side === "sell" && o.quantity > holding + 1e-12) {
    return { ...o, holding, invalid: `You hold ${price(holding)} ${T.coin.asset} (${eur(holding * o.px)}).` };
  }
  if (T.bag === "core" && o.cash < 0) {
    const cash = holdingOf(cashAsset());
    if (-o.cash > cash + 1e-9) {
      return { ...o, holding, invalid: `Core cash is ${price(cash)} ${cashAsset()} (${eur(cash * T.quote.usdcEur.ask)}). Sell something or buy ${cashAsset()} first.` };
    }
  }
  return { ...o, holding };
}

function drawCalc() {
  const el = $("trade-calc");
  if (!el) return;
  const o = currentOrder();
  const canSave = !!(store.get("trade-token") && repoInfo());
  let html = "";
  if (o.invalid) {
    html = `<p class="note">${esc(o.invalid)}</p>`;
  } else {
    const verb = T.side === "buy" ? "get" : "sell";
    const net = T.side === "buy" ? "" : ` · you receive ${eur(o.amountEur - o.fee)}`;
    const cash = T.bag !== "core" || !o.cash ? ""
      : ` · ${o.cash < 0 ? "paid from" : "into"} core cash: ${price(Math.abs(o.cash))} ${esc(cashAsset())}`;
    html = `<p class="calc num">You ${verb} <strong>${price(o.quantity)} ${esc(T.coin.asset)}</strong> · fee ${eur(o.fee)}${net}${cash}</p>`;
    const warns = T.bag === "satellite" ? satelliteWarnings(o.amountEur) : [];
    if (warns.length) {
      html += `<div class="warns">${status("warning", "alert", "Breaks the satellite rules")}<ul>${warns.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>
        <label class="check"><input type="checkbox" id="trade-override"${T.override ? " checked" : ""}> Buy anyway</label></div>`;
    }
  }
  const label = T.side === "buy" ? "Confirm buy" : "Confirm sell";
  html += `<div class="actions"><button type="button" class="primary" id="trade-confirm" ${o.invalid || !canSave || T.busy ? "disabled" : ""}>${T.busy ? "Saving…" : label}</button>
    ${canSave ? "" : '<span class="note">Connect GitHub to save trades.</span>'}</div>`;
  el.innerHTML = html;
  updateConfirm();
}

function updateConfirm() {
  const btn = $("trade-confirm");
  const box = $("trade-override");
  const canSave = !!(store.get("trade-token") && repoInfo());
  if (btn && box && !T.busy) btn.disabled = !box.checked || !canSave;
}

function messageHtml() {
  return T.message ? `<div class="trade-msg">${T.message}</div>` : "";
}

// ------------------------------------------------------------ actions

async function selectCoin(pair, asset) {
  T.coin = { pair, asset };
  T.sellAll = false;
  T.override = false;
  T.amount = null;
  T.message = null;
  T.quote = { loading: true };
  drawTrade();
  try {
    const q = await fetchQuote(pair);
    if (T.coin && T.coin.pair === pair) T.quote = q;
  } catch (e) {
    if (T.coin && T.coin.pair === pair) T.quote = { failed: e.message };
  }
  drawPanel();
}

async function confirmTrade() {
  if (T.busy) return;
  if (Date.now() - T.quote.fetchedAt > QUOTE_MAX_AGE_MS) {
    // never save at a price fetched minutes ago: refresh and ask again
    const { pair, asset } = T.coin;
    T.busy = true;
    drawCalc();
    try {
      const q = await fetchQuote(pair);
      if (T.coin && T.coin.pair === pair) T.quote = q;
      T.message = `${status("warning", "alert", "Price refreshed")} The price was over a minute old. Check the numbers and confirm again.`;
    } catch (e) {
      T.message = `${status("critical", "alert", "Not saved")} Could not refresh the ${esc(asset)} price: ${esc(e.message)}.`;
    } finally {
      T.busy = false;
      drawPanel();
    }
    return;
  }
  const o = currentOrder();
  if (o.invalid) return;
  const { asset, pair } = T.coin;
  const q = T.quote;
  const side = T.side;
  const bag = T.bag;
  const nativePx = side === "buy" ? q.askStr : q.bidStr;
  const when = nowIso();
  const src = q.live ? "dashboard" : "dashboard, last run price";
  const cashCcy = cashAsset();
  const cashNote = bag === "core" && o.cash
    ? `; ${o.cash < 0 ? "paid" : "received"} ${round(Math.abs(o.cash), 6)} ${cashCcy}` : "";
  const note = `${src}; ${eur(o.amountEur)} at ${eur(o.px)}/${asset}${cashNote}`;
  const logRow = {
    date: when, bag, pair, side, quantity: round(o.quantity, 10), price: nativePx,
    fee: round(o.fee, 4), notes: note,
  };

  T.busy = true;
  T.message = null;
  drawCalc();
  try {
    let tradeId, newHoldings;
    const paths = bag === "core" ? [FILES.holdings, FILES.log] : [FILES.trades, FILES.log];
    const commit = await commitFiles(paths, (cur) => {
      const out = { [FILES.log]: appendCsv(cur[FILES.log], LOG_COLUMNS, logRow) };
      if (bag === "core") {
        const h = cur[FILES.holdings] ? JSON.parse(cur[FILES.holdings]) : {};
        const held = Number(h[asset]) || 0;
        if (side === "sell" && o.quantity > held + 1e-12) throw new Error(`the repo shows only ${held} ${asset}`);
        h[asset] = round(Math.max(0, side === "buy" ? held + o.quantity : held - o.quantity), 10);
        if (o.cash) {
          const cash = (Number(h[cashCcy]) || 0) + o.cash;
          if (cash < -1e-9) throw new Error(`the repo shows only ${Number(h[cashCcy]) || 0} ${cashCcy} of core cash`);
          h[cashCcy] = round(Math.max(0, cash), 10);
        }
        out[FILES.holdings] = JSON.stringify(h, null, 2) + "\n";
        newHoldings = h;
      } else {
        tradeId = nextTradeId(cur[FILES.trades]);
        out[FILES.trades] = appendCsv(cur[FILES.trades], TRADE_COLUMNS, {
          id: tradeId, date_opened: today(), pair, entry_price: nativePx,
          size_eur: round(o.amountEur, 2), notes: src,
        });
      }
      return out;
    }, `Dashboard ${side} ${pair} ${eur(o.amountEur)} (${bag})`);
    const what = bag === "core"
      ? `${side === "buy" ? "Bought" : "Sold"} ${price(o.quantity)} ${esc(asset)} for ${eur(o.amountEur)}.`
      : `Opened satellite trade ${esc(tradeId)}: ${esc(pair)} at ${esc(nativePx)}, ${eur(o.amountEur)}.`;
    T.message = `${status("good", "check", "Saved")} ${what} <a href="${esc(commit.html_url)}" target="_blank" rel="noopener">Commit</a>. The dashboard shows it after the next run (about 15 minutes).`;
    T.sellAll = false;
    T.override = false;
    T.amount = "";
    T.saved.at = Date.now();
    if (newHoldings) T.saved.holdings = newHoldings;
    if (bag === "satellite") T.saved.satPairs.push(pair);
  } catch (e) {
    const hint = e.status === 401 || e.status === 403 || e.status === 404
      ? " Check the token has Contents: read and write on this repository." : "";
    T.message = `${status("critical", "alert", "Not saved")} ${esc(e.message)}.${hint}`;
  } finally {
    T.busy = false;
    drawTrade();
  }
}

function openSettings() {
  const dlg = $("trade-dialog");
  $("set-token").value = store.get("trade-token");
  $("set-repo").value = repoInfo();
  dlg.returnValue = "";
  dlg.showModal();
}

function initTrade() {
  const root = $("trade");
  root.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    if (b.dataset.bag) {
      T.bag = b.dataset.bag; store.set("trade-bag", T.bag);
      T.coin = null; T.quote = null; T.message = null;
      drawTrade();
    } else if (b.dataset.pair) {
      selectCoin(b.dataset.pair, b.dataset.asset);
    } else if (b.dataset.side) {
      T.side = b.dataset.side; T.sellAll = false; T.override = false; T.amount = null; T.message = null;
      drawPanel();
    } else if (b.id === "trade-refresh") {
      selectCoin(T.coin.pair, T.coin.asset);
    } else if (b.id === "trade-max") {
      T.sellAll = true;
      T.amount = (holdingOf(T.coin.asset) * priceEur(T.quote, "sell")).toFixed(2);
      $("trade-amount").value = T.amount;
      drawCalc();
    } else if (b.id === "trade-confirm") {
      confirmTrade();
    } else if (b.id === "trade-settings") {
      openSettings();
    }
  });
  root.addEventListener("input", (e) => {
    if (e.target.id === "trade-amount") { T.amount = e.target.value; T.sellAll = false; T.override = false; drawCalc(); }
  });
  root.addEventListener("change", (e) => {
    if (e.target.id === "trade-override") { T.override = e.target.checked; updateConfirm(); }
  });

  $("trade-dialog").addEventListener("close", () => {
    const dlg = $("trade-dialog");
    if (dlg.returnValue === "save") {
      store.set("trade-token", $("set-token").value.trim());
      store.set("trade-repo", $("set-repo").value.trim());
    } else if (dlg.returnValue === "forget") {
      store.set("trade-token", "");
    }
    drawTrade();
  });
}

initTrade();
