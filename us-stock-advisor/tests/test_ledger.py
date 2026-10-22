"""Team 3 — ledger.py: epoch/mark/shadow, TWR, --log, pending orders + T5, T1/T2 authority.

All state lives in the per-test `state` dir (conftest patches config paths in-process and
exports US_ADVISOR_STATE). Uses Team 1's real book/fetch_indicators via helpers.make_run.
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import date, timedelta

import pytest

import config as C
import helpers as H
import ledger as L

SK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX_A = os.path.join(SK, "tests", "fixtures", "mock_run")


def _types(recs):
    return [r["type"] for r in recs]


def _bump_as_of(rd, new_date):
    """Re-date a make_run run dir to a later session (make_run always ends on 10-07)."""
    for name in ("indicators.json", "book.json"):
        p = os.path.join(rd, name)
        d = H.read_json(p)
        d["as_of_close_date"] = new_date
        for v in (d.get("tickers") or {}).values():
            v["as_of_close_date"] = new_date
        H.write_json(p, d)


def _first_run(state, run_id="20261010-0830"):
    rd = H.make_run(state, run_id)
    assert L.main(["--mark", "--run-id", run_id]) == 0
    return rd


# aggregate.py's validated position (E2 is not in evidence.json -> dropped from evidence_ids)
_VALID_NVDA = {"ticker": "NVDA", "action": "ENTER", "reason_code": "GUIDANCE_RAISE", "evidence_ids": ["E1"],
               "horizon_weeks": 8, "kill_conditions": [{"type": "PRICE_BELOW", "value": 205.0},
                                                       {"type": "DATE", "value": "2026-12-05"}],
               "thesis_30w": "mock", "expected_excess_vs_qqq_pp": 3.0}


def _log_nvda_entry(state, run_id, rd):
    """Scenario A by hand (contract shapes): NVDA 0.13 2/3, D3 core-only; real book --solve."""
    agg = {"schema": "aggregate/v6", "run_id": run_id, "valid": True, "regime": "UP",
           "as_of_close_date": "2026-10-07",
           "targets": {"CORE": 0.87, "SGOV": 0.0, "NVDA": 0.13},
           "sleeve": {"NVDA": {"weight": 0.13, "votes": 2, "weights": [0.13, 0.15, 0.0], "action": "ENTER",
                               "rule": "MEDIAN", "reason_codes": ["GUIDANCE_RAISE", "EARNINGS_BEAT_RAISE"],
                               "evidence_ids": ["E1"], "horizon_weeks": 8, "thesis_30w": "mock",
                               "kill_conditions": [{"type": "PRICE_BELOW", "value": 205.0},
                                                   {"type": "DATE", "value": "2026-12-05"}],
                               "kill_override": False}},
           "sgov": {"weight": 0.0, "votes": 0, "weights": [0, 0, 0], "rule": "NONE", "renewed": False},
           "exits": [], "no_majority": [], "drops": [], "clamps": [],
           "vote_table": [{"decider": d, "core": c, "sgov": 0.0, "sleeve": s, "p_qqq_up_20d": 0.6,
                           "dissent_note": "x", "unchanged": d == "D3_MACRO", "drops": 0,
                           "positions": ps}
                          for d, c, s, ps in (("D1_TREND", 0.87, {"NVDA": 0.13}, [_VALID_NVDA]),
                                              ("D2_FUNDAMENTAL", 0.85, {"NVDA": 0.15}, [_VALID_NVDA]),
                                              ("D3_MACRO", 1.0, {}, []))],
           "paper_targets": {"D1_TREND": {"CORE": 0.87, "NVDA": 0.13},
                             "D2_FUNDAMENTAL": {"CORE": 0.85, "NVDA": 0.15},
                             "D3_MACRO": {"CORE": 1.0}},
           "forced_sells": {}, "unchanged": False, "unchanged_weeks": 0, "noop_flag": False,
           "p_qqq_up_20d_median": 0.6, "event_scope": None}
    H.write_json(os.path.join(rd, "aggregate_out.json"), agg)
    import book
    assert book.main(["--solve", "--run-id", run_id]) == 0
    return agg


# ---------------------------------------------------------------- 22
def test_first_mark_writes_epoch_and_shadow_equals_account(state):
    R = "20261010-0830"
    _first_run(state, R)
    recs = L.load()
    assert _types(recs) == ["epoch", "mark"]
    ep, mk = recs
    assert ep["date"] == "2026-10-10" and ep["version"] == C.VERSION
    assert mk["date"] == "2026-10-07" and mk["source"] == "run"
    acct = mk["account"]
    assert acct["positions"] == {"QQQ": 2, "QQQM": 1}
    assert acct["total_usd"] == pytest.approx(1895.99, abs=0.01)
    for k in ("positions", "cash_usd", "total_usd"):
        assert mk["shadow"][k] == acct[k]
        for d in C.DECIDER_IDS:
            assert mk["paper"][d][k] == acct[k]
    # second run mark for the same session is refused; --force-remark supersedes
    assert L.main(["--mark", "--run-id", R]) == 5
    assert len(L.load()) == 2
    assert L.main(["--mark", "--run-id", R, "--force-remark"]) == 0
    recs = L.load()
    assert recs[-1]["supersedes"] == mk["ts_utc"]
    assert len(L.marks(recs)) == 1
    dg = L.digest(recs)
    assert dg["epoch_start"] == "2026-10-10" and dg["first_run"] is True
    assert dg["scoreboard"]["weeks_of_data"] == 1 and dg["triggers"]["authority"] == "FULL"


def test_append_validates_and_load_skips_corrupt(state):
    with pytest.raises(ValueError):
        L.append({"type": "bogus"})
    L.append({"type": "flow", "date": "2026-10-10", "run_id": "r", "usd": 1.0})
    with open(C.LEDGER_JSONL, "a") as f:
        f.write("{not json\n\n")                    # corrupt + blank, then a torn tail
        f.write('{"type": "flow", "usd": 2')
    L.append({"type": "flow", "date": "2026-10-11", "run_id": "r", "usd": 3.0})
    recs = L.load()
    assert [r["usd"] for r in recs] == [1.0, 3.0]
    assert L.load.corrupt == 2
    assert all("ts_utc" in r for r in recs)


def test_digest_cli_on_absent_ledger_matches_first_run_defaults(state):
    R = "20261010-0830"
    assert L.main(["--digest", "--run-id", R]) == 0
    dg = H.read_json(os.path.join(C.run_dir(R), "digest.json"))
    ref = H.first_run_digest(R)
    for k, v in ref.items():
        if k == "triggers":
            for kk, vv in v.items():
                if isinstance(vv, dict):
                    assert {x: dg[k][kk][x] for x in vv} == vv, kk
                else:
                    assert dg[k][kk] == vv, kk
        elif k == "brier":
            assert {d: {x: dg[k][d][x] for x in vv} for d, vv in v.items()} == v
        else:
            assert dg[k] == v, k
    assert not os.path.exists(C.LEDGER_JSONL)


# ---------------------------------------------------------------- 23
def test_twr_with_flows_matches_hand_calc(state):
    pts = [("2026-10-01", 1000.0), ("2026-10-02", 1100.0), ("2026-10-03", 1050.0)]
    fl = {"2026-10-02": 100.0}
    assert L.twr(pts[:2], fl) == pytest.approx(0.0, abs=1e-9)
    pct, pnl, net = L.twr_detail(pts, fl)
    assert pct == pytest.approx(-4.5455, abs=1e-4)
    assert round(pct, 2) == -4.55
    assert net == 100.0 and pnl == pytest.approx(-50.0)
    # same thing from ledger lines: a flow dated the receiving mark's day
    recs = [{"type": "mark", "date": d, "account": {"total_usd": v}} for d, v in pts]
    recs.insert(1, {"type": "flow", "date": "2026-10-02", "usd": 100.0})
    assert L.flows(recs) == {"2026-10-02": 100.0}
    assert L.twr(L.marks(recs), L.flows(recs)) == pytest.approx(-4.5455, abs=1e-4)
    # a KST-dated flow without receive_date lands on the first mark on/after its date
    recs2 = [{"type": "mark", "date": "2026-10-09", "account": {"total_usd": 1}},
             {"type": "flow", "date": "2026-10-10", "usd": 5.0},
             {"type": "mark", "date": "2026-10-12", "account": {"total_usd": 1}}]
    assert L.flows(recs2) == {"2026-10-12": 5.0}


# ---------------------------------------------------------------- 24
def test_shadow_rolls_on_deposit_with_commission(state):
    _first_run(state)
    R2 = "20261017-0830"
    rd2 = H.make_run(state, R2, holdings_text="QQQ 2, QQQM 1, cash 468.59",
                     digest=L.digest(L.load()))
    _bump_as_of(rd2, "2026-10-16")
    assert L.main(["--mark", "--run-id", R2, "--deposit", "400"]) == 0
    recs = L.load()
    assert _types(recs) == ["epoch", "mark", "flow", "mark"]
    flow, mk = recs[2], recs[3]
    assert flow["usd"] == 400.0 and flow["receive_date"] == "2026-10-16"
    sh = mk["shadow"]
    assert sh["orders"] == [{"ticker": "QQQM", "action": "BUY", "shares": 1, "limit_ref_price": 311.94}]
    assert sh["positions"] == {"QQQ": 2, "QQQM": 2}
    assert sh["cash_usd"] == pytest.approx(468.59 - 311.94 * (1 + C.COMMISSION_PCT), abs=0.01)
    # no decision yet -> paper books just receive the flow
    for d in C.DECIDER_IDS:
        assert mk["paper"][d]["cash_usd"] == pytest.approx(468.59)
    # TWR removes the deposit: same prices -> account ~0%, shadow pays only commission
    sb = L.scoreboard(recs)
    assert sb["account_twr_pct"] == pytest.approx(0.0, abs=1e-6)
    assert sb["value_add_pp"] == pytest.approx(311.94 * C.COMMISSION_PCT / 1895.99 * 100, abs=2e-4)


# ---------------------------------------------------------------- 25
def test_log_appends_votes_orders_decision_and_resets_triggers(state):
    R = "20261010-0830"
    rd = _first_run(state, R)
    H.write_json(L.triggers_path(), {"schema": "triggers/v6", "events": [
        {"kind": "SHOCK_MOVE", "ticker": "QQQ", "severity": "HIGH", "detail": {}, "first_seen": "2026-10-09"}],
        "event_run_needed": True, "affected_tickers": ["QQQ"], "consumed_by_run": None})
    os.makedirs(os.path.join(rd, "deciders"), exist_ok=True)
    shutil.copy(os.path.join(FIX_A, "deciders", "D1_TREND.json"), os.path.join(rd, "deciders"))
    _log_nvda_entry(state, R, rd)
    od = H.read_json(os.path.join(rd, "orders.json"))
    assert [(o["action"], o["ticker"], o["shares"]) for o in od["orders"]] == [("SELL", "QQQM", 1), ("BUY", "NVDA", 1)]

    assert L.main(["--log", "--run-id", R]) == 0
    recs = L.load()
    new = recs[2:]
    assert _types(new) == ["vote"] * 3 + ["order"] * 2 + ["decision"]
    assert [v["decider_id"] for v in new[:3]] == list(C.DECIDER_IDS)
    assert new[0]["positions"][0]["ticker"] == "NVDA" and new[0]["p_qqq_up_20d"] == 0.6
    assert new[0]["positions"][0]["evidence_ids"] == ["E1"]                 # validated (aggregate)
    assert new[0]["positions_raw"][0]["evidence_ids"] == ["E1", "E2"]       # as the decider wrote it
    assert new[2]["positions"] == [] and new[2]["positions_raw"] == []
    assert all(o["status"] == "RECOMMENDED" for o in new[3:5])
    dec = new[-1]
    th = dec["theses"][0]
    assert th["ticker"] == "NVDA" and th["entered_date"] == "2026-10-10"
    assert th["horizon_end_date"] == (date(2026, 10, 10) + timedelta(weeks=8)).isoformat()
    assert th["entry_price"] == pytest.approx(237.47)
    assert dec["sgov"] == {"weight": 0.0, "entered_run_id": None, "runs_held": 0}
    assert dec["event_consumed"] == [{"kind": "SHOCK_MOVE", "ticker": "QQQ"}]
    assert dec["post_cash_weight"] == pytest.approx(0.0755, abs=1e-3)
    # paper books solved at today's prices: D1 holds NVDA, D3 stays core-only
    assert dec["paper_books"]["D1_TREND"]["positions"].get("NVDA") == 1
    assert "NVDA" not in dec["paper_books"]["D3_MACRO"]["positions"]
    tj = H.read_json(L.triggers_path())
    assert tj["events"] == [] and tj["consumed_by_run"] == R and tj["event_run_needed"] is False
    # logging the same run twice is refused
    assert L.main(["--log", "--run-id", R]) == 5
    # digest after the log
    dg = L.digest(L.load(), indicators=H.read_json(os.path.join(rd, "indicators.json")))
    ot = dg["open_theses"][0]
    assert ot["ticker"] == "NVDA" and ot["kill_fired"] is False
    assert {k["type"]: k["status"] for k in ot["kill_conditions"]} == {"PRICE_BELOW": "OK", "DATE": "OK"}
    assert [(p["ticker"], p["runs_unfilled"]) for p in dg["pending_orders"]] == [("QQQM", 1), ("NVDA", 1)]
    assert dg["first_run"] is False and dg["last_targets"]["NVDA"] == 0.13


def test_log_refuses_invalid_or_missing_inputs(state):
    R = "20261010-0830"
    rd = _first_run(state, R)
    assert L.main(["--log", "--run-id", R]) == 2                  # no aggregate_out
    H.write_json(os.path.join(rd, "aggregate_out.json"), {"valid": False})
    assert L.main(["--log", "--run-id", R]) == 2
    H.write_json(os.path.join(rd, "aggregate_out.json"), {"valid": True, "targets": {"CORE": 1.0}})
    assert L.main(["--log", "--run-id", R]) == 2                  # no orders.json
    assert _types(L.load()) == ["epoch", "mark"]


# ---------------------------------------------------------------- 26
def test_digest_pending_orders_fill_and_t5(state):
    R1 = "20261010-0830"
    rd1 = _first_run(state, R1)
    _log_nvda_entry(state, R1, rd1)
    assert L.main(["--log", "--run-id", R1]) == 0
    dg = L.digest(L.load())
    assert [p["runs_unfilled"] for p in dg["pending_orders"]] == [1, 1]
    assert dg["triggers"]["T5"]["fire"] is False

    # run 2: Theo did not trade -> book reconciliation says UNFILLED -> fill lines
    R2 = "20261017-0830"
    rd2 = H.make_run(state, R2, digest=L.digest(L.load()))
    _bump_as_of(rd2, "2026-10-16")
    assert len(H.read_json(os.path.join(rd2, "book.json"))["reconciliation"]["unfilled"]) == 2
    assert L.main(["--mark", "--run-id", R2]) == 0
    fills = [r for r in L.load() if r["type"] == "fill"]
    assert [(f["ticker"], f["status"], f["runs_unfilled"], f["ref_run_id"]) for f in fills] == [
        ("QQQM", "UNFILLED", 2, R1), ("NVDA", "UNFILLED", 2, R1)]
    dg = L.digest(L.load())
    assert [p["runs_unfilled"] for p in dg["pending_orders"]] == [2, 2]
    assert dg["triggers"]["T5"]["fire"] is True
    assert {u["ticker"] for u in dg["triggers"]["T5"]["unfilled"]} == {"QQQM", "NVDA"}

    # run 3: both orders filled -> nothing pending, T5 clear
    R3 = "20261024-0830"
    rd3 = H.make_run(state, R3, holdings_text="QQQ 2, NVDA 1, cash 143.06", digest=L.digest(L.load()))
    _bump_as_of(rd3, "2026-10-23")
    assert L.main(["--mark", "--run-id", R3]) == 0
    st = [r["status"] for r in L.load() if r["type"] == "fill"]
    assert st[-2:] == ["FILLED", "FILLED"]
    dg = L.digest(L.load())
    assert dg["pending_orders"] == [] and dg["triggers"]["T5"]["fire"] is False


def test_pending_order_carries_runs_unfilled_when_re_recommended():
    recs = [{"type": "decision", "run_id": "r1"},
            {"type": "order", "run_id": "r1", "ticker": "NVDA", "action": "BUY", "shares": 1},
            {"type": "fill", "run_id": "r2", "ref_run_id": "r1", "ticker": "NVDA", "action": "BUY",
             "status": "UNFILLED"},
            {"type": "decision", "run_id": "r2"},
            {"type": "order", "run_id": "r2", "ticker": "NVDA", "action": "BUY", "shares": 1}]
    assert L.pending_orders(recs)[0]["runs_unfilled"] == 2
    recs.append({"type": "decision", "run_id": "r3"})            # run 3 decides no orders
    assert L.pending_orders(recs) == []


# ---------------------------------------------------------------- 27
def _synthetic_ledger(path, weeks, acct_end, start="2026-01-05"):
    """epoch + `weeks` weekly marks; shadow flat at 1000, account linear 1000 -> acct_end."""
    d0 = date.fromisoformat(start)
    L.append({"type": "epoch", "date": start, "run_id": "e", "total_usd": 1000.0}, path)
    for i in range(weeks):
        a = 1000.0 + (acct_end - 1000.0) * i / (weeks - 1)
        L.append({"type": "mark", "date": (d0 + timedelta(weeks=i)).isoformat(), "run_id": f"m{i}",
                  "source": "run", "qqq_px": 500.0,
                  "account": {"positions": {}, "cash_usd": a, "total_usd": a},
                  "shadow": {"positions": {}, "cash_usd": 1000.0, "total_usd": 1000.0}, "paper": {}}, path)


def test_t1_t2_authority_from_synthetic_marks(state, tmp_path):
    p1 = str(tmp_path / "t1" / "ledger.jsonl")
    _synthetic_ledger(p1, 13, 960.0)
    dg = L.digest(L.load(p1))
    assert dg["scoreboard"]["weeks_of_data"] == 13
    assert dg["scoreboard"]["value_add_12w_pp"] == pytest.approx(-4.0, abs=1e-6)
    t = dg["triggers"]
    assert t["T1"]["active"] and t["authority"] == "T1_REDUCED"
    assert t["sleeve_max_pct_effective"] == C.T1_SLEEVE_CUT == 0.15
    last = date(2026, 1, 5) + timedelta(weeks=12)
    assert t["T1"]["until"] == (last + timedelta(weeks=C.T1_DURATION_WEEKS)).isoformat()
    assert t["sgov_authority"] is True
    # 12 marks are not enough (trailing 12w needs 12 weeks of data)
    p0 = str(tmp_path / "t0" / "ledger.jsonl")
    _synthetic_ledger(p0, 11, 960.0)
    assert L.digest(L.load(p0))["triggers"]["authority"] == "FULL"

    p2 = str(tmp_path / "t2" / "ledger.jsonl")
    _synthetic_ledger(p2, 27, 940.0)
    dg = L.digest(L.load(p2))
    assert dg["scoreboard"]["value_add_26w_pp"] == pytest.approx(-6.0, abs=1e-6)
    assert dg["triggers"]["T2"]["active"] and dg["triggers"]["authority"] == "T2_SUSPENDED"
    assert dg["triggers"]["sleeve_max_pct_effective"] == 0.0 and dg["triggers"]["sgov_authority"] is False
    # wrong confirm string refused, nothing appended
    n = len(L.load(p2))
    assert L.main(["--clear-t2", "--run-id", "20260720-0900", "--ledger", p2, "--confirm", "yes"]) == 2
    assert L.main(["--clear-t2", "--run-id", "20260720-0900", "--ledger", p2]) == 2
    assert len(L.load(p2)) == n
    assert L.main(["--clear-t2", "--run-id", "20260720-0900", "--ledger", p2, "--confirm", "re-enable"]) == 0
    recs = L.load(p2)
    assert recs[-1]["kind"] == "T2" and recs[-1]["state"] == "CLEARED"
    dg = L.digest(recs)
    assert dg["triggers"]["authority"] == "FULL" and dg["triggers"]["T2"]["active"] is False


def test_t1_persists_until_and_log_writes_edges(state):
    """A persisted T1 FIRED line keeps authority reduced until `until` even after recovery."""
    recs = [{"type": "trigger", "kind": "T1", "state": "FIRED", "date": "2026-10-10",
             "until": "2026-12-05", "detail": {"since": "2026-10-09"}}]
    part = {"scoreboard": {"weeks_of_data": 20, "value_add_12w_pp": 1.0, "value_add_26w_pp": None},
            "pending_orders": [], "as_of_close_date": "2026-11-20"}
    t = L.evaluate_triggers(recs, part)
    assert t["T1"]["active"] and t["authority"] == "T1_REDUCED" and t["T1"]["until"] == "2026-12-05"
    part["as_of_close_date"] = "2026-12-07"
    assert L.evaluate_triggers(recs, part)["authority"] == "FULL"


def test_t4_counts_consecutive_cash_heavy_decisions():
    recs = [{"type": "decision", "post_cash_weight": 0.12, "sgov": {"weight": 0.0}},
            {"type": "decision", "post_cash_weight": 0.05, "sgov": {"weight": 0.0}},
            {"type": "decision", "post_cash_weight": 0.12, "sgov": {"weight": 0.0}},
            {"type": "decision", "post_cash_weight": 0.11, "sgov": {"weight": 0.0}}]
    part = {"scoreboard": {}, "pending_orders": [], "as_of_close_date": "2026-10-09"}
    t4 = L.evaluate_triggers(recs, part)["T4"]
    assert t4 == {"cash_over_runs": 2, "fire": True}
    recs[-1]["sgov"]["weight"] = 0.1
    assert L.evaluate_triggers(recs, part)["T4"]["fire"] is False


def test_brier_logged_but_hidden_until_min_n():
    recs = [{"type": "mark", "date": "2026-10-09", "run_id": "r1", "qqq_px": 100.0, "account": {"total_usd": 1}},
            {"type": "vote", "date": "2026-10-10", "run_id": "r1", "decider_id": "D1_TREND", "p_qqq_up_20d": 0.7},
            {"type": "mark", "date": "2026-11-09", "run_id": "WD-20261109", "qqq_px": 105.0,
             "account": {"total_usd": 1}}]
    b = L.brier(recs)["D1_TREND"]
    assert b["n"] == 1 and b["display"] is False and b["score"] is None
    assert b["raw_score"] == pytest.approx(0.09)


def test_sessions_held_busdays():
    assert L.sessions_held("2026-10-10", "2026-11-09") == 20
    assert L.sessions_held("2026-10-12", "2026-10-12") == 0


def test_in_epoch_deposit_is_never_a_twr_flow(state):
    R = "20261010-0830"
    H.make_run(state, R)
    assert L.main(["--mark", "--run-id", R, "--deposit", "400"]) == 0
    recs = L.load()
    assert _types(recs) == ["epoch", "flow", "mark"] and recs[1]["in_epoch"] is True
    recs.append({"type": "mark", "date": "2026-10-12", "source": "watchdog",
                 "account": {"total_usd": recs[2]["account"]["total_usd"]}})
    assert L.flows(recs) == {}
    assert L.scoreboard(recs)["account_twr_pct"] == pytest.approx(0.0)


def test_force_remark_does_not_duplicate_fills_or_flows(state):
    R1 = "20261010-0830"
    rd1 = _first_run(state, R1)
    _log_nvda_entry(state, R1, rd1)
    assert L.main(["--log", "--run-id", R1]) == 0
    R2 = "20261017-0830"
    rd2 = H.make_run(state, R2, holdings_text="QQQ 2, QQQM 1, cash 468.59", digest=L.digest(L.load()))
    _bump_as_of(rd2, "2026-10-16")
    assert L.main(["--mark", "--run-id", R2, "--deposit", "400"]) == 0
    sh1 = L.load()[-1]["shadow"]
    # a second deposit for the same run is refused; a plain re-mark re-uses the recorded flow
    assert L.main(["--mark", "--run-id", R2, "--force-remark", "--deposit", "400"]) == 2
    assert L.main(["--mark", "--run-id", R2, "--force-remark"]) == 0
    recs = L.load()
    assert sum(1 for r in recs if r["type"] == "fill") == 2
    assert sum(1 for r in recs if r["type"] == "flow") == 1
    assert recs[-1]["shadow"]["cash_usd"] == pytest.approx(sh1["cash_usd"])
    assert recs[-1]["shadow"]["positions"] == sh1["positions"]
    assert [p["runs_unfilled"] for p in L.digest(recs)["pending_orders"]] == [2, 2]
    assert L.flows(recs) == {"2026-10-16": 400.0}


def test_clear_t2_refused_when_t2_not_active(state):
    _first_run(state)
    assert L.main(["--clear-t2", "--run-id", "20261010-0830", "--confirm", "re-enable"]) == 5
    assert not any(r["type"] == "trigger" for r in L.load())


def test_digest_uses_kst_run_date_for_horizon_and_min_hold(state):
    recs = [{"type": "mark", "date": "2026-12-04", "run_id": "20261205-0830", "source": "run",
             "account": {"positions": {"NVDA": 1}, "cash_usd": 0, "total_usd": 240.0}},
            {"type": "decision", "run_id": "20261010-0830", "theses": [
                {"ticker": "NVDA", "status": "OPEN", "entered_date": "2026-10-10",
                 "horizon_end_date": "2026-12-05", "kill_conditions": [{"type": "DATE", "value": "2026-12-05"}]}]}]
    ot = L.digest(recs, run_id="20261205-0830")["open_theses"][0]
    assert ot["horizon_reached"] is True and ot["kill_conditions"][0]["status"] == "FIRED"
    assert ot["sessions_held"] == 40 and ot["min_hold_met"] is True


# ---------------------------------------------------------------- review fixes (v6.0 integration)
def test_log_vote_positions_are_validated_and_raw_kept(state):
    """Vote lines record what aggregate.py counted; the decider's raw entries go to positions_raw."""
    R = "20261010-0830"
    rd = _first_run(state, R)
    os.makedirs(os.path.join(rd, "deciders"), exist_ok=True)
    raw = H.read_json(os.path.join(FIX_A, "deciders", "D1_TREND.json"))
    raw["positions"].append({"ticker": "PLTR", "action": "ENTER", "reason_code": "VIBES",
                             "evidence_ids": [], "horizon_weeks": 4, "kill_conditions": []})
    H.write_json(os.path.join(rd, "deciders", "D1_TREND.json"), raw)
    _log_nvda_entry(state, R, rd)
    assert L.main(["--log", "--run-id", R]) == 0
    votes = {v["decider_id"]: v for v in L.load() if v["type"] == "vote"}
    d1 = votes["D1_TREND"]
    assert [p["ticker"] for p in d1["positions"]] == ["NVDA"]               # PLTR was dropped to core
    assert d1["positions"][0] == {k: _VALID_NVDA[k] for k in L.VOTE_POS_KEYS}
    assert [p["ticker"] for p in d1["positions_raw"]] == ["NVDA", "PLTR"]
    assert d1["positions_raw"][1]["reason_code"] == "VIBES"
    assert d1["positions_raw"][0]["kill_conditions"][-1]["type"] == "EVENT"
    # D2 has no file in the run dir: validated positions still come from the vote table
    assert [p["ticker"] for p in votes["D2_FUNDAMENTAL"]["positions"]] == ["NVDA"]
    assert votes["D2_FUNDAMENTAL"]["positions_raw"] == []


