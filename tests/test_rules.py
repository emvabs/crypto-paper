from datetime import date, datetime, timezone

import pytest

from src import rules
from src.okx_client import Candle

SAT = {"stop_loss": 0.08, "target1": 0.10, "target2": 0.20, "trailing_stop": 0.08,
       "trailing_starts_after": "T2", "fee_rate": 0.0, "floor_eur": 10,
       "sweep_above_eur": 25, "max_open_trades": 3}
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


def candle(high, close, ts=0):
    return Candle(ts=ts, open=close, high=high, low=close, close=close,
                  vol_quote=0, confirmed=True)


def trade(entry=100.0, size=6.0):
    return rules.PaperTrade(id="1", pair="SOL-USDC", entry_price=entry, size_eur=size)


def run(t, prices, sat=SAT):
    events = []
    for p in prices:
        t, ev = rules.step(t, p, NOW, sat)
        events += ev
    return t, events


# ---------------------------------------------------------------- core

TARGETS = {"BTC": 0.50, "ETH": 0.30, "USDC": 0.20}


def test_drift_on_target_is_in_band():
    d = rules.core_drift({"BTC": 1, "ETH": 3, "USDC": 20}, {"BTC": 50, "ETH": 10, "USDC": 1},
                         TARGETS, 0.05)
    assert [x.out_of_band for x in d] == [False, False, False]
    assert all(abs(x.rebalance_eur) < 1e-9 for x in d)


def test_drift_band_edges_are_inside():
    # BTC exactly 55%, ETH 25%, USDC 20%
    d = {x.asset: x for x in rules.core_drift(
        {"BTC": 55, "ETH": 25, "USDC": 20}, {"BTC": 1, "ETH": 1, "USDC": 1}, TARGETS, 0.05)}
    assert not d["BTC"].out_of_band and not d["ETH"].out_of_band


def test_drift_out_of_band_gives_rebalance_amounts():
    # €100 total: BTC 60, ETH 25, USDC 15
    d = {x.asset: x for x in rules.core_drift(
        {"BTC": 60, "ETH": 25, "USDC": 15}, {"BTC": 1, "ETH": 1, "USDC": 1}, TARGETS, 0.05)}
    assert d["BTC"].out_of_band and d["BTC"].rebalance_eur == pytest.approx(-10)
    assert not d["ETH"].out_of_band and d["ETH"].rebalance_eur == pytest.approx(5)
    assert not d["USDC"].out_of_band and d["USDC"].rebalance_eur == pytest.approx(5)
    assert sum(x.rebalance_eur for x in d.values()) == pytest.approx(0)


def test_drift_with_nothing_held():
    d = rules.core_drift({}, {"BTC": 1, "ETH": 1, "USDC": 1}, TARGETS, 0.05)
    assert all(x.weight == 0 and x.rebalance_eur == 0 for x in d)


def test_rebalance_period():
    months = [1, 4, 7, 10]
    assert rules.rebalance_period(date(2026, 10, 7), months) == "2026-10"
    assert rules.rebalance_period(date(2026, 11, 1), months) is None


# ----------------------------------------------------------- breakout

def test_breakout_needs_close_above_prior_high():
    bars = [candle(high=10, close=9) for _ in range(20)]
    assert rules.breakout(bars + [candle(high=12, close=10.5)], 20)
    assert not rules.breakout(bars + [candle(high=12, close=10)], 20)  # equal is not above


def test_breakout_ignores_highs_older_than_lookback():
    bars = [candle(high=50, close=9)] + [candle(high=10, close=9) for _ in range(20)]
    assert rules.breakout(bars + [candle(high=11, close=10.5)], 20)


def test_breakout_needs_enough_history():
    bars = [candle(high=10, close=9) for _ in range(19)]
    assert not rules.breakout(bars + [candle(high=12, close=11)], 20)


