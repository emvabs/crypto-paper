"""Reading and writing `paper_trades.csv` without losing the user's edits.

The user edits the file on GitHub while the bot writes to it. The write
re-reads the file and applies the script's columns only to rows whose user
columns are unchanged since the run read them: rows added, edited or deleted
in between are kept as the user left them. Columns the user added beyond
`rules.COLUMNS` are kept, after the standard ones.
"""

from __future__ import annotations

import csv
import io
import os
from pathlib import Path

from src import rules


def _read(path: Path) -> tuple[list[str], list[dict]]:
    """The header and the non-blank rows."""
    if not path.exists():
        return [], []
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        header = list(reader.fieldnames or [])
    # skip blank lines users leave at the end on GitHub; values past the
    # header land under the key None and are dropped
    rows = [{k: v for k, v in r.items() if k is not None} for r in rows]
    return header, [r for r in rows
                    if any(isinstance(v, str) and v.strip() for v in r.values())]


def read_rows(path: Path) -> list[dict]:
    return _read(path)[1]


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
            row = {**row, **rules.trade_to_row(trade)}
        out.append({c: row.get(c) or "" for c in [*rules.COLUMNS, *_extra(row)]})
    return out


def _extra(columns) -> list[str]:
    return [c for c in columns if c not in rules.COLUMNS]


def _write_rows(path: Path, header: list[str], rows: list[dict]) -> None:
    fields = [*rules.COLUMNS, *_extra(header)]
    for row in rows:
        fields += [c for c in _extra(row) if c not in fields]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, lineterminator="\n", restval="")
    writer.writeheader()
    writer.writerows(rows)
    tmp = path.with_suffix(".csv.tmp")
    tmp.write_text(buf.getvalue(), encoding="utf-8")
    os.replace(tmp, path)


def write(path: Path, updated: list[rules.PaperTrade]) -> None:
    header, rows = _read(path)
    _write_rows(path, header, merge(rows, updated))


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


def _key(row: dict) -> tuple:
    return tuple(sorted((k, (v or "").strip()) for k, v in row.items() if (v or "").strip()))


def reconcile(base: Path, ours: Path, target: Path) -> None:
    """Apply the run's changes (`base` → `ours`) to `target`, the file on GitHub now.

    `base` is the file the run started from. A row takes the run's version only
    when the user left it exactly as in `base`; rows the user added, edited (in
    any column) or deleted keep the user's version.
    """
    base_rows = {(r.get("id") or "").strip(): r for r in read_rows(base)}
    our_rows = {(r.get("id") or "").strip(): r for r in read_rows(ours)}
    header, rows = _read(target)
    out = []
    for row in rows:
        rid = (row.get("id") or "").strip()
        b, o = base_rows.get(rid), our_rows.get(rid)
        if o is not None and b is not None and _key(row) == _key(b):
            row = {**row, **o}
        out.append(row)
    _write_rows(target, header, out)


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 5 or sys.argv[1] != "reconcile":
        raise SystemExit("usage: python -m src.trades_file reconcile BASE.csv OURS.csv TARGET.csv")
    reconcile(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]))
