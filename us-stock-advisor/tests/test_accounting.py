"""Accounting: time-weighted return, external flows, same-date de-dup, the live-ledger
correction, benchmark window alignment, unexplained-flow warning."""
from __future__ import annotations

import json
import shutil
from datetime import timedelta

import pandas as pd
import pytest

import score_recs as S
from helpers import (HISTORY_CSV, LIVE_LAST_RUN_COPY, LIVE_LEDGER_COPY, build_ledger,
                     make_baseline, mark, portfolio, read_bytes, run, utc_today, write_json)


def flat_csv(path, start="2025-01-01", n=120, px=100.0):
    idx = pd.date_range(start, periods=n, freq="D")
    df = pd.DataFrame({"QQQ": [px] * n}, index=idx)
    df.index.name = "Date"
    df.to_csv(path)
    return str(path)


# --------------------------------------------------------------- TWR
def test_twr_ignores_deposits_and_withdrawals(tmp_path):
    csv = flat_csv(tmp_path / "q.csv")
    a, b, c = (tmp_path / x for x in ("a", "b", "c"))
    for d in (a, b, c):
        d.mkdir()
    # flow-free: +10% then +10%
    build_ledger(a / "recommendations.jsonl", [
        mark("2025-02-01", 100.0, 100.0), mark("2025-02-10", 110.0, 110.0),
        mark("2025-02-20", 121.0, 121.0)])
    # same performance, +50 deposit (v5.1 field) then a -66 withdrawal (legacy + new)
    build_ledger(b / "recommendations.jsonl", [
        mark("2025-02-01", 100.0, 100.0),
        mark("2025-02-10", 160.0, 160.0, external_flow_usd=50.0, deposit_usd=50.0),
        mark("2025-02-20", 110.0, 110.0, external_flow_usd=-66.0, withdraw_usd=66.0)])
    # same again, but the flows are declared by appended cash_flow_adjustment lines
    build_ledger(c / "recommendations.jsonl", [
        mark("2025-02-01", 100.0, 100.0), mark("2025-02-10", 160.0, 160.0),
        mark("2025-02-20", 110.0, 110.0),
        {"type": "cash_flow_adjustment", "date": "2025-03-01", "effective_date": "2025-02-05",
         "flow_usd": 50.0, "ticker": None, "action": "ADJUST"},
        {"type": "cash_flow_adjustment", "date": "2025-03-01", "effective_date": "2025-02-20",
         "flow_usd": -66.0, "ticker": None, "action": "ADJUST"}])
    ra, rb, rc = (S.cumulative(str(d / "recommendations.jsonl"), csv) for d in (a, b, c))
    assert ra["actual_cum_pct"] == pytest.approx(21.0)
    assert rb["actual_cum_pct"] == pytest.approx(ra["actual_cum_pct"])
    assert rc["actual_cum_pct"] == pytest.approx(ra["actual_cum_pct"])
    assert ra["method"] == rb["method"] == "TWR"
    assert rb["net_external_flow_usd"] == pytest.approx(-16.0)
    assert rc["flow_adjustments"] == 2
    assert ra["pnl_usd"] == pytest.approx(21.0)            # 10 + 11
    assert rb["pnl_usd"] == pytest.approx(26.0)            # 10 + 10% of the larger 160 book


def test_legacy_deposit_on_superseded_mark_counted_once(tmp_path):
    csv = flat_csv(tmp_path / "q.csv")
    recs = tmp_path / "recommendations.jsonl"
    build_ledger(recs, [
        mark("2025-02-01", 100.0, 100.0),
        mark("2025-02-10", 150.0, 150.0, deposit_usd=50.0),     # superseded run keeps flow
        mark("2025-02-10", 150.0, 150.0, deposit_usd=0.0),
        mark("2025-02-20", 150.0, 150.0)])
    loaded = S.load_recs(str(recs))
    assert S.external_flows(loaded) == {"2025-02-10": 50.0}
    r = S.cumulative(str(recs), csv)
    assert r["actual_cum_pct"] == pytest.approx(0.0)
    assert r["superseded_runs"] == 1