def test_distance_from_high():
    bars = [candle(high=10, close=9) for _ in range(20)] + [candle(high=9.6, close=9.5)]
    assert rules.distance_from_high(bars, 20) == pytest.approx(-0.05)
    assert rules.distance_from_high(bars[:5], 20) is None


def test_regime_filter():
    assert rules.regime_allows_entries([1.0] * 199 + [2.0], 200) is True
    assert rules.regime_allows_entries([2.0] * 199 + [1.0], 200) is False
    assert rules.regime_allows_entries([1.0] * 50, 200) is None


# ------------------------------------------------------- stops, targets

def test_levels():
    lv = rules.levels(100, SAT)
    assert (lv.stop, lv.target1, lv.target2) == pytest.approx((92, 110, 120))


def test_third_pnl_with_fees():
    assert rules.third_pnl(6, 100, 110, 0) == pytest.approx(0.2)
    assert rules.third_pnl(6, 100, 110, 0.001) == pytest.approx(2 * (1.1 * 0.999 ** 2 - 1))
    assert rules.third_pnl(6, 100, 100, 0.001) < 0  # round trip costs the fee


def test_price_sanity():
    assert rules.price_is_sane(None, 100, 0.25)
    assert rules.price_is_sane(100, 120, 0.25)
    assert not rules.price_is_sane(100, 130, 0.25)
    assert not rules.price_is_sane(100, 0, 0.25)
    assert not rules.price_is_sane(100, None, 0.25)


# ------------------------------------------------------- paper exits

def test_stop_before_any_target_closes_everything():
    t, ev = run(trade(), [101, 95, 92])
    assert not t.is_open and t.date_closed == "2026-10-07"
    assert [(e.third, e.reason) for e in ev] == [(1, "stop"), (2, "stop"), (3, "stop")]
    assert all(e.price == 92 and e.rule_level == pytest.approx(92) for e in ev)
    assert t.exit3_reason == "stop"
    assert t.realized_pnl(0) == pytest.approx(6 * -0.08)


def test_fill_is_the_observed_price_not_the_level():
    t, ev = run(trade(), [85])  # gapped below the 92 stop
    assert all(e.price == 85 for e in ev)
    assert t.realized_pnl(0) == pytest.approx(6 * -0.15)


def test_t1_then_stop():
    t, ev = run(trade(), [111, 105, 91])
    assert [(e.third, e.reason, e.price) for e in ev] == [
        (1, "target1", 111), (2, "stop", 91), (3, "stop", 91)]
    assert not t.is_open
    assert t.realized_pnl(0) == pytest.approx(2 * 0.11 + 2 * 2 * -0.09)


def test_t1_t2_then_trailing_stop():
    t, ev = run(trade(), [110, 121, 130, 125, 119.5])
    # trailing high 130 → trail level 119.6
    assert [(e.third, e.reason) for e in ev] == [
        (1, "target1"), (2, "target2"), (3, "trailing_stop")]
    assert ev[2].price == 119.5 and ev[2].rule_level == pytest.approx(119.6)
    assert t.trailing_high == 130 and t.exit3_reason == "trailing_stop"
    assert not t.is_open


def test_trailing_not_active_before_t2():
    # 100 → 115 (T1) → 106: 8% below 115 is 105.8, not hit; trailing only after T2 anyway
    t, ev = run(trade(), [115, 105])
    assert [(e.third, e.reason) for e in ev] == [(1, "target1")]
    assert t.is_open and t.remaining == 2


def test_trailing_from_entry_when_configured():
    t, ev = run(trade(), [109, 100], sat={**SAT, "trailing_starts_after": "entry"})
    assert [(e.third, e.reason) for e in ev] == [(3, "trailing_stop")]
    assert t.remaining == 2


def test_gap_through_both_targets_in_one_run():
    t, ev = run(trade(), [125])
    assert [(e.third, e.reason, e.price) for e in ev] == [
        (1, "target1", 125), (2, "target2", 125)]
    assert t.remaining == 1 and t.trailing_high == 125