def _carry_nvda(run_id, *, rule="MEDIAN", action="HOLD", horizon_weeks=6, frozen=False):
    """aggregate_out + orders for a later run that keeps the NVDA thesis (no orders)."""
    rd = C.run_dir(run_id)
    os.makedirs(rd, exist_ok=True)
    kills = [{"type": "PRICE_BELOW", "value": 205.0}, {"type": "DATE", "value": "2027-01-16"}]
    H.write_json(os.path.join(rd, "aggregate_out.json"), {
        "schema": "aggregate/v6", "run_id": run_id, "valid": True, "regime": "UP",
        "targets": {"CORE": 0.87, "SGOV": 0.0, "NVDA": 0.13},
        "sleeve": {"NVDA": {"weight": 0.13, "votes": 2, "action": action, "rule": rule, "held": True,
                            "reason_codes": ["GUIDANCE_RAISE"], "evidence_ids": ["E9"],
                            "horizon_weeks": horizon_weeks, "kill_conditions": kills,
                            "thesis_30w": "extended", "kill_override": False, "frozen": frozen}},
        "sgov": {"weight": 0.0, "rule": "NONE", "renewed": False}, "exits": [], "drops": [], "clamps": [],
        "vote_table": [], "paper_targets": {}, "unchanged": True, "unchanged_weeks": 1})
    H.write_json(os.path.join(rd, "orders.json"), {"orders": [], "dropped_targets": [], "post_trade": {}})