def test_forced_relog_repeating_deposit_not_double_counted(tmp_path):
    recs = tmp_path / "recommendations.jsonl"
    build_ledger(recs, [
        mark("2025-02-01", 100.0, 100.0),
        mark("2025-02-10", 150.0, 150.0, external_flow_usd=50.0),
        mark("2025-02-10", 150.0, 150.0, external_flow_usd=50.0, supersedes_same_date=True)])
    assert S.external_flows(S.load_recs(str(recs))) == {"2025-02-10": 50.0}
    r = S.cumulative(str(recs), flat_csv(tmp_path / "q.csv"))
    assert r["actual_cum_pct"] == pytest.approx(0.0)


def test_same_date_dedup_keeps_last_run(tmp_path):
    recs = tmp_path / "recommendations.jsonl"
    build_ledger(recs, [
        mark("2025-02-01", 100.0, 100.0), {"type": "order", "date": "2025-02-01", "ticker": "QQQ",
                                           "action": "BUY", "shares": 1.0},
        {"type": "pipeline_halt", "date": "2025-02-01", "ticker": None, "action": "HALT"},
        mark("2025-02-01", 100.0, 100.0), {"type": "order", "date": "2025-02-01", "ticker": "QQQM",
                                           "action": "BUY", "shares": 1.0}])
    eff = S.effective_recs(S.load_recs(str(recs)))
    assert [r["type"] for r in eff] == ["pipeline_halt", "portfolio_mark", "order"]
    assert eff[-1]["ticker"] == "QQQM"


# ------------------------------------------------- live ledger (copy only)
@pytest.fixture
def live_copy(state):
    shutil.copy(LIVE_LEDGER_COPY, state / "recommendations.jsonl")
    shutil.copy(LIVE_LAST_RUN_COPY, state / "last_run.json")
    return state


def test_live_copy_pre_adjustment_facts(live_copy):
    recs = str(live_copy / "recommendations.jsonl")
    loaded = S.load_recs(recs)
    assert len(loaded) == 33
    assert S.ledger_intact(recs, str(live_copy / "last_run.json"))
    assert S.superseded_run_count(loaded) == 2
    assert S.external_flows(loaded) == {"2026-08-21": 176.67}
    eff = S.effective_recs(loaded)
    assert len([r for r in eff if r["type"] == "portfolio_mark"]) == 14 - 2
    r = S.cumulative(recs, HISTORY_CSV)
    assert r["actual_cum_pct"] == pytest.approx(-11.13, abs=0.02)


def test_live_copy_adjustment_appends_and_corrects(live_copy):
    recs = live_copy / "recommendations.jsonl"
    before = read_bytes(recs)
    r = run("report.py", "--adjust-flow", "--effective-date", "2026-07-23", "--flow-usd", "-325.18",
            "--flow-krw", "-244129", "--reason", "test: withdrawal on 07-23", state=live_copy)
    assert r.returncode == 0, r.stderr
    after = read_bytes(recs)
    assert after.startswith(before) and len(after.splitlines()) == 34
    assert S.ledger_intact(str(recs), str(live_copy / "last_run.json"))
    assert json.load(open(live_copy / "last_run.json"))["ledger_len"] == 34
    c = S.cumulative(str(recs), HISTORY_CSV)
    assert c["actual_cum_pct"] == pytest.approx(6.68, abs=0.02)
    assert c["pnl_usd"] == pytest.approx(121.35, abs=0.05)
    assert c["flow_adjustments"] == 1
    assert c["net_external_flow_usd"] == pytest.approx(176.67 - 325.18, abs=0.01)
    # identical adjustment again: refused (exit 4), nothing appended
    r2 = run("report.py", "--adjust-flow", "--effective-date", "2026-07-23", "--flow-usd", "-325.18",
             "--reason", "again", state=live_copy)
    assert r2.returncode == 4 and read_bytes(recs) == after
    # ... unless forced
    r3 = run("report.py", "--adjust-flow", "--effective-date", "2026-07-23", "--flow-usd", "-325.18",
             "--reason", "again, deliberately", "--force-relog", state=live_copy)
    assert r3.returncode == 0 and read_bytes(recs).startswith(after)


