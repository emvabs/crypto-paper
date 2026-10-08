"""Public OKX market data. No API keys, no account access.

Endpoints (checked against eea.okx.com and www.okx.com, October 2026):
  GET /api/v5/public/instruments?instType=SPOT
  GET /api/v5/market/tickers?instType=SPOT
  GET /api/v5/market/ticker?instId=BTC-USDC
  GET /api/v5/market/candles?instId=BTC-USDC&bar=1Dutc&limit=N
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx


class OKXError(RuntimeError):
    pass


@dataclass(frozen=True)
class Ticker:
    inst_id: str
    last: float
    bid: float
    ask: float
    vol_quote_24h: float
    ts: int  # ms since epoch

    @property
    def spread(self) -> float:
        """Bid-ask spread as a fraction of the mid price."""
        mid = (self.bid + self.ask) / 2
        return (self.ask - self.bid) / mid if mid > 0 else float("inf")


@dataclass(frozen=True)
class Candle:
    ts: int  # bar open, ms since epoch (UTC day for bar=1Dutc)
    open: float
    high: float
    low: float
    close: float
    vol_quote: float
    confirmed: bool


def _float(value: str) -> float:
    return float(value) if value not in ("", None) else 0.0


class OKXClient:
    def __init__(self, base_url: str, fallback_base_url: str | None = None,
                 timeout_seconds: float = 10, max_retries: int = 3,
                 backoff_seconds: float = 1.0):
        self.hosts = [base_url] + ([fallback_base_url] if fallback_base_url else [])
        self.max_retries = max_retries
        self.backoff = backoff_seconds
        self.http = httpx.Client(timeout=timeout_seconds,
                                 headers={"User-Agent": "core-satellite-paper/1.0"})

    @classmethod
    def from_config(cls, config: dict) -> "OKXClient":
        return cls(**config["okx"])

    def _get(self, path: str, params: dict) -> list:
        """GET with retries and exponential backoff; tries the fallback host last."""
        last_error: Exception | None = None
        for host in self.hosts:
            for attempt in range(self.max_retries):
                try:
                    r = self.http.get(host + path, params=params)
                    if r.status_code == 429 or r.status_code >= 500:
                        raise OKXError(f"HTTP {r.status_code}")
                    r.raise_for_status()
                    body = r.json()
                    if body.get("code") != "0":
                        raise OKXError(f"OKX code {body.get('code')}: {body.get('msg')}")
                    return body["data"]
                except (httpx.HTTPError, OKXError, ValueError) as e:
                    last_error = e
                    time.sleep(self.backoff * 2 ** attempt)
        raise OKXError(f"{path} {params} failed: {last_error}")

    @staticmethod
    def _ticker(d: dict) -> Ticker:
        return Ticker(inst_id=d["instId"], last=_float(d["last"]),
                      bid=_float(d["bidPx"]), ask=_float(d["askPx"]),
                      vol_quote_24h=_float(d["volCcy24h"]), ts=int(d["ts"]))

    def ticker(self, inst_id: str) -> Ticker:
        data = self._get("/api/v5/market/ticker", {"instId": inst_id})
        if not data:
            raise OKXError(f"no ticker for {inst_id}")
        return self._ticker(data[0])

    def tickers(self) -> dict[str, Ticker]:
        """All spot tickers in one call, keyed by instId."""
        data = self._get("/api/v5/market/tickers", {"instType": "SPOT"})
        return {d["instId"]: self._ticker(d) for d in data}

    def instruments(self) -> list[dict]:
        """Spot instruments that are live for trading."""
        data = self._get("/api/v5/public/instruments", {"instType": "SPOT"})
        return [d for d in data if d.get("state") == "live"]

    def daily_candles(self, inst_id: str, limit: int = 100,
                      confirmed_only: bool = True) -> list[Candle]:
        """UTC daily candles, oldest first. OKX caps one page at 300 bars."""
        data = self._get("/api/v5/market/candles",
                         {"instId": inst_id, "bar": "1Dutc", "limit": str(min(limit, 300))})
        candles = [Candle(ts=int(c[0]), open=_float(c[1]), high=_float(c[2]),
                          low=_float(c[3]), close=_float(c[4]),
                          vol_quote=_float(c[7]), confirmed=c[8] == "1")
                   for c in data]
        candles.sort(key=lambda c: c.ts)
        return [c for c in candles if c.confirmed] if confirmed_only else candles
