"""Whole runs against a fake OKX client and a temporary data folder."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from src import main, rules
from src.okx_client import Candle, OKXError, Ticker

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
DAY_MS = 86_400_000
YESTERDAY_MS = int(datetime(2026, 10, 6, tzinfo=timezone.utc).timestamp() * 1000)


def config():
    cfg = main.load_config()
    cfg["satellite"]["fee_rate"] = 0.0
    cfg["watchlist"]["size_min"] = 1
    return cfg


class FakeOKX:
    hosts = ["https://fake"]

    def __init__(self, prices, breakout_pairs=(), fail=False):
        self.prices = dict(prices)
        self.breakout_pairs = set(breakout_pairs)
        self.fail = fail
        self.candle_calls = 0

    def tickers(self):
        if self.fail:
            raise OKXError("timeout")
        ts = int(NOW.timestamp() * 1000)
        return {p: Ticker(p, last=v, bid=v * 0.9999, ask=v * 1.0001,
                          vol_quote_24h=1e6, ts=ts) for p, v in self.prices.items()}

    def instruments(self):
        return [{"instId": p, "baseCcy": p.split("-")[0], "quoteCcy": p.split("-")[1],
                 "instCategory": "1"} for p in self.prices]

    def daily_candles(self, pair, limit=100, confirmed_only=True):
        self.candle_calls += 1
        n = min(limit, 250)
        bars = [Candle(YESTERDAY_MS - (n - 1 - i) * DAY_MS, 10, 10, 9, 9.5, 1e5, True)
                for i in range(n)]
        if pair in self.breakout_pairs:
            bars[-1] = Candle(YESTERDAY_MS, 10, 11, 10, 10.8, 1e5, True)
        return bars


PRICES = {"USDC-EUR": 0.9, "BTC-USDC": 80_000, "ETH-USDC": 2_500,
          "SOL-USDC": 111, "XRP-USDC": 2.0}


@pytest.fixture
def data(tmp_path):
    (tmp_path / "core_holdings.json").write_text(
        json.dumps({"BTC": 0.000556, "ETH": 0.010667, "USDC": 17.78}))  # €80 at 50/30/20
    header = ",".join(rules.COLUMNS)
    row = "1,2026-10-01,SOL-USDC,100,6" + "," * 10 + ",first trade"
    (tmp_path / "paper_trades.csv").write_text(header + "\n" + row + "\n")
    return tmp_path


def go(client, data, now=NOW, dry_run=False, cfg=None):
    sent = []
    state = main.run(client, cfg or config(), data, now,
                     lambda t: sent.append(t) or True, dry_run=dry_run)
    return state, sent


def test_run_records_exit_writes_state_and_alerts_once(data):
    state, sent = go(FakeOKX(PRICES), data)
    row = (data / "paper_trades.csv").read_text().splitlines()[1].split(",")
    assert row[5] == "111" and row[-1] == "first trade"
    assert any("Target 1 hit — SOL-USDC" in t and "Third 1/3 sold at 111.00 (T1 110.00): +0.22 €" in t
               for t in sent)
    saved = json.loads((data / "state.json").read_text())
    assert saved["satellite"]["open_trades"][0]["thirds_sold"] == 1
    assert saved["core"]["total_eur"] == pytest.approx(80, abs=0.5)
    assert saved["daily"]["candle_date"] == "2026-10-06"
    assert saved["recent_alerts"][0]["text"] == sent[-1]

    _, again = go(FakeOKX(PRICES), data, now=NOW + timedelta(minutes=15))
    assert again == []


def test_drift_alert_with_amounts(data):
    # BTC €50.4 of €90.4 = 55.8%; ETH 26.5% and USDC 17.7% stay inside their bands
    (data / "core_holdings.json").write_text(
        json.dumps({"BTC": 0.0007, "ETH": 0.010667, "USDC": 17.78}))
    _, sent = go(FakeOKX(PRICES), data)
    drift = [t for t in sent if t.startswith("⚖️")]
    assert len(drift) == 1 and "BTC at 55.8%" in drift[0] and "ETH at" not in drift[0]
    assert "BTC sell 5.20 €" in drift[0] and "ETH buy" in drift[0]


def test_drift_lists_every_asset_out_of_band_in_one_message(data):
    (data / "core_holdings.json").write_text(json.dumps({"BTC": 0.002, "ETH": 0, "USDC": 0}))
    _, sent = go(FakeOKX(PRICES), data)
    drift = [t for t in sent if t.startswith("⚖️")]
    assert len(drift) == 1
    assert all(f"{a} at" in drift[0] for a in ("BTC", "ETH", "USDC"))


def test_implausible_jump_skips_the_pair_for_one_run(data):
    go(FakeOKX({**PRICES, "SOL-USDC": 101}), data)
    # 101 → 60 is a 40% drop in 15 minutes: no stop, a data alert instead
    state, sent = go(FakeOKX({**PRICES, "SOL-USDC": 60}), data, now=NOW + timedelta(minutes=15))
    assert any(t.startswith("⚠️ Data problem — SOL-USDC") for t in sent)
    assert not any("Stop-loss" in t for t in sent)
    assert state["satellite"]["open_trades"][0]["thirds_sold"] == 0
    # the same price on the next run is believed
    _, sent = go(FakeOKX({**PRICES, "SOL-USDC": 60}), data, now=NOW + timedelta(minutes=30))
    stops = [t for t in sent if "Stop-loss hit" in t]
    assert len(stops) == 1  # one message for the three thirds
    assert all(f"Third {n}/3 sold at 60 (stop 92)" in stops[0] for n in (1, 2, 3))
    assert "Trade closed. Total P&L: -2.40 €" in stops[0]


def test_dry_run_writes_nothing(data):
    before = {p.name: p.read_text() for p in data.iterdir()}
    _, sent = go(FakeOKX(PRICES), data, dry_run=True)
    assert sent  # printed instead
    assert {p.name: p.read_text() for p in data.iterdir()} == before


def test_breakout_alert_once_per_candle(data):
    client = FakeOKX(PRICES, breakout_pairs={"XRP-USDC"})
    state, sent = go(client, data)
    assert any(t.startswith("🚀 Breakout — XRP-USDC") for t in sent)
    assert not any("Breakout — BTC" in t or "Breakout — ETH" in t for t in sent)
    assert [w["pair"] for w in state["daily"]["watchlist"] if w["breakout"]] == ["XRP-USDC"]

    calls = client.candle_calls
    _, sent = go(client, data, now=NOW + timedelta(minutes=15))
    assert sent == [] and client.candle_calls == calls + 1  # only the BTC date check


def test_no_breakout_alert_below_floor(data):
    cfg = config()
    cfg["satellite"]["floor_eur"] = 50
    state, sent = go(FakeOKX(PRICES, breakout_pairs={"XRP-USDC"}), data, cfg=cfg)
    assert not any(t.startswith("🚀") for t in sent)
    assert any(t.startswith("🛑 Satellite balance") for t in sent)
    assert state["daily"]["entry_block"] == "satellite below floor"


def test_bad_rows_reported_once(data):
    with (data / "paper_trades.csv").open("a") as f:
        f.write("2,2026-10-02,XRP-USDC,,5" + "," * 11 + "\n")
    _, sent = go(FakeOKX(PRICES), data)
    assert any("1 row(s) skipped" in t and "bad entry_price" in t for t in sent)
    _, sent = go(FakeOKX(PRICES), data, now=NOW + timedelta(minutes=15))
    assert not any("skipped" in t for t in sent)


def test_okx_unreachable_sends_one_data_alert_and_keeps_files(data):
    go(FakeOKX(PRICES), data)
    trades_before = (data / "paper_trades.csv").read_text()
    state, sent = go(FakeOKX(PRICES, fail=True), data, now=NOW + timedelta(minutes=15))
    assert len(sent) == 1 and "OKX unreachable" in sent[0]
    assert (data / "paper_trades.csv").read_text() == trades_before
    assert state["core"] is not None  # last known values kept for the dashboard
