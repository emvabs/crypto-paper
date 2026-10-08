"""Strategy rules as pure functions: no network, no files, no clock.

Money conventions:
- Satellite sizes, balances and P&L are in EUR. A third's P&L is its EUR size
  times the pair's return (in the pair's quote currency), after the fee on
  both the buy and the sell. USDC/EUR moves are ignored.
- Ratios are fractions: 0.08 means 8%.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Sequence

from src.okx_client import Candle

# --------------------------------------------------------------------- core


@dataclass(frozen=True)
class Drift:
    asset: str
    value_eur: float
    weight: float
    target: float
    low: float
    high: float
    rebalance_eur: float  # > 0 buy, < 0 sell, to return to target

    @property
    def out_of_band(self) -> bool:
        return not (self.low <= self.weight <= self.high)


def core_drift(holdings: dict[str, float], prices_eur: dict[str, float],
               targets: dict[str, float], band: float) -> list[Drift]:
    """Weight of each core asset against its target and band (target ± band).

    Weights are rounded to 1e-9 before comparing, so a weight exactly on the
    band edge counts as inside it.
    """
    values = {a: holdings.get(a, 0.0) * prices_eur[a] for a in targets}
    total = sum(values.values())
    result = []
    for asset, target in targets.items():
        weight = values[asset] / total if total > 0 else 0.0
        result.append(Drift(asset=asset, value_eur=values[asset],
                            weight=round(weight, 9), target=target,
                            low=round(target - band, 9), high=round(target + band, 9),
                            rebalance_eur=(target - weight) * total))
    return result


def rebalance_period(today: date, months: Sequence[int]) -> str | None:
    """'YYYY-MM' when today falls in a quarterly rebalance month, else None.

    Alerts de-duplicate on this key, so the reminder goes out once per quarter.
    """
    return f"{today.year}-{today.month:02d}" if today.month in months else None


# --------------------------------------------------------------- satellite


def breakout(candles: Sequence[Candle], lookback: int) -> bool:
    """The latest candle closes above the highest high of the `lookback` before it.

    `candles` must be confirmed daily candles, oldest first.
    """
    if len(candles) < lookback + 1:
        return False
    prior_high = max(c.high for c in candles[-lookback - 1:-1])
    return candles[-1].close > prior_high


def distance_from_high(candles: Sequence[Candle], lookback: int) -> float | None:
    """Latest close relative to the prior `lookback`-day high (-0.03 = 3% below)."""
    if len(candles) < lookback + 1:
        return None
    prior_high = max(c.high for c in candles[-lookback - 1:-1])
    return candles[-1].close / prior_high - 1


def sma(values: Sequence[float], n: int) -> float | None:
    return sum(values[-n:]) / n if len(values) >= n else None


def regime_allows_entries(closes: Sequence[float], sma_days: int) -> bool | None:
    """True if the last close is at or above its SMA, None without enough history."""
    avg = sma(closes, sma_days)
    return None if avg is None else closes[-1] >= avg


@dataclass(frozen=True)
class Levels:
    stop: float
    target1: float
    target2: float


def levels(entry: float, sat: dict) -> Levels:
    return Levels(stop=entry * (1 - sat["stop_loss"]),
                  target1=entry * (1 + sat["target1"]),
                  target2=entry * (1 + sat["target2"]))


def trailing_level(trailing_high: float, sat: dict) -> float:
    return trailing_high * (1 - sat["trailing_stop"])


def third_pnl(size_eur: float, entry: float, exit_price: float, fee: float) -> float:
    """Realized EUR P&L of one third, with the fee charged on buy and sell."""
    return size_eur / 3 * (exit_price / entry * (1 - fee) ** 2 - 1)


def price_is_sane(previous: float | None, current: float | None,
                  max_move: float) -> bool:
    """False for a missing or non-positive price, or an implausible jump."""
    if current is None or current <= 0:
        return False
    if previous is None or previous <= 0:
        return True
    return abs(current / previous - 1) <= max_move


# ---------------------------------------------------------- paper trades

USER_COLUMNS = ("id", "date_opened", "pair", "entry_price", "size_eur", "notes")
SCRIPT_COLUMNS = ("exit1_price", "exit1_time", "exit2_price", "exit2_time",
                  "exit3_price", "exit3_time", "exit3_reason", "trailing_high",
                  "date_closed", "status")
COLUMNS = ("id", "date_opened", "pair", "entry_price", "size_eur",
           "exit1_price", "exit1_time", "exit2_price", "exit2_time",
           "exit3_price", "exit3_time", "exit3_reason", "trailing_high",
           "date_closed", "status", "notes")

OPEN, CLOSED = "Open", "Closed"


@dataclass(frozen=True)
class Exit:
    price: float
    time: str  # ISO 8601, UTC


@dataclass(frozen=True)
class PaperTrade:
    id: str
    pair: str
    entry_price: float
    size_eur: float
    exits: tuple[Exit | None, Exit | None, Exit | None] = (None, None, None)
    exit3_reason: str = ""
    trailing_high: float | None = None
    date_closed: str = ""
    row: dict = field(default_factory=dict, compare=False)  # the CSV row as read

    @property
    def is_open(self) -> bool:
        return any(e is None for e in self.exits)

    @property
    def remaining(self) -> int:
        return sum(e is None for e in self.exits)

    def realized_pnl(self, fee: float) -> float:
        return sum(third_pnl(self.size_eur, self.entry_price, e.price, fee)
                   for e in self.exits if e is not None)

    def unrealized_pnl(self, price: float, fee: float) -> float:
        return self.remaining * third_pnl(self.size_eur, self.entry_price, price, fee)


def parse_number(value) -> float | None:
    try:
        n = float(str(value).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None
    return n if n == n else None  # NaN


def parse_trade(row: dict) -> tuple[PaperTrade | None, str | None]:
    """A trade from a CSV row, or (None, reason) when the entry data is unusable."""
    tid = (row.get("id") or "").strip()
    pair = (row.get("pair") or "").strip().upper()
    entry = parse_number(row.get("entry_price"))
    size = parse_number(row.get("size_eur"))
    problems = []
    if not tid:
        problems.append("no id")
    if not pair or "-" not in pair:
        problems.append(f"bad pair {row.get('pair')!r}")
    if entry is None or entry <= 0:
        problems.append(f"bad entry_price {row.get('entry_price')!r}")
    if size is None or size <= 0:
        problems.append(f"bad size_eur {row.get('size_eur')!r}")
    if problems:
        return None, f"row {tid or '?'}: " + ", ".join(problems)

    exits = []
    for i in (1, 2, 3):
        price = parse_number(row.get(f"exit{i}_price"))
        exits.append(Exit(price, row.get(f"exit{i}_time") or "") if price else None)
    return PaperTrade(id=tid, pair=pair, entry_price=entry, size_eur=size,
                      exits=tuple(exits),
                      exit3_reason=row.get("exit3_reason") or "",
                      trailing_high=parse_number(row.get("trailing_high")),
                      date_closed=row.get("date_closed") or "",
                      row=dict(row)), None


def trade_to_row(trade: PaperTrade) -> dict:
    """The CSV row: user columns exactly as read, script columns from the trade."""
    row = {c: trade.row.get(c, "") for c in COLUMNS}
    for i, e in enumerate(trade.exits, start=1):
        row[f"exit{i}_price"] = "" if e is None else f"{e.price:.10g}"
        row[f"exit{i}_time"] = "" if e is None else e.time
    row["exit3_reason"] = trade.exit3_reason
    row["trailing_high"] = "" if trade.trailing_high is None else f"{trade.trailing_high:.10g}"
    row["date_closed"] = trade.date_closed
    row["status"] = OPEN if trade.is_open else CLOSED
    return row


@dataclass(frozen=True)
class ExitEvent:
    trade_id: str
    pair: str
    third: int          # 1, 2 or 3
    reason: str         # stop, target1, target2, trailing_stop
    price: float        # observed price, the simulated fill
    rule_level: float   # the stop or target the rule compared against
    pnl_eur: float


def _trailing_active(trade: PaperTrade, sat: dict) -> bool:
    start = sat.get("trailing_starts_after", "T2")
    if start == "entry":
        return True
    if start == "T1":
        return trade.exits[0] is not None
    return trade.exits[1] is not None


def step(trade: PaperTrade, price: float, now: datetime,
         sat: dict) -> tuple[PaperTrade, list[ExitEvent]]:
    """Apply one observed price to an open trade. Returns the new trade and its exits.

    Order: trailing high, stop-loss (all unsold thirds), target 1, target 2,
    trailing stop on the final third. Fills are at the observed price.
    """
    if not trade.is_open:
        return trade, []
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    fee = sat["fee_rate"]
    lv = levels(trade.entry_price, sat)
    high = max(trade.trailing_high or trade.entry_price, price)
    exits = list(trade.exits)
    reason3 = trade.exit3_reason
    events: list[ExitEvent] = []

    def close(i: int, reason: str, level: float) -> None:
        nonlocal reason3
        exits[i] = Exit(price, stamp)
        if i == 2:
            reason3 = reason
        events.append(ExitEvent(trade.id, trade.pair, i + 1, reason, price, level,
                                third_pnl(trade.size_eur, trade.entry_price, price, fee)))

    if price <= lv.stop:
        for i in range(3):
            if exits[i] is None:
                close(i, "stop", lv.stop)
    else:
        if exits[0] is None and price >= lv.target1:
            close(0, "target1", lv.target1)
        if exits[1] is None and price >= lv.target2:
            close(1, "target2", lv.target2)
        probe = replace(trade, exits=tuple(exits))
        trail = trailing_level(high, sat)
        if exits[2] is None and _trailing_active(probe, sat) and price <= trail:
            close(2, "trailing_stop", trail)

    new = replace(trade, exits=tuple(exits), exit3_reason=reason3, trailing_high=high)
    if not new.is_open and not new.date_closed:
        new = replace(new, date_closed=now.strftime("%Y-%m-%d"))
    return new, events


# --------------------------------------------------------- satellite bag


def satellite_balance(start_eur: float, trades: Sequence[PaperTrade], fee: float,
                      transfers_eur: float = 0.0) -> float:
    """Starting satellite money plus realized P&L plus transfers in (sweeps out < 0)."""
    return start_eur + transfers_eur + sum(t.realized_pnl(fee) for t in trades)


def win_rate(trades: Sequence[PaperTrade], fee: float) -> float | None:
    closed = [t for t in trades if not t.is_open]
    if not closed:
        return None
    return sum(t.realized_pnl(fee) > 0 for t in closed) / len(closed)


def bag_status(balance: float, sat: dict) -> str:
    """'pause' below the floor, 'sweep' above the sweep level, else 'ok'."""
    if balance < sat["floor_eur"]:
        return "pause"
    if balance > sat["sweep_above_eur"]:
        return "sweep"
    return "ok"


def entry_block(open_trades: int, balance: float, sat: dict,
                regime_ok: bool | None = True) -> str | None:
    """Why new entry signals are suppressed, or None when they are allowed.

    `regime_ok` is None when the filter is off or lacks history (not blocking).
    """
    if balance < sat["floor_eur"]:
        return "satellite below floor"
    if open_trades >= sat["max_open_trades"]:
        return "max open trades reached"
    if regime_ok is False:
        return "regime filter: BTC below its moving average"
    return None
