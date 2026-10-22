"""Team 1 — book.py: holdings, lot table, candidate menu, reconciliation, whole-share solver."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

import book as B
import config as C
import helpers as H

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "book.py")
PX = H.today_prices()
TODAY = {"QQQ": 2, "QQQM": 1}
CASH = 68.59


def _cli(args, state):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", US_ADVISOR_STATE=str(state))
    return subprocess.run([sys.executable, SCRIPT] + args, capture_output=True, text=True, env=env,
                          timeout=120)


def _lines(res):
    return [(o["action"], o["ticker"], o["shares"]) for o in res["orders"]]


# ---------------------------------------------------------------- 6
def test_parse_holdings_formats_and_rejects_fractional(state):
    want = {"positions": {"QQQ": 2, "QQQM": 1}, "cash_usd": 68.59}
    assert B.parse_holdings("QQQ 2, QQQM 1, cash 68.59") == want
    a = B.parse_holdings("현금 68.59 / 2 QQQ, qqqm 1")
    assert a["positions"] == want["positions"] and a["cash_usd"] == 68.59
    b = B.parse_holdings("QQQ 2 @ 710\nQQQM 1 @711.5\n$68.59")
    assert b == want
    assert B.parse_holdings("QQQ 2주, QQQM 1, 68.59 USD") == want
    assert B.parse_holdings("QQQ 2") == {"positions": {"QQQ": 2}, "cash_usd": 0.0}
    assert all(isinstance(n, int) for n in a["positions"].values())
    for bad, msg in (("QQQ 1.5", "whole shares only"), ("QQQ 2, cash -5", "negative cash"),
                     ("QQQ 2, hello world", "unrecognized"), ("", "empty"),
                     ("QQQ 1, QQQ 1", "twice"), ("QQQ -1", "negative shares")):
        with pytest.raises(ValueError, match=msg):
            B.parse_holdings(bad)
    r = _cli(["--holdings", "QQQ 1.5, cash 10", "--run-id", "R"], state)
    assert r.returncode == 2 and "whole shares only" in r.stderr
    assert not (state / "run" / "R" / "holdings.json").exists()
    r = _cli(["--holdings", "QQQ 2, QQQM 1, cash 68.59", "--run-id", "R"], state)
    assert r.returncode == 0, r.stderr
    doc = H.read_json(state / "run" / "R" / "holdings.json")
    assert doc == {"schema": "holdings/v6", "run_id": "R", "source": "args",
                   "raw": "QQQ 2, QQQM 1, cash 68.59", "positions": {"QQQ": 2, "QQQM": 1},
                   "cash_usd": 68.59}


def test_holdings_last_reads_ledger_mark_or_exits_4(state):
    assert _cli(["--holdings", "last", "--run-id", "R"], state).returncode == 4
    led = state / "ledger.jsonl"
    rows = [{"type": "epoch", "date": "2026-10-10"},
            {"type": "mark", "date": "2026-10-09", "account": {"positions": {"QQQ": 2, "QQQM": 1},
                                                               "cash_usd": 68.59}},
            {"type": "mark", "date": "2026-10-16", "account": {"positions": {"QQQ": 2, "NVDA": 1},
                                                               "cash_usd": 143.06}}]
    led.write_text("\n".join(json.dumps(r) for r in rows) + "\n{corrupt\n")
    r = _cli(["--holdings", "last", "--run-id", "R"], state)
    assert r.returncode == 0, r.stderr
    doc = H.read_json(state / "run" / "R" / "holdings.json")
    assert doc["source"] == "ledger" and doc["positions"] == {"QQQ": 2, "NVDA": 1}
    assert doc["cash_usd"] == 143.06 and doc["ledger_mark_date"] == "2026-10-16"


# ---------------------------------------------------------------- 7
def test_lot_table_feasibility_today():
    v = B.value(TODAY, CASH, PX)
    assert v["total_usd"] == 1895.99
    assert v["core_usd"] == 1827.40 and round(v["cash_pct"], 1) == 3.6
    assert v["weights"]["CASH"] == pytest.approx(68.59 / 1895.99, abs=1e-6)
    lot = B.lot_table(1895.99, PX, 0.25, cash_usd=CASH)
    assert set(lot) == set(C.ALL_TICKERS)
    for t in ("NVDA", "AMZN", "PLTR", "GOOGL", "AVGO", "XLK", "SOXQ", "IGV", "SPYM"):
        assert lot[t]["feasible"], t
    for t in ("TSM", "MSFT", "META", "AMD", "MU", "SMH", "SOXX"):
        assert not lot[t]["feasible"], t
    for t in ("QQQ", "QQQM", "SGOV"):
        assert lot[t]["feasible"], t
    assert lot["TSM"]["lot_usd_buffered"] == 476.92          # > 0.25 * 1895.99 = 474.00
    assert lot["NVDA"] == {"price": 237.47, "lot_usd_buffered": 239.84, "lot_pct": 12.65,
                           "feasible": True, "max_shares_in_cash": 0, "group": "name"}
    assert lot["SOXQ"]["max_shares_in_cash"] == 0 and lot["SGOV"]["max_shares_in_cash"] == 0
    assert B.lot_table(1895.99, PX, 0.25, cash_usd=400)["QQQM"]["max_shares_in_cash"] == 1


# ---------------------------------------------------------------- 8
def test_candidate_menu_order_and_cap():
    pos = {"QQQ": 2, "QQQM": 1, "MU": 1}
    lot = B.lot_table(1895.99, PX, 0.25)        # today's lot table; MU held but infeasible
    assert not lot["MU"]["feasible"] and not lot["MSFT"]["feasible"]
    ranks = {"PLTR": 1, "TSM": 2, "NVDA": 3, "XLK": 4, "SOXQ": 5, "IGV": 6, "AMZN": 7, "MU": 8}
    tick = {t: {"rs_rank": ranks.get(t), "earnings_in_sessions": None} for t in C.ALL_TICKERS}
    tick["GOOGL"]["earnings_in_sessions"] = 5
    tick["AAPL"]["earnings_in_sessions"] = 12          # outside the 10-session window
    tick["MSFT"]["earnings_in_sessions"] = 3           # infeasible -> hidden
    menu = B.candidate_menu(lot, pos, {"tickers": tick})
    assert menu == ["MU", "GOOGL", "PLTR", "NVDA", "XLK", "SOXQ"]
    assert len(menu) <= C.CANDIDATE_MENU_MAX
    m2, why = B.candidate_menu_with_reasons(lot, pos, tick, max_n=3)
    assert m2 == ["MU", "GOOGL", "PLTR"]
    assert why == {"MU": "held", "GOOGL": "earnings_in_5", "PLTR": "rs_rank_1"}
    assert "TSM" not in B.candidate_menu(lot, {"QQQ": 2}, tick, max_n=20)


# ---------------------------------------------------------------- 9
def test_solve_nvda_entry_sells_qqqm():
    res = B.solve({"CORE": 0.87, "NVDA": 0.13}, TODAY, CASH, PX)
    assert _lines(res) == [("SELL", "QQQM", 1), ("BUY", "NVDA", 1)]
    sell, buy = res["orders"]
    assert sell["sleeve"] == "CORE" and sell["priority"] == 2 and sell["limit_ref_price"] == 311.94
    assert buy["sleeve"] == "SLEEVE" and buy["priority"] == 4 and buy["usd"] == 237.47
    assert res["post_trade"]["cash_usd"] == 143.06
    assert res["post_trade"]["positions"] == {"QQQ": 2, "NVDA": 1}
    assert round(res["post_trade"]["cash_pct"], 1) == 7.5
    assert res["post_trade"]["weights"]["CORE"] == pytest.approx(0.799, abs=1e-3)
    assert res["post_trade"]["weights"]["NVDA"] == pytest.approx(0.125, abs=1e-3)
    assert res["residual"]["unavoidable"] is True and res["residual"]["cash_usd"] == 143.06
    assert res["conversion_used"] is False and res["lines"] == 2 and res["dropped_targets"] == []
    # 12.5% is below one buffered lot (237.0 < 239.84) -> dropped, zero orders
    res2 = B.solve({"CORE": 0.875, "NVDA": 0.125}, TODAY, CASH, PX)
    assert res2["orders"] == [] and res2["dropped_targets"] == [{"ticker": "NVDA", "reason": "LOT_TOO_SMALL"}]
    # pure / deterministic
    assert B.solve({"CORE": 0.87, "NVDA": 0.13}, TODAY, CASH, PX) == res


# ---------------------------------------------------------------- 10
def test_solve_no_conversion_without_need_but_conversion_when_residual_over_cap():
    assert B.solve(B.core_only_targets(), TODAY, CASH, PX)["orders"] == []
    r400 = B.solve(B.core_only_targets(), TODAY, 400.0, PX)
    assert _lines(r400) == [("BUY", "QQQM", 1)] and r400["conversion_used"] is False
    # QQQ 2 + $250: no QQQM fits the cash (315.06 > 250), residual 14.2% > 10%
    # -> only a QQQ -> QQQM conversion cuts it below the cap
    rc = B.solve(B.core_only_targets(), {"QQQ": 2}, 250.0, PX)
    assert _lines(rc) == [("SELL", "QQQ", 1), ("BUY", "QQQM", 3)]
    assert rc["conversion_used"] is True
    assert rc["post_trade"]["cash_usd"] == 71.91 and rc["post_trade"]["cash_pct"] <= 10
    # conversion disabled -> filler instead
    rn = B.solve(B.core_only_targets(), {"QQQ": 2}, 250.0, PX, allow_conversion=False)
    assert _lines(rn) == [("BUY", "SPYM", 2)] and rn["conversion_used"] is False


# ---------------------------------------------------------------- 11
def test_solve_filler_line_cap_and_forced_sell():
    rf = B.solve(B.core_only_targets(), {}, 250.0, PX)
    assert _lines(rf) == [("BUY", "SPYM", 2)]
    assert rf["orders"][0]["sleeve"] == "FILLER" and rf["orders"][0]["priority"] == 6
    assert rf["residual"]["unavoidable"] is True     # nothing else fits $68
    assert B.solve(B.core_only_targets(), {}, 250.0, PX, filler=False)["orders"] == []

    # 6 natural lines (2 exits, 2 sleeve buys, QQQM, filler) > 4 -> FILLER first, then smallest BUY
    big = B.solve({"CORE": 0.25, "SOXQ": 0.25, "XLK": 0.25, "IGV": 0.25},
                  {"PLTR": 1, "AMZN": 1}, 300.0, PX)
    assert big["lines"] <= C.MAX_ORDER_LINES
    assert not any(o["sleeve"] == "FILLER" for o in big["orders"])
    assert {("SELL", "PLTR", 1), ("SELL", "AMZN", 1)} <= set(_lines(big))
    codes = [w["code"] for w in big["warnings"]]
    assert codes.count("LINE_CAP") == 2 and "LOT_TOO_SMALL" in codes
    assert {"ticker": "SOXQ", "reason": "LINE_CAP"} in big["dropped_targets"]
    assert {"ticker": "XLK", "reason": "LOT_TOO_SMALL"} in big["dropped_targets"]
    # with room for 6 lines everything (incl. the filler) is emitted
    assert any(o["sleeve"] == "FILLER" for o in
               B.solve({"CORE": 0.25, "SOXQ": 0.25, "XLK": 0.25, "IGV": 0.25},
                       {"PLTR": 1, "AMZN": 1}, 300.0, PX, max_lines=6)["orders"])

    # kill-fired forced sell is priority 1 even when targets keep NVDA
    rk = B.solve({"CORE": 0.875, "NVDA": 0.125}, {"QQQ": 2, "QQQM": 1, "NVDA": 1}, CASH, PX,
                 forced_sells={"NVDA": 1})
    first = rk["orders"][0]
    assert (first["action"], first["ticker"], first["shares"], first["priority"]) == ("SELL", "NVDA", 1, 1)
    assert "NVDA" not in rk["post_trade"]["positions"]
    # a forced sell is never dropped by the line cap
    rk1 = B.solve({"CORE": 1.0}, {"QQQ": 2, "NVDA": 1}, CASH, PX, forced_sells={"NVDA": 1}, max_lines=1)
    assert ("SELL", "NVDA", 1) in _lines(rk1)


# ---------------------------------------------------------------- extras
def test_incumbent_not_trimmed_by_buffer_and_filler_not_churned():
    pos = {"QQQ": 2, "NVDA": 1}
    total = B.value(pos, 143.06, PX)["total_usd"]
    w_now = 237.47 / total
    res = B.solve({"CORE": 1 - round(w_now, 3), "NVDA": round(w_now, 3)}, pos, 143.06, PX)
    assert res["orders"] == []                     # held weight re-voted -> no churn
    # explicit 0 / absent -> exit
    assert ("SELL", "NVDA", 1) in _lines(B.solve({"CORE": 1.0}, pos, 143.06, PX))
    # held filler SPYM absent from targets is kept (no sell/rebuy churn)...
    assert B.solve(B.core_only_targets(), {"QQQ": 2, "SPYM": 2}, 100.0, PX)["orders"] == []
    # ...but released when that funds a whole QQQM share
    rr = B.solve(B.core_only_targets(), {"QQQ": 2, "SPYM": 2}, 150.0, PX)
    assert _lines(rr) == [("SELL", "SPYM", 2), ("BUY", "QQQM", 1)]


def test_apply_orders_and_value():
    orders = [{"ticker": "QQQM", "action": "SELL", "shares": 1, "limit_ref_price": 311.94},
              {"ticker": "NVDA", "action": "BUY", "shares": 1, "limit_ref_price": 237.47}]
    pos, cash = B.apply_orders(TODAY, CASH, orders)
    assert pos == {"QQQ": 2, "NVDA": 1} and cash == pytest.approx(143.06)
    pos, cash = B.apply_orders({}, 400.0, [{"ticker": "QQQM", "action": "BUY", "shares": 1,
                                            "limit_ref_price": 311.94}], C.COMMISSION_PCT)
    assert pos == {"QQQM": 1} and cash == pytest.approx(400 - 311.94 * (1 + C.COMMISSION_PCT))
    with pytest.raises(ValueError):
        B.apply_orders({"QQQ": 1}, 0, [{"ticker": "QQQ", "action": "SELL", "shares": 2,
                                        "limit_ref_price": 1.0}])
    v = B.value({"QQQ": 1, "SGOV": 2, "NVDA": 1, "XYZ": 3}, 10.0, PX)
    assert v["unpriced"] == ["XYZ"]
    assert v["total_usd"] == round(757.73 + 200 + 237.47 + 10, 2)
    assert v["sgov_usd"] == 200.0 and v["sleeve_usd"] == 237.47
    assert B.core_only_targets() == {"CORE": 1.0}


def test_caps_in_effect_by_authority_and_regime():
    assert B.caps_in_effect("FULL", "UP") == {
        "authority": "FULL", "sleeve_max_pct": 0.50, "sleeve_position_max_pct": 0.25,
        "sleeve_max_positions": 3, "sgov_max_pct": 0.30, "sgov_authority": True}
    assert B.caps_in_effect("FULL", "DOWN")["sgov_max_pct"] == 0.60
    t1 = B.caps_in_effect("T1_REDUCED", "UP")
    assert t1["sleeve_max_pct"] == C.T1_SLEEVE_CUT and t1["sleeve_position_max_pct"] == 0.15
    t2 = B.caps_in_effect("T2_SUSPENDED", "DOWN")
    assert t2["sleeve_max_pct"] == 0 and t2["sgov_max_pct"] == 0 and t2["sgov_authority"] is False


def test_table_first_run_and_reconciliation(state):
    rd = H.make_run(state, "20261011-0830")
    b = H.read_json(os.path.join(rd, "book.json"))
    assert b["schema"] == "book/v6" and b["run_id"] == "20261011-0830"
    assert b["total_usd"] == 1895.99 and b["cash_usd"] == 68.59 and b["cash_pct"] == 3.62
    assert b["positions"]["QQQ"] == {"shares": 2, "price": 757.73, "usd": 1515.46, "pct": 79.93,
                                     "sleeve": "CORE"}
    assert b["core_usd"] == 1827.4 and b["sleeve_usd"] == 0 and b["held_sleeve"] == []
    assert b["caps_in_effect"]["authority"] == "FULL" and b["caps_in_effect"]["sgov_max_pct"] == 0.30
    assert b["candidate_menu"] == ["NVDA", "AMZN", "PLTR", "XLK", "SOXQ", "IGV"]
    assert b["candidate_reasons"]["NVDA"] == "rs_rank_1"
    assert set(b["hidden_infeasible"]) == {"TSM", "MSFT", "META", "AMD", "MU", "SMH", "SOXX"}
    assert b["reconciliation"] == {"pending_orders": [], "filled": [], "unfilled": [], "unexpected": [],
                                   "implied_cash_delta_usd": 0.0, "warnings": []}
    assert b["as_of_close_date"] == "2026-10-07" and b["regime"] == "UP"

    # second run: NVDA BUY filled, QQQM SELL not, + an unexplained AAPL and a $100 deposit
    dig = H.first_run_digest("20261018-0830")
    dig.update(first_run=False,
               holdings_last_mark={"date": "2026-10-09", "positions": {"QQQ": 2, "QQQM": 1},
                                   "cash_usd": 68.59, "total_usd": 1895.99},
               pending_orders=[
                   {"ref_run_id": "20261011-0830", "ticker": "QQQM", "action": "SELL", "shares": 1,
                    "limit_ref_price": 311.94, "runs_unfilled": 0},
                   {"ref_run_id": "20261011-0830", "ticker": "NVDA", "action": "BUY", "shares": 1,
                    "limit_ref_price": 237.47, "runs_unfilled": 0}],
               open_theses=[{"ticker": "NVDA"}])
    dig["triggers"]["authority"] = "T1_REDUCED"
    rd2 = H.make_run(state, "20261018-0830", holdings_text="QQQ 2, QQQM 1, NVDA 1, AAPL 1, cash 100",
                     digest=dig)
    b2 = H.read_json(os.path.join(rd2, "book.json"))
    rec = b2["reconciliation"]
    assert [o["ticker"] for o in rec["filled"]] == ["NVDA"] and rec["filled"][0]["status"] == "FILLED"
    assert [o["ticker"] for o in rec["unfilled"]] == ["QQQM"]
    assert rec["unexpected"] == [{"ticker": "AAPL", "delta_shares": 1, "explained_shares": 0,
                                  "unexplained_shares": 1}]
    assert rec["implied_cash_delta_usd"] == round(100 - (68.59 - 237.47), 2)
    assert {w["code"] for w in rec["warnings"]} == {"DECLARE_FLOW", "UNEXPECTED_POSITION_CHANGE"}
    assert b2["held_sleeve"] == ["NVDA", "AAPL"] and b2["candidate_menu"][:2] == ["NVDA", "AAPL"]
    assert b2["caps_in_effect"]["authority"] == "T1_REDUCED"
    assert b2["caps_in_effect"]["sleeve_max_pct"] == 0.15
    assert b2["positions"]["NVDA"]["sleeve"] == "SLEEVE"
    # --table via CLI reproduces the file byte-for-byte content
    before = H.read_json(os.path.join(rd2, "book.json"))
    r = _cli(["--table", "--run-id", "20261018-0830"], state)
    assert r.returncode == 0, r.stderr
    assert H.read_json(os.path.join(rd2, "book.json")) == before
    assert _cli(["--table", "--run-id", "NOPE"], state).returncode == 2


def test_solve_cli_from_aggregate_out(state):
    rd = H.make_run(state, "R")
    assert _cli(["--solve", "--run-id", "R"], state).returncode == 2          # no aggregate_out
    agg = {"schema": "aggregate/v6", "run_id": "R", "valid": False,
           "targets": {"CORE": 0.87, "SGOV": 0.0, "NVDA": 0.13}}
    H.write_json(os.path.join(rd, "aggregate_out.json"), agg)
    assert _cli(["--solve", "--run-id", "R"], state).returncode == 2          # valid=false
    assert not os.path.exists(os.path.join(rd, "orders.json"))
    agg.update(valid=True, forced_sells={},
               sleeve={"NVDA": {"weight": 0.13, "votes": 2, "action": "ENTER",
                                "reason_codes": ["EARNINGS_BEAT_RAISE", "GUIDANCE_RAISE"]}},
               sgov={"weight": 0.0, "votes": 0, "rule": "NONE", "reason_codes": []}, exits=[])
    H.write_json(os.path.join(rd, "aggregate_out.json"), agg)
    r = _cli(["--solve", "--run-id", "R"], state)
    assert r.returncode == 0, r.stderr
    o = H.read_json(os.path.join(rd, "orders.json"))
    assert o["schema"] == "orders/v6" and o["run_id"] == "R"
    assert _lines(o) == [("SELL", "QQQM", 1), ("BUY", "NVDA", 1)]
    assert o["orders"][1]["reason"] == "ENTER EARNINGS_BEAT_RAISE 2/3"
    assert o["post_trade"]["cash_usd"] == 143.06 and o["conversion_used"] is False
    assert set(o) >= {"orders", "post_trade", "residual", "conversion_used", "lines",
                      "dropped_targets", "warnings"}
    # T2 suspended -> non-core targets zeroed, warning
    dig = H.read_json(os.path.join(rd, "digest.json"))
    dig["triggers"]["authority"] = "T2_SUSPENDED"
    H.write_json(os.path.join(rd, "digest.json"), dig)
    assert _cli(["--solve", "--run-id", "R"], state).returncode == 0
    o2 = H.read_json(os.path.join(rd, "orders.json"))
    assert o2["orders"] == [] and o2["warnings"][0]["code"] == "T2_SUSPENDED"
