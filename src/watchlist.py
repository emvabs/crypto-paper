"""The satellite watchlist: the most traded USDC spot pairs on OKX Europe.

Ranking uses the average daily quote volume of the last `volume_days`
confirmed daily candles. Fetching candles for all ~300 USDC pairs would be
slow, so the 24h ticker volume picks the candidates first.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from src.okx_client import Candle, OKXClient, Ticker


@dataclass(frozen=True)
class WatchItem:
    pair: str
    avg_quote_volume: float
    spread: float
    history_days: int


def candidates(instruments: Sequence[dict], tickers: dict[str, Ticker],
               cfg: dict, core_bases: Sequence[str] = ()) -> list[Ticker]:
    """Live pairs in the quote currency that pass the static filters, by 24h volume."""
    quote = cfg["quote"]
    excluded = {b.upper() for b in cfg["exclude_bases"]}
    if cfg.get("exclude_core", True):
        excluded |= {b.upper() for b in core_bases}
    categories = {str(c) for c in cfg.get("inst_categories", ["1"])}
    result = []
    for inst in instruments:
        if inst.get("quoteCcy") != quote or inst.get("baseCcy", "").upper() in excluded:
            continue
        if str(inst.get("instCategory", "1")) not in categories:
            continue
        t = tickers.get(inst["instId"])
        if t is None or t.last <= 0 or t.bid <= 0 or t.ask <= 0:
            continue
        if t.spread > cfg["max_spread"]:
            continue
        result.append(t)
    result.sort(key=lambda t: t.vol_quote_24h, reverse=True)
    return result


def rank(candidates_: Sequence[Ticker], candles: dict[str, Sequence[Candle]],
         cfg: dict) -> list[WatchItem]:
    """Rank candidates by average daily quote volume; drop short histories."""
    items = []
    for t in candidates_:
        bars = [c for c in candles.get(t.inst_id, []) if c.confirmed]
        if len(bars) < cfg["min_history_days"]:
            continue
        recent = bars[-cfg["volume_days"]:]
        avg = sum(c.vol_quote for c in recent) / len(recent)
        items.append(WatchItem(t.inst_id, avg, t.spread, len(bars)))
    items.sort(key=lambda i: i.avg_quote_volume, reverse=True)
    return items[:cfg["size_max"]]


def build(client: OKXClient, cfg: dict, core_bases: Sequence[str] = (),
          history_limit: int | None = None,
          fetch: Callable[[str, int], list[Candle]] | None = None
          ) -> tuple[list[WatchItem], dict[str, list[Candle]]]:
    """Fetch, filter and rank. Returns the watchlist and the candles it fetched.

    Candles are fetched for up to three times `size_max` candidates, enough
    history for both the volume average and the age filter.
    """
    fetch = fetch or client.daily_candles
    limit = history_limit or max(cfg["min_history_days"], cfg["volume_days"]) + 10
    pool = candidates(client.instruments(), client.tickers(), cfg, core_bases)
    pool = pool[:cfg["size_max"] * 3]
    candles = {t.inst_id: fetch(t.inst_id, limit) for t in pool}
    return rank(pool, candles, cfg), candles