def test_trade_that_stays_open():
    t, ev = run(trade(), [100, 104, 97, 108, 93])
    assert ev == [] and t.is_open and t.trailing_high == 108


def test_closed_trade_is_left_alone():
    t, _ = run(trade(), [80])
    t2, ev = rules.step(t, 200, NOW, SAT)
    assert ev == [] and t2 == t


def test_stop_still_applies_after_t2_below_trailing():
    # after T2 at 121, a crash to 90 is below both stop (92) and trail; stop wins
    t, ev = run(trade(), [121, 90])
    assert ev[-1].reason == "stop" and t.exit3_reason == "stop"


# ------------------------------------------------------- CSV rows

def row(**kw):
    base = {c: "" for c in rules.COLUMNS}
    base.update(id="7", date_opened="2026-10-01", pair="sol-usdc",
                entry_price="100", size_eur="5", notes="my note")
    base.update(kw)
    return base


def test_parse_and_write_back_preserves_user_columns():
    t, err = rules.parse_trade(row(entry_price="100,5"))
    assert err is None and t.pair == "SOL-USDC" and t.entry_price == 100.5
    t, _ = rules.step(t, 120, NOW, SAT)
    out = rules.trade_to_row(t)
    for c in rules.USER_COLUMNS:
        assert out[c] == row(entry_price="100,5")[c]
    assert out["exit1_price"] == "120" and out["exit1_time"] == "2026-10-07T12:00:00Z"
    assert out["status"] == "Open" and out["trailing_high"] == "120"


def test_round_trip_of_script_columns():
    t, _ = rules.parse_trade(row())
    t, _ = run(t, [125, 140, 128])
    again, _ = rules.parse_trade(rules.trade_to_row(t))
    assert again.exits == t.exits and again.exit3_reason == "trailing_stop"
    assert not again.is_open


@pytest.mark.parametrize("bad", [
    {"entry_price": ""}, {"entry_price": "abc"}, {"entry_price": "-1"},
    {"size_eur": "0"}, {"pair": "SOLUSDC"}, {"id": ""},
])
def test_invalid_rows_are_reported_not_raised(bad):
    t, err = rules.parse_trade(row(**bad))
    assert t is None and err


# ------------------------------------------------------- satellite bag

def test_balance_and_win_rate():
    win, _ = run(trade(size=6), [125, 140, 128])        # +25%, +25%, +28% thirds
    loss, _ = run(trade(size=6), [90])                   # -10% on all
    still_open, _ = run(trade(size=6), [111])            # T1 only
    trades = [win, loss, still_open]
    realized = 2 * (0.25 + 0.25 + 0.28) + 6 * -0.10 + 2 * 0.11
    assert rules.satellite_balance(20, trades, 0) == pytest.approx(20 + realized)
    assert rules.satellite_balance(20, trades, 0, transfers_eur=-5) == pytest.approx(15 + realized)
    assert rules.win_rate(trades, 0) == pytest.approx(0.5)
    assert rules.win_rate([still_open], 0) is None


def test_unrealized_pnl_counts_remaining_thirds():
    t, _ = run(trade(size=6), [111])
    assert t.unrealized_pnl(105, 0) == pytest.approx(2 * 2 * 0.05)


@pytest.mark.parametrize("balance, status", [
    (9.99, "pause"), (10, "ok"), (25, "ok"), (25.01, "sweep")])
def test_floor_and_sweep(balance, status):
    assert rules.bag_status(balance, SAT) == status


def test_entry_block():
    assert rules.entry_block(0, 20, SAT) is None
    assert rules.entry_block(0, 9, SAT) == "satellite below floor"
    assert rules.entry_block(3, 20, SAT) == "max open trades reached"
    assert "regime" in rules.entry_block(0, 20, SAT, regime_ok=False)
    assert rules.entry_block(0, 20, SAT, regime_ok=None) is None
