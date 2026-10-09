"""One run: fetch → evaluate → alert → write state.

    .venv/bin/python -m src.main             # sends Telegram alerts, writes data/
    .venv/bin/python -m src.main --dry-run   # prints alerts, writes nothing

Every run checks prices, core drift and open paper trades. The watchlist and
breakout scan run once per confirmed UTC daily candle.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from src import alerts, rules, trades_file, watchlist
from src.alerts import Alert
from src.okx_client import OKXClient, OKXError

ROOT = Path(__file__).resolve().parent.parent
STALE_TICKER_MS = 60 * 60 * 1000
RECENT_ALERTS = 20


def load_config(path: Path = ROOT / "config.json") -> dict:
    return json.loads(path.read_text())


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return default


def _fmt(price: float) -> str:
    """Prices from 80,000 down to 0.00001 with useful precision."""
    return f"{price:,.2f}" if price >= 100 else f"{price:.6g}"


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _eur(x: float, sign: bool = False) -> str:
    return f"{x:+.2f} €" if sign else f"{x:.2f} €"


class Run:
    def __init__(self, client: OKXClient, cfg: dict, data_dir: Path, now: datetime):
        self.client, self.cfg, self.data, self.now = client, cfg, data_dir, now
        self.prev = _read_json(data_dir / "state.json", {})
        self.prev_prices: dict = self.prev.get("prices", {})
        self.prices: dict = dict(self.prev_prices)
        self.alerts: list[Alert] = []
        self.evaluated: set[str] = set()
        self.problems: list[str] = []
        self.tickers: dict = {}
        self.checked: dict[str, float | None] = {}  # price() result per pair this run

    # ------------------------------------------------------------ prices

    def price(self, pair: str) -> float | None:
        """Last price, or None (and a data-problem alert) when it can't be trusted.

        Checked once per pair per run, so two trades on one pair share a result.
        """
        if pair not in self.checked:
            self.checked[pair] = self._check_price(pair)
        return self.checked[pair]

    def _check_price(self, pair: str) -> float | None:
        t = self.tickers.get(pair)
        last = t.last if t else None
        reason = None
        if t is None:
            reason = "no ticker"
        elif not rules.price_is_sane(self.prev_prices.get(pair), last,
                                     self.cfg["sanity"]["max_move_between_runs"]):
            prev = self.prev_prices.get(pair)
            reason = (f"price {last}" if not last or last <= 0 else
                      f"jumped from {_fmt(prev)} to {_fmt(last)} since the last run")
        elif self.now.timestamp() * 1000 - t.ts > STALE_TICKER_MS:
            reason = "ticker older than one hour"
        if last and last > 0:
            self.prices[pair] = last  # a real jump passes on the next run
        if reason:
            text = f"⚠️ Data problem — {pair}: {reason}. Alerts for this pair skipped this run."
            self.problems.append(f"{pair}: {reason}")
            self.alerts.append(Alert(f"data:{pair}", text, condition=True))
            return None
        return last

    # -------------------------------------------------------------- core

    def core(self) -> dict | None:
        cfg = self.cfg["core"]
        usdc_eur = self.price(self.cfg["valuation"]["eur_pair"])
        quote_px = {a: self.price(p) for a, p in cfg["pairs"].items()}
        if usdc_eur is None or any(v is None for v in quote_px.values()):
            return None
        prices_eur = {a: px * usdc_eur for a, px in quote_px.items()}
        prices_eur[self.cfg["valuation"]["quote"]] = usdc_eur
        holdings = _read_json(self.data / "core_holdings.json", {})
        drift = rules.core_drift(holdings, prices_eur, cfg["targets"], cfg["drift_band"])
        total = sum(d.value_eur for d in drift)
        self.evaluated.add("drift")

        plan = ", ".join(f"{d.asset} {'buy' if d.rebalance_eur > 0 else 'sell'} "
                         f"{_eur(abs(d.rebalance_eur))}" for d in drift
                         if abs(d.rebalance_eur) >= 0.01)
        out = [d for d in drift if d.out_of_band] if total > 0 else []
        if out:
            # one message per set of out-of-band assets; a new set alerts again
            lines = "\n".join(f"{d.asset} at {_pct(d.weight)} (band {_pct(d.low)}–"
                              f"{_pct(d.high)}, target {_pct(d.target)})" for d in out)
            self.alerts.append(Alert(
                "drift:" + "+".join(d.asset for d in out),
                f"⚖️ Core drift\n{lines}\nCore value {_eur(total)}. "
                f"To return to target: {plan}.", condition=True))

        period = rules.rebalance_period(self.now.date(), cfg["rebalance_months"])
        if period and total > 0:
            self.alerts.append(Alert(f"rebalance:{period}", (
                f"🗓️ Quarterly rebalance due ({period}). Core value {_eur(total)}."
                + (f"\nTo return to target: {plan}." if plan else ""))))

        return {
            "total_eur": total,
            "usdc_eur": usdc_eur,
            "rebalance_due": period,
            "assets": [{"asset": d.asset, "quantity": holdings.get(d.asset, 0),
                        "price_eur": prices_eur[d.asset], "value_eur": d.value_eur,
                        "weight": d.weight, "target": d.target, "low": d.low,
                        "high": d.high, "out_of_band": d.out_of_band,
                        "rebalance_eur": d.rebalance_eur} for d in drift],
        }

    # --------------------------------------------------------- satellite

    def paper_trades(self) -> tuple[list[rules.PaperTrade], list[rules.PaperTrade]]:
        """All valid trades after this run, and those this run changed."""
        sat = self.cfg["satellite"]
        trades, row_problems = trades_file.load(self.data / "paper_trades.csv")
        if row_problems:
            digest = hashlib.sha1("\n".join(sorted(row_problems)).encode()).hexdigest()[:10]
            self.problems += row_problems
            self.alerts.append(Alert(f"rows:{digest}", (
                f"⚠️ paper_trades.csv: {len(row_problems)} row(s) skipped\n"
                + "\n".join(f"• {p}" for p in row_problems))))

        result, changed = [], []
        for t in trades:
            if not t.is_open:
                result.append(t)
                continue
            px = self.price(t.pair)
            if px is None:
                result.append(t)
                continue
            new, events = rules.step(t, px, self.now, sat)
            result.append(new)
            if new != t:
                changed.append(new)
            if events:
                thirds = "+".join(str(e.third) for e in events)
                self.alerts.append(Alert(f"exit:{t.id}:{thirds}",
                                         self._exit_text(new, events, sat["fee_rate"])))
        return result, changed

    @staticmethod
    def _exit_text(trade: rules.PaperTrade, events: list[rules.ExitEvent],
                   fee: float) -> str:
        """One message for all thirds a trade sold in this run."""
        labels = {"stop": ("🔴", "Stop-loss hit", "stop"),
                  "target1": ("🟢", "Target 1 hit", "T1"),
                  "target2": ("🟢", "Target 2 hit", "T2"),
                  "trailing_stop": ("🟠", "Trailing stop hit", "trail")}
        icon, title, _ = labels[events[-1].reason]
        lines = [f"{icon} {title} — {trade.pair} (trade {trade.id}, "
                 f"entry {_fmt(trade.entry_price)})"]
        for e in events:
            lines.append(f"Third {e.third}/3 sold at {_fmt(e.price)} "
                         f"({labels[e.reason][2]} {_fmt(e.rule_level)}): "
                         f"{_eur(e.pnl_eur, sign=True)}")
        if not trade.is_open:
            lines.append(f"Trade closed. Total P&L: {_eur(trade.realized_pnl(fee), sign=True)}")
        return "\n".join(lines)

    def bag(self, trades: list[rules.PaperTrade]) -> dict:
        sat = self.cfg["satellite"]
        alloc = self.cfg["allocation"]
        fee = sat["fee_rate"]
        start = alloc["start_eur"] * alloc["satellite_share"]
        transfers = trades_file.load_transfers(self.data / "transfers.csv")
        balance = rules.satellite_balance(start, trades, fee, transfers)
        status = rules.bag_status(balance, sat)
        self.evaluated.add("floor")
        if status == "pause":
            self.alerts.append(Alert("floor:satellite", (
                f"🛑 Satellite balance {_eur(balance)} is below the {_eur(sat['floor_eur'])} "
                f"floor. Pause trading: new entry signals are suppressed."), condition=True))
        elif status == "sweep":
            quarter = f"{self.now.year}-Q{(self.now.month - 1) // 3 + 1}"
            excess = balance - start
            self.alerts.append(Alert(f"sweep:{quarter}", (
                f"💶 Satellite balance {_eur(balance)} is above {_eur(sat['sweep_above_eur'])}.\n"
                f"Quarterly sweep: consider moving {_eur(excess)} (the amount above the "
                f"{_eur(start)} start) to the core, and log it in transfers.csv.")))

        open_rows = []
        for t in trades:
            if not t.is_open:
                continue
            px = self.prices.get(t.pair)
            lv = rules.levels(t.entry_price, sat)
            high = t.trailing_high or t.entry_price
            open_rows.append({
                "id": t.id, "pair": t.pair, "date_opened": t.row.get("date_opened", ""),
                "entry_price": t.entry_price, "size_eur": t.size_eur, "price": px,
                "stop": lv.stop, "target1": lv.target1, "target2": lv.target2,
                "trailing_high": high, "trailing_level": rules.trailing_level(high, sat),
                "thirds_sold": 3 - t.remaining,
                "realized_eur": t.realized_pnl(fee),
                "unrealized_eur": t.unrealized_pnl(px, fee) if px else None,
            })
        closed = [t for t in trades if not t.is_open]
        return {
            "start_eur": start, "transfers_eur": transfers, "balance_eur": balance,
            "status": status, "floor_eur": sat["floor_eur"],
            "max_open_trades": sat["max_open_trades"],
            "sweep_above_eur": sat["sweep_above_eur"],
            "realized_eur": sum(t.realized_pnl(fee) for t in trades),
            "unrealized_eur": sum(r["unrealized_eur"] or 0 for r in open_rows),
            "win_rate": rules.win_rate(trades, fee),
            "closed_trades": len(closed),
            "open_trades": open_rows,
        }

    # ------------------------------------------------------------- daily

    def daily(self, open_pairs: set[str], open_count: int, balance: float) -> dict:
        """Watchlist and breakouts, once per new confirmed daily candle."""
        sat, reg = self.cfg["satellite"], self.cfg["regime_filter"]
        prev = self.prev.get("daily", {})
        btc = self.client.daily_candles(reg["pair"], limit=reg["sma_days"] + 10)
        if not btc:
            raise OKXError(f"no confirmed daily candles for {reg['pair']}")
        candle_date = datetime.fromtimestamp(btc[-1].ts / 1000, timezone.utc).date().isoformat()

        regime_ok = None
        sma = rules.sma([c.close for c in btc], reg["sma_days"])
        if reg["enabled"]:
            regime_ok = rules.regime_allows_entries([c.close for c in btc], reg["sma_days"])
        block = rules.entry_block(open_count, balance, sat, regime_ok)

        if prev.get("candle_date") == candle_date:
            return {**prev, "entry_block": block}

        lookback = sat["breakout_lookback_days"]
        items, candles = watchlist.build(
            self.client, self.cfg["watchlist"],
            core_bases=list(self.cfg["core"]["targets"]),
            history_limit=max(lookback + 1, self.cfg["watchlist"]["min_history_days"]) + 10)
        rows = []
        for item in items:
            bars = candles[item.pair]
            is_breakout = rules.breakout(bars, lookback)
            rows.append({"pair": item.pair, "avg_quote_volume": item.avg_quote_volume,
                         "spread": item.spread, "close": bars[-1].close,
                         "distance_from_high": rules.distance_from_high(bars, lookback),
                         "breakout": is_breakout})
            if is_breakout and item.pair not in open_pairs and block is None:
                lv = rules.levels(bars[-1].close, sat)
                prior_high = max(c.high for c in bars[-lookback - 1:-1])
                self.alerts.append(Alert(f"breakout:{item.pair}:{candle_date}", (
                    f"🚀 Breakout — {item.pair}\n"
                    f"Closed {_fmt(bars[-1].close)} on {candle_date}, above its "
                    f"{lookback}-day high {_fmt(prior_high)}.\n"
                    f"Suggested paper entry: {_eur(sat['trade_size_eur'])} near "
                    f"{_fmt(bars[-1].close)}\n"
                    f"Stop {_fmt(lv.stop)} · T1 {_fmt(lv.target1)} · T2 {_fmt(lv.target2)}\n"
                    f"Open slots: {sat['max_open_trades'] - open_count} of "
                    f"{sat['max_open_trades']}. Log it in paper_trades.csv to track it.")))

        if len(items) < self.cfg["watchlist"]["size_min"]:
            self.problems.append(f"watchlist has only {len(items)} pairs")
        return {
            "candle_date": candle_date, "run_at": alerts.stamp(self.now),
            "lookback_days": lookback, "watchlist": rows, "entry_block": block,
            "regime": {"enabled": reg["enabled"], "pair": reg["pair"],
                       "sma_days": reg["sma_days"], "sma": sma,
                       "close": btc[-1].close, "allows_entries": regime_ok},
        }

    # --------------------------------------------------------------- run

    def evaluate(self) -> tuple[dict, list[rules.PaperTrade]]:
        try:
            self.tickers = self.client.tickers()
        except OKXError as e:
            self.evaluated.add("data")
            self.alerts.append(Alert("data:okx", (
                f"⚠️ Data problem — OKX unreachable ({e}). No checks this run."),
                condition=True))
            return {**self.prev, "updated_at": alerts.stamp(self.now),
                    "problems": [f"OKX unreachable: {e}"]}, []
        self.evaluated.add("data")

        core = self.core()
        trades, changed = self.paper_trades()
        bag = self.bag(trades)
        open_trades = [t for t in trades if t.is_open]
        try:
            daily = self.daily({t.pair for t in open_trades}, len(open_trades),
                               bag["balance_eur"])
        except OKXError as e:
            self.problems.append(f"daily scan failed: {e}")
            daily = self.prev.get("daily", {})

        if self.cfg["alerts"].get("heartbeat"):
            self.alerts.append(Alert(f"heartbeat:{self.now.date().isoformat()}", (
                f"✅ Daily check-in: the job is running.\n"
                + (f"Core {_eur(core['total_eur'])} · " if core else "")
                + f"Satellite {_eur(bag['balance_eur'])} · open trades {len(open_trades)}")))

        state = {
            "updated_at": alerts.stamp(self.now),
            "source": self.client.hosts[0],
            "prices": self.prices,
            "core": core if core is not None else self.prev.get("core"),
            "core_stale": core is None,
            "satellite": bag,
            "daily": daily,
            "problems": self.problems,
        }
        return state, changed


def run(client: OKXClient, cfg: dict, data_dir: Path, now: datetime,
        send: Callable[[str], bool], dry_run: bool = False) -> dict:
    r = Run(client, cfg, data_dir, now)
    state, changed = r.evaluate()

    alert_path = data_dir / "alert_state.json"
    alert_state = alerts.load_state(alert_path)
    if dry_run:
        alert_state = copy.deepcopy(alert_state)
    else:
        # trade exits first: the CSV is the record, alerts can be retried
        trades_file.write(data_dir / "paper_trades.csv", changed)
    new = alerts.select(r.alerts, alert_state, r.evaluated, now)
    failed = alerts.deliver(new, alert_state, send, now)
    state["recent_alerts"] = list(reversed(alert_state["history"][-RECENT_ALERTS:]))
    state["pending_alerts"] = len(failed)
    if not dry_run:
        (data_dir / "state.json").write_text(
            json.dumps(state, indent=2, ensure_ascii=False) + "\n")
        alerts.save_state(alert_path, alert_state)
    return state


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="print alerts instead of sending; write no files")
    args = parser.parse_args()

    cfg = load_config()
    send = alerts.print_sender if args.dry_run else alerts.telegram_sender()
    state = run(OKXClient.from_config(cfg), cfg, ROOT / "data",
                datetime.now(timezone.utc), send, dry_run=args.dry_run)
    print(f"\n{state['updated_at']}  problems: {len(state.get('problems', []))}  "
          f"failed alerts: {state.get('pending_alerts', 0)}")
    for p in state.get("problems", []):
        print("  -", p)
    if state.get("pending_alerts"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