def test_log_rebases_horizon_when_kept_past_it(state):
    R1 = "20261010-0830"
    rd = _first_run(state, R1)
    _log_nvda_entry(state, R1, rd)
    assert L.main(["--log", "--run-id", R1]) == 0
    end0 = (date(2026, 10, 10) + timedelta(weeks=8)).isoformat()            # 2026-12-05

    # before the horizon: a 2/3 HOLD carries the thesis unchanged
    R2 = "20261017-0830"
    _carry_nvda(R2)
    assert L.main(["--log", "--run-id", R2]) == 0
    th = L._last(L.load(), "decision")["theses"][0]
    assert th["horizon_end_date"] == end0 and th["horizon_weeks"] == 8
    assert th["entered_run_id"] == R1 and "horizon_rebased_run_id" not in th

    # an event run that froze NVDA (out of scope) past the horizon does not extend it
    R3 = "20261205-0830"
    _carry_nvda(R3, rule="OUT_OF_SCOPE", frozen=True)
    assert L.main(["--log", "--run-id", R3]) == 0
    th = L._last(L.load(), "decision")["theses"][0]
    assert th["horizon_end_date"] == end0
    assert L.digest(L.load(), run_id="20261206-0830")["open_theses"][0]["horizon_reached"] is True

    # past the horizon and kept by a real 2/3 vote: horizon re-based from this run's date
    R4 = "20261212-0830"
    _carry_nvda(R4, horizon_weeks=6)
    assert L.main(["--log", "--run-id", R4]) == 0
    th = L._last(L.load(), "decision")["theses"][0]
    assert th["horizon_end_date"] == (date(2026, 12, 12) + timedelta(weeks=6)).isoformat()
    assert th["horizon_weeks"] == 6 and th["horizon_rebased_run_id"] == R4
    assert th["entered_run_id"] == R1 and th["entered_date"] == "2026-10-10"   # still the same thesis
    ot = L.digest(L.load(), run_id="20261219-0830")["open_theses"][0]
    assert ot["horizon_reached"] is False


def test_sgov_runs_held_frozen_on_event_runs(state):
    """Review cycle 2: an event run that leaves SGOV out of scope must not burn a grace run."""
    R1 = "20261010-0830"
    rd = _first_run(state, R1)
    _log_nvda_entry(state, R1, rd)
    assert L.main(["--log", "--run-id", R1]) == 0

    def sgov_run(run_id, rule, renewed):
        _carry_nvda(run_id)
        p = os.path.join(C.run_dir(run_id), "aggregate_out.json")
        a = H.read_json(p)
        a["targets"] = {"CORE": 0.77, "SGOV": 0.10, "NVDA": 0.13}
        a["sgov"] = {"weight": 0.10, "rule": rule, "renewed": renewed}
        H.write_json(p, a)
        assert L.main(["--log", "--run-id", run_id]) == 0
        return L._last(L.load(), "decision")["sgov"]["runs_held"]

    assert sgov_run("20261017-0830", "MIN_UP", True) == 0          # entry
    assert sgov_run("20261024-0830", "INCUMBENT", False) == 1      # unrenewed weekly run
    assert sgov_run("20261027-1900", "OUT_OF_SCOPE", False) == 1   # event run: frozen
    assert sgov_run("20261031-0830", "INCUMBENT", False) == 2
