from datetime import datetime, timezone

from src import rules, trades_file

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
SAT = {"stop_loss": 0.08, "target1": 0.10, "target2": 0.20, "trailing_stop": 0.08,
       "trailing_starts_after": "T2", "fee_rate": 0.0}
HEADER = ",".join(rules.COLUMNS)


def line(id_, pair="SOL-USDC", entry="100", size="5", notes=""):
    cells = {c: "" for c in rules.COLUMNS}
    cells.update(id=id_, date_opened="2026-10-01", pair=pair, entry_price=entry,
                 size_eur=size, notes=notes)
    return ",".join(cells[c] for c in rules.COLUMNS)


def write_csv(path, *lines):
    path.write_text("\n".join([HEADER, *lines]) + "\n")


def test_load_reports_bad_and_duplicate_rows(tmp_path):
    p = tmp_path / "t.csv"
    write_csv(p, line("1"), line("2", entry="abc"), line("1"), "", ",,,,,,,,,,,,,,,")
    trades, problems = trades_file.load(p)
    assert [t.id for t in trades] == ["1"]
    assert len(problems) == 2 and "duplicate" in problems[1]


def test_write_keeps_rows_added_since_the_read(tmp_path):
    p = tmp_path / "t.csv"
    write_csv(p, line("1"))
    trades, _ = trades_file.load(p)
    updated, _ = rules.step(trades[0], 111, NOW, SAT)
    write_csv(p, line("1"), line("2", pair="XRP-USDC"))  # user adds a row meanwhile
    trades_file.write(p, [updated])
    rows = trades_file.read_rows(p)
    assert [r["id"] for r in rows] == ["1", "2"]
    assert rows[0]["exit1_price"] == "111" and rows[0]["status"] == "Open"
    assert rows[1]["pair"] == "XRP-USDC" and rows[1]["exit1_price"] == ""


def test_write_skips_a_row_the_user_edited_since_the_read(tmp_path):
    p = tmp_path / "t.csv"
    write_csv(p, line("1"))
    trades, _ = trades_file.load(p)
    updated, _ = rules.step(trades[0], 111, NOW, SAT)
    write_csv(p, line("1", entry="105"))  # user corrected the entry price
    trades_file.write(p, [updated])
    row = trades_file.read_rows(p)[0]
    assert row["entry_price"] == "105" and row["exit1_price"] == ""


def test_write_keeps_user_columns_byte_for_byte(tmp_path):
    p = tmp_path / "t.csv"
    write_csv(p, line("1", entry="100,50", notes="good setup"))
    p.write_text(p.read_text().replace("100,50", '"100,50"'))
    trades, _ = trades_file.load(p)
    updated, _ = rules.step(trades[0], 130, NOW, SAT)
    trades_file.write(p, [updated])
    row = trades_file.read_rows(p)[0]
    assert row["entry_price"] == "100,50" and row["notes"] == "good setup"


def test_deleted_row_stays_deleted(tmp_path):
    p = tmp_path / "t.csv"
    write_csv(p, line("1"), line("2"))
    trades, _ = trades_file.load(p)
    write_csv(p, line("2"))
    trades_file.write(p, [rules.step(trades[0], 111, NOW, SAT)[0]])
    assert [r["id"] for r in trades_file.read_rows(p)] == ["2"]


def test_transfers(tmp_path):
    p = tmp_path / "transfers.csv"
    p.write_text("date,from_bag,to_bag,amount_eur,notes\n"
                 "2026-12-31,satellite,core,6,sweep\n"
                 "2027-01-02,core,satellite,2.5,top up\n"
                 "2027-01-03,core,satellite,,blank\n")
    assert trades_file.load_transfers(p) == -3.5
    assert trades_file.load_transfers(tmp_path / "none.csv") == 0


def test_reconcile_applies_our_exits_to_the_users_newer_file(tmp_path):
    ours, theirs = tmp_path / "ours.csv", tmp_path / "theirs.csv"
    write_csv(ours, line("1"))
    trades, _ = trades_file.load(ours)
    trades_file.write(ours, [rules.step(trades[0], 125, NOW, SAT)[0]])
    write_csv(theirs, line("1"), line("2", pair="XRP-USDC", notes="added on GitHub"))
    trades_file.reconcile(ours, theirs)
    rows = trades_file.read_rows(theirs)
    assert rows[0]["exit2_price"] == "125" and rows[0]["status"] == "Open"
    assert rows[1]["notes"] == "added on GitHub" and rows[1]["status"] == ""


def test_reads_a_row_appended_by_the_dashboard(tmp_path):
    # docs/trade.js appends rows in exactly this shape: ask price as OKX sends
    # it, size rounded to cents, notes in the last column
    p = tmp_path / "t.csv"
    p.write_text(HEADER + "\n" + line("7") + "\n"
                 + "8,2026-10-08,SOL-USDC,106.12,5,,,,,,,,,,,dashboard\n")
    trades, problems = trades_file.load(p)
    assert problems == []
    t = trades[1]
    assert (t.id, t.pair, t.entry_price, t.size_eur) == ("8", "SOL-USDC", 106.12, 5.0)
    trades_file.write(p, trades)
    assert p.read_text().splitlines()[2].endswith(",dashboard")
