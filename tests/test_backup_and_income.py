"""backup_state.py and export_income.py: records that must survive the laptop."""
from __future__ import annotations

import csv
import sqlite3
from datetime import datetime, timezone

import backup_state as bs
import export_income as ei


def test_backup_copies_state_consistently_and_keeps_only_the_newest_days(tmp_path):
    root = tmp_path / "repo"
    (root / ".execution").mkdir(parents=True)
    (root / "execution").mkdir()
    (root / "carry_paper_ledger.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (root / "execution" / "carry_targets_latest.json").write_text("{}", encoding="utf-8")
    (root / ".execution" / "kill_switch.json").write_text("{}", encoding="utf-8")
    db = root / ".execution" / "testnet_execution.sqlite3"
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE t(x)")
        c.execute("INSERT INTO t VALUES (42)")
    (root / "data_snapshots").mkdir()
    (root / "data_snapshots" / "2026-09-01.csv").write_text("d,s,f,v\n", encoding="utf-8")

    backups = tmp_path / "backups"
    for old in ("2026-09-01", "2026-09-02", "2026-09-03"):
        (backups / old).mkdir(parents=True)
    out, copied = bs.run(root, backups, keep=2, today="2026-09-04")

    assert out == backups / "2026-09-04"
    assert (out / "carry_paper_ledger.csv").read_text(encoding="utf-8") == "a,b\n1,2\n"
    assert (out / "execution" / "carry_targets_latest.json").exists()
    assert (out / ".execution" / "kill_switch.json").exists()
    with sqlite3.connect(out / ".execution" / "testnet_execution.sqlite3") as c:
        assert c.execute("SELECT x FROM t").fetchone() == (42,)
    assert (out / "data_snapshots" / "2026-09-01.csv").exists()
    remaining = sorted(p.name for p in backups.iterdir())
    assert remaining == ["2026-09-03", "2026-09-04"], "retention keeps the newest `keep` folders"
    assert "carry_paper_ledger.csv" in copied and ".execution/testnet_execution.sqlite3" in copied


def test_backup_with_nothing_to_copy_still_succeeds(tmp_path):
    out, copied = bs.run(tmp_path / "empty", tmp_path / "b", keep=5, today="2026-09-04")
    assert out.exists() and copied == []


# --- income ---------------------------------------------------------------------------
def test_month_bounds_cover_the_calendar_month():
    start, end = ei.month_bounds("2026-02")
    assert start == datetime(2026, 2, 1, tzinfo=timezone.utc)
    assert end == datetime(2026, 3, 1, tzinfo=timezone.utc)
    start, end = ei.month_bounds("2026-12")
    assert end == datetime(2027, 1, 1, tzinfo=timezone.utc)


class _Client:
    """Pages of 1000 rows then a short page; the last row of a page repeats on the next."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def signed(self, method, path, params):
        self.calls.append(params)
        return self.pages.pop(0) if self.pages else []


def _row(i, kind="FUNDING_FEE", income="0.5"):
    return {"time": 1_788_300_000_000 + i * 1000, "incomeType": kind, "symbol": "BTCUSDT",
            "income": income, "asset": "USDT", "info": "", "tranId": i, "tradeId": ""}


def test_fetch_income_pages_and_dedupes_the_boundary_row():
    page1 = [_row(i) for i in range(1000)]
    page2 = [_row(999)] + [_row(i) for i in range(1000, 1010)]   # boundary row repeated
    client = _Client([page1, page2])
    rows = ei.fetch_income(client, datetime(2026, 9, 1, tzinfo=timezone.utc),
                           datetime(2026, 10, 1, tzinfo=timezone.utc), sleep=lambda s: None)
    assert len(rows) == 1010
    assert len(client.calls) == 2
    assert client.calls[1]["startTime"] == page1[-1]["time"] + 1


def test_write_csv_totals_per_income_type(tmp_path):
    rows = [_row(1, "FUNDING_FEE", "0.5"), _row(2, "COMMISSION", "-0.1"), _row(3, "REALIZED_PNL", "3.0")]
    out = tmp_path / "reports" / "income_testnet_2026-09.csv"
    totals = ei.write_csv(rows, out)
    assert totals == {"FUNDING_FEE": 0.5, "COMMISSION": -0.1, "REALIZED_PNL": 3.0}
    with out.open(encoding="utf-8") as fh:
        got = list(csv.DictReader(fh))
    assert [r["incomeType"] for r in got] == ["FUNDING_FEE", "COMMISSION", "REALIZED_PNL"]
    assert got[0]["time_utc"].endswith("+00:00")