def test_adjust_flow_refusals(live_copy):
    recs = live_copy / "recommendations.jsonl"
    before = read_bytes(recs)
    for args in (["--effective-date", "2030-01-01", "--flow-usd", "-1", "--reason", "x"],  # no mark
                 ["--effective-date", "2026-07-23", "--flow-usd", "0", "--reason", "x"],
                 ["--effective-date", "2026-07-23", "--flow-usd", "-1"],                  # no reason
                 ["--effective-date", "07/23/2026", "--flow-usd", "-1", "--reason", "x"]):
        r = run("report.py", "--adjust-flow", *args, state=live_copy)
        assert r.returncode == 2, (args, r.stderr)
    assert read_bytes(recs) == before


# --------------------------------------------------- benchmark alignment
def test_benchmark_measured_between_valuation_bars(tmp_path):
    idx = pd.date_range("2025-03-01", periods=40, freq="D")
    px = [100.0 if d < pd.Timestamp("2025-03-10") else 110.0 for d in idx]
    df = pd.DataFrame({"QQQ": px}, index=idx)
    df.index.name = "Date"
    csv = tmp_path / "q.csv"
    df.to_csv(csv)
    recs = tmp_path / "recommendations.jsonl"
    build_ledger(recs, [mark("2025-03-10", 100.0, 100.0), mark("2025-03-20", 100.0, 100.0)])
    # the 03-10 run was valued at the 03-09 close (100), not at its own day's 110
    assert S.cumulative(str(recs), str(csv))["qqq_cum_pct"] == pytest.approx(10.0)
    recs2 = tmp_path / "r2.jsonl"
    build_ledger(recs2, [mark("2025-03-10", 100.0, 100.0, last_bar="2025-03-10"),
                         mark("2025-03-20", 100.0, 100.0, last_bar="2025-03-19")], anchor=False)
    assert S.cumulative(str(recs2), str(csv))["qqq_cum_pct"] == pytest.approx(0.0)


# ------------------------------------------------- unexplained-flow warning
def _prior_ledger(state, cash):
    y = str(utc_today() - timedelta(days=1))
    build_ledger(state / "recommendations.jsonl",
                 [mark(y, 1475.86 + cash, cash, {"QQQ": {"shares": 2.0, "usd": 1475.86}})],
                 anchor=False)


@pytest.mark.parametrize("prev_cash,withdraw,warn", [(325.18, 0, True), (25.0, 0, False),
                                                     (325.18, 325.18, False)])
def test_unexplained_flow_warning(state, tmp_path, prev_cash, withdraw, warn):
    _prior_ledger(state, prev_cash)
    b = make_baseline(pf=portfolio(0.0, QQQ=2))
    bp = write_json(tmp_path / "b.json", b)
    fp = write_json(tmp_path / "f.json", {"final_plan": {"orders": [], "source": "BASELINE"}})
    args = ["--log", "--baseline", bp, "--final", fp]
    if withdraw:
        args += ["--withdraw", withdraw]
    r = run("report.py", *args, state=state)
    assert r.returncode == 0, r.stderr
    m = [x for x in S.load_recs(str(state / "recommendations.jsonl"))
         if x["type"] == "portfolio_mark"][-1]
    assert (m["unexplained_flow_usd"] is not None) is warn
    if withdraw:
        assert m["external_flow_usd"] == pytest.approx(-withdraw)
        assert m["withdraw_usd"] == pytest.approx(withdraw)
