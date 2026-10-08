# Crypto core-satellite paper trading

Paper-trading alerts and a static dashboard for a core-satellite strategy on
OKX Europe. Public market data only: no API keys, no orders, no account access.

All strategy numbers live in `config.json`.

## Run locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m src.main --dry-run   # prints the alerts it would send; writes nothing
.venv/bin/python -m src.main             # sends to Telegram and writes data/
```

A real run needs `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in the environment.

## Setup on GitHub

1. **Telegram bot.** In Telegram, message @BotFather, send `/newbot` and keep
   the token. Send any message to the new bot, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and note `chat.id`.
2. **Repository.** Create an empty repository on GitHub (no README), then:
   ```bash
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```
3. **Secrets.** Repository → Settings → Secrets and variables → Actions →
   New repository secret: `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
4. **Workflow permissions.** Settings → Actions → General → Workflow
   permissions → "Read and write permissions" (the run commits `data/`).
5. **First run.** Actions → Paper run → Run workflow, first with "dry run"
   ticked (alerts appear in the log), then without. The schedule starts on
   its own after that.

### Dashboard

`docs/` is a static page published with GitHub Pages. One-time setup:
Settings → Pages → Build and deployment → Source: **GitHub Actions**. The
Dashboard workflow then publishes it whenever `docs/` changes, at
`https://<you>.github.io/<repo>/`.

The page reads `data/state.json` straight from the repository
(raw.githubusercontent.com, cached up to 5 minutes) and refreshes every 5
minutes, so new runs show up without republishing. It never calls OKX.
Locally, serve the repository root and open `/docs/`:

```bash
.venv/bin/python -m http.server 8000   # then http://localhost:8000/docs/
```

`?state=<url>` loads a different state file, e.g. a sample.

### How the schedule behaves

- Runs every 15 minutes at :07, :22, :37 and :52. GitHub delays scheduled
  runs under load and can drop some, so stop alerts are not real time. Fine
  for paper trading; not something to rely on with real money.
- Each run commits `data/state.json`, `data/alert_state.json` and any new
  exits in `data/paper_trades.csv` as `github-actions[bot]`.
- **You can edit `paper_trades.csv` on GitHub at any time.** If you push while
  a run is in progress, the bot starts again from your version and re-applies
  only its exits (`scripts/commit_state.sh`). Rows you added or changed keep
  your version; if you edited a trade the run had just closed, your edit wins
  and the next run re-evaluates it.
- **Schedules switch off after 60 days without activity** in public
  repositories. GitHub doesn't say whether the bot's own commits count, so each
  run also calls the "enable workflow" API, and the daily heartbeat message
  (`alerts.heartbeat`) is on: if it stops arriving, check the Actions tab.
- **Minutes.** Public repositories run free. A private repository on GitHub
  Free gets 2,000 minutes a month, and 96 runs a day (~2,900 a month, each
  billed as at least a minute) doesn't fit. For a private repository, change
  the cron to every 30 minutes (`"7,37 * * * *"`).
- Code changes run the tests on Python 3.11 (`.github/workflows/tests.yml`).

## Files you edit

| File | What goes in it |
|---|---|
| `data/core_holdings.json` | Quantity held of BTC, ETH, USDC |
| `data/paper_trades.csv` | A new row per paper buy: `id, date_opened, pair, entry_price, size_eur` (and `notes`). The script fills in the rest. |
| `data/transfers.csv` | Money moved between bags: `date, from_bag, to_bag, amount_eur, notes` (`core` / `satellite`) |
| `data/trade_log.csv` | Your own record of every buy and sell; the script does not read it |

## Alerts

- **Conditions** (core drift, satellite below floor, data problems) alert once,
  then again only after they clear and come back.
- **Events** (exits, breakouts, quarterly rebalance and sweep reminders, bad
  rows in `paper_trades.csv`) alert once.
- One message per trade per run, even when a stop closes all three thirds.
- A message that fails to send is retried on the next run.
- A price that is missing, older than an hour, or moved more than 25% since the
  previous run is skipped for that run with a "data problem" alert. A real move
  is acted on at the next run.
- The watchlist and breakout scan run once per new confirmed daily candle.
  Breakouts are not sent while new entries are blocked (below floor, 3 trades
  open, or the regime filter).

## Decisions

- **API host:** `eea.okx.com` (OKX Europe), falling back to `www.okx.com`.
  Both served identical public data when checked in October 2026.
- **Valuation:** BTC and ETH are priced on their USDC pairs and converted once
  with `USDC-EUR`.
- **Trailing stop:** 8% below the highest price since entry, active only after T2.
- **Spread filter:** 0.2% of mid price.
- **Daily candles** use OKX's `1Dutc` bar; only bars with `confirm = 1` count.
- **Repo visibility:** public. Holdings, paper trades and the dashboard are
  visible to anyone with the link.
- **Watchlist:** BTC and ETH are left out (they are the core), and so are
  OKX's tokenized stocks (`instCategory` 3, e.g. XMSTR). Both are config
  switches (`exclude_core`, `inst_categories`). The 24h ticker volume picks
  30 candidates; the 7-day average of daily candle volume ranks them.
- **Paper P&L:** each third is a third of the EUR size times the pair's return,
  after the fee on the buy and the sell. USDC/EUR moves are ignored.
- **Satellite balance:** starting satellite money + realized P&L + transfers.
  The floor and sweep checks use it; open trades count only once sold.
- **Exit order each run:** stop-loss (closes every unsold third), T1, T2, then
  the trailing stop on the last third. Fills are the price seen at that run.

## Tests

```bash
.venv/bin/python -m pytest -q
```
