from src import watchlist
from src.okx_client import Candle, Ticker

CFG = {"quote": "USDC", "size_min": 2, "size_max": 3, "volume_days": 7,
       "min_history_days": 30, "max_spread": 0.002,
       "exclude_bases": ["USDT", "EURC"], "exclude_core": True, "inst_categories": ["1"]}


def inst(base, quote="USDC", category="1"):
    return {"instId": f"{base}-{quote}", "baseCcy": base, "quoteCcy": quote,
            "instCategory": category}


def tick(pair, vol=1000.0, bid=99.95, ask=100.05):
    return Ticker(pair, last=100, bid=bid, ask=ask, vol_quote_24h=vol, ts=0)


def bars(n, vol=1.0, last_vols=None):
    out = [Candle(i, 1, 1, 1, 1, vol, True) for i in range(n)]
    for k, v in enumerate(last_vols or []):
        out[n - len(last_vols) + k] = Candle(n - len(last_vols) + k, 1, 1, 1, 1, v, True)
    return out


def test_candidates_apply_static_filters():
    instruments = [inst("SOL"), inst("USDT"), inst("BTC"), inst("XRP", "EUR"),
                   inst("XMSTR", category="3"), inst("WIDE"), inst("NEAR")]
    tickers = {i["instId"]: tick(i["instId"]) for i in instruments}
    tickers["WIDE-USDC"] = tick("WIDE-USDC", bid=99, ask=101)  # 2% spread
    tickers["NEAR-USDC"] = tick("NEAR-USDC", vol=5000)
    got = [t.inst_id for t in watchlist.candidates(instruments, tickers, CFG, ["BTC", "ETH"])]
    assert got == ["NEAR-USDC", "SOL-USDC"]


def test_core_coins_kept_when_not_excluded():
    instruments = [inst("BTC")]
    tickers = {"BTC-USDC": tick("BTC-USDC")}
    cfg = {**CFG, "exclude_core": False}
    assert len(watchlist.candidates(instruments, tickers, cfg, ["BTC"])) == 1


def test_missing_or_zero_ticker_is_skipped():
    instruments = [inst("SOL"), inst("DEAD")]
    tickers = {"DEAD-USDC": tick("DEAD-USDC", bid=0)}
    assert watchlist.candidates(instruments, tickers, CFG) == []


def test_rank_uses_seven_day_average_and_history_filter():
    pool = [tick("A-USDC"), tick("B-USDC"), tick("C-USDC"), tick("D-USDC"), tick("NEW-USDC")]
    candles = {
        # A: huge volume long ago, small recently
        "A-USDC": [Candle(i, 1, 1, 1, 1, 1000, True) for i in range(30)] + bars(7, vol=1),
        "B-USDC": bars(40, vol=50),
        "C-USDC": bars(40, vol=1, last_vols=[70] * 7),
        "D-USDC": bars(40, vol=10),
        "NEW-USDC": bars(20, vol=10_000),  # too young
    }
    got = watchlist.rank(pool, candles, CFG)
    assert [i.pair for i in got] == ["C-USDC", "B-USDC", "D-USDC"]  # capped at size_max
    assert got[0].avg_quote_volume == 70 and got[0].history_days == 40


def test_unconfirmed_candle_not_counted():
    c = bars(30)
    c[-1] = Candle(29, 1, 1, 1, 1, 1, False)
    assert watchlist.rank([tick("A-USDC")], {"A-USDC": c}, CFG) == []
