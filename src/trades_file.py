"""Reading and writing `paper_trades.csv` without losing the user's edits.

The user edits the file on GitHub while the bot writes to it. The write
re-reads the file and applies the script's columns only to rows whose user
columns are unchanged since the run read them: rows added, edited or deleted
in between are kept as the user left them.
"""

from __future__ import annotations

import csv
import io
import os
from pathlib import Path

from src import rules


def read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    # skip blank lines users leave at the end on GitHub
    return [r for r in rows if any((v or "").strip() for v in r.values())]


def load(path: Path) -> tuple[list[rules.PaperTrade], list[str]]:
    """Valid trades and one message per unusable row (bad data or duplicate id)."""
    trades, problems, seen = [], [], set()
    for row in read_rows(path):
        trade, problem = rules.parse_trade(row)
        if problem:
            problems.append(problem)
        elif trade.id in seen:
            problems.append(f"row {trade.id}: duplicate id, skipped")
        else:
            seen.add(trade.id)
            trades.append(trade)
    return trades, problems


def _user_part(row: dict) -> tuple:
    return tuple((row.get(c) or "").strip() for c in rules.USER_COLUMNS)


def merge(current_rows: list[dict], updated: list[rules.PaperTrade]) -> list[dict]:
    """Rows to write: the file as it is now, with the script columns of `updated` applied."""
    by_id = {t.id: t for t in updated}
    out = []
    for row in current_rows:
        trade = by_id.get((row.get("id") or "").strip())
        if trade is not None and _user_part(row) == _user_part(trade.row):
            row = rules.trade_to_row(trade)
        out.append({c: row.get(c) or "" for c in rules.COLUMNS})
    return out


def write(path: Path, updated: list[rules.PaperTrade]) -> None:
    rows = merge(read_rows(path), updated)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=rules.COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    tmp = path.with_suffix(".csv.tmp")
    tmp.write_text(buf.getvalue(), encoding="utf-8")
    os.replace(tmp, path)


def load_transfers(path: Path) -> float:
    """Net EUR moved into the satellite bag (sweeps out are negative).

    `transfers.csv`: date, from_bag, to_bag, amount_eur, notes.
    """
    if not path.exists():
        return 0.0
    total = 0.0
    with path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            amount = rules.parse_number(row.get("amount_eur"))
            if not amount:
                continue
            if (row.get("to_bag") or "").strip().lower() == "satellite":
                total += amount
            if (row.get("from_bag") or "").strip().lower() == "satellite":
                total -= amount
    return total


def reconcile(ours: Path, target: Path) -> None:
    """Apply the exits recorded in `ours` to `target` (the file as on GitHub now).

    Used by the workflow when the user pushed an edit during a run: rows the
    user added, edited or deleted keep the user's version.
    """
    trades, _ = load(ours)
    write(target, trades)


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 4 or sys.argv[1] != "reconcile":
        raise SystemExit("usage: python -m src.trades_file reconcile OURS.csv TARGET.csv")
    reconcile(Path(sys.argv[2]), Path(sys.argv[3]))
