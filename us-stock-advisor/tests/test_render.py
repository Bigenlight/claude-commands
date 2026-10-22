"""Team 2 tests: render.py (plan test 21). Offline; state lives in tmp dirs only."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import date

from test_aggregate import (SCRIPTS, build_run, copy_deciders, first_run_digest, run_cli)  # noqa: F401

import config as C  # noqa: E402
import render as R  # noqa: E402

ORDERS_A = {"schema": "orders/v6", "orders": [
    {"ticker": "NVDA", "action": "BUY", "shares": 1, "limit_ref_price": 237.47, "usd": 237.47, "sleeve": "SLEEVE",
     "reason": "ENTER GUIDANCE_RAISE 2/3", "priority": 4},
    {"ticker": "QQQM", "action": "SELL", "shares": 1, "limit_ref_price": 311.94, "usd": 311.94, "sleeve": "CORE",
     "reason": "fund sleeve entry NVDA", "priority": 2}],
    "post_trade": {"positions": {"QQQ": 2, "NVDA": 1}, "cash_usd": 143.06, "cash_pct": 7.5,
                   "weights": {"CORE": 0.799, "NVDA": 0.125, "SGOV": 0.0, "CASH": 0.075}},
    "residual": {"cash_usd": 143.06, "cash_pct": 7.5, "unavoidable": True, "reason": "<=CASH_MAX_PCT"},
    "conversion_used": False, "lines": 2, "dropped_targets": [], "warnings": []}

ORDERS_NONE = {"schema": "orders/v6", "orders": [],
               "post_trade": {"positions": {"QQQ": 2, "QQQM": 1}, "cash_usd": 68.59, "cash_pct": 3.6, "weights": {}},
               "residual": {"cash_usd": 68.59, "cash_pct": 3.6, "unavoidable": True, "reason": "<=CASH_MAX_PCT"},
               "conversion_used": False, "lines": 0, "dropped_targets": [], "warnings": []}


def _scenario(state, run_id, scenario="mock_run", orders=ORDERS_A, digest=None):
    rd = build_run(state, run_id, digest=digest)
    copy_deciders(rd, scenario)
    r = run_cli(state, run_id)
    assert r.returncode == 0, r.stderr
    o = dict(orders, run_id=run_id)
    (rd / "orders.json").write_text(json.dumps(o))
    return rd


def _render_cli(state, run_id, *extra):
    env = dict(os.environ, US_ADVISOR_STATE=str(state), PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run([sys.executable, str(SCRIPTS / "render.py"), "--run-id", run_id, *extra],
                          env=env, capture_output=True, text=True, timeout=60)


def test_render_header_lines(tmp_path):
    state = tmp_path / "state"
    rd = _scenario(state, "20261008-1900")                       # Thursday KST
    r = _render_cli(state, "20261008-1900")
    assert r.returncode == 0, r.stderr
    lines = r.stdout.rstrip("\n").split("\n")
    assert len(lines) == 5
    assert (rd / "header.txt").read_text().rstrip("\n").split("\n") == lines
    assert R.header(str(rd)).split("\n") == lines
    l1, l2, l3, l4, l5 = lines
    assert l1.startswith("👉 이번 주 행동: ")
    assert "QQQM 1주 매도 → NVDA 1주 매수 (~$237)" in l1          # sells first (priority), buys carry ~$
    assert "현금 $143" in l1 and "(불가피 잔여)" in l1
    assert l2.startswith("📊 v6 기준 2026-10-08~: ")
    assert "계좌 +0.0%" in l2 and "QQQ +0.0%" in l2 and "LLM 기여 +0.0%p" in l2
    assert "트리거 정상" in l2 and "표본 1주" in l2 and "실력 판정 불가" in l2
    assert "NVDA 진입 2/3" in l3 and "SGOV 0/3" in l3 and "레짐 UP" in l3
    assert "변경없음 D3_MACRO" in l3 and "QQQ>SMA200" in l3 and "월봉>10M SMA" in l3 and "드롭" not in l3
    assert l4.startswith("⏭️ 다음 정기: 10/10(토)")
    assert "NVDA<$205" in l4 and "QQQ<SMA200" in l4
    assert l5 == "⚠️ 미체결: 없음"

    # --json prints fields and still writes header.txt
    r = _render_cli(state, "20261008-1900", "--json")
    assert r.returncode == 0 and json.loads(r.stdout)["lines"] == lines

    # Saturday run -> the following Saturday
    rd_sat = state / "run" / "20261010-0830"
    shutil.copytree(rd, rd_sat)
    agg = json.loads((rd_sat / "aggregate_out.json").read_text())
    agg["run_id"] = "20261010-0830"
    (rd_sat / "aggregate_out.json").write_text(json.dumps(agg))
    assert R.header(str(rd_sat)).split("\n")[3].startswith("⏭️ 다음 정기: 10/17(토)")
    assert R.next_regular(date(2026, 10, 8)) == date(2026, 10, 10)
    assert R.next_regular(date(2026, 10, 10)) == date(2026, 10, 17)
    assert R.next_regular(date(2026, 10, 11)) == date(2026, 10, 17)


def test_render_t2_t1_unfilled_and_noop(tmp_path):
    state = tmp_path / "state"
    dg = first_run_digest(first_run=False, epoch_start="2026-10-10")
    dg["triggers"]["authority"] = "T2_SUSPENDED"
    dg["triggers"]["T2"] = {"active": True, "since": "2027-04-10"}
    dg["scoreboard"].update({"account_twr_pct": -1.23, "qqq_pct": 4.5, "value_add_pp": -6.04, "weeks_of_data": 27})
    rd = _scenario(state, "20270410-0830", digest=dg)
    lines = R.header(str(rd)).split("\n")
    assert "트리거 T2 중단 🔴" in lines[1] and "표본 27주" in lines[1]
    assert "계좌 -1.2%" in lines[1] and "QQQ +4.5%" in lines[1] and "LLM 기여 -6.0%p" in lines[1]
    assert lines[1].startswith("📊 v6 기준 2026-10-10~")

    dg["triggers"]["authority"] = "T1_REDUCED"
    dg["triggers"]["T1"] = {"active": True, "since": "2027-02-13", "until": "2027-04-10"}
    dg["triggers"]["T5"] = {"unfilled": [{"ticker": "NVDA", "action": "BUY", "shares": 1, "runs_unfilled": 2}],
                            "fire": True}
    dg["triggers"]["T4"] = {"cash_over_runs": 2, "fire": True}
    (rd / "digest.json").write_text(json.dumps(dg))
    lines = R.header(str(rd)).split("\n")
    assert "트리거 T1 축소(~4/10)" in lines[1]
    assert lines[4].startswith("⚠️ 미체결: NVDA 매수 1주 (2회차)")
    assert f"{C.T4_CASH_WEEKS}주 연속 → QQQM 자동 채움" in lines[4]

    # zero orders (split scenario) + no-op flag + drops
    dg2 = first_run_digest(unchanged_weeks=C.NOOP_FLAG_WEEKS - 1)
    rd2 = _scenario(state, "20261017-0830", scenario="mock_run_split", orders=ORDERS_NONE, digest=dg2)
    agg = json.loads((rd2 / "aggregate_out.json").read_text())
    agg["drops"] = [{"decider": "D1_TREND", "ticker": "TSLA", "rule": "NON_MENU_TICKER", "detail": ""}]
    (rd2 / "aggregate_out.json").write_text(json.dumps(agg))
    lines = R.header(str(rd2)).split("\n")
    assert lines[0] == "👉 이번 주 행동: 변경 없음 (코어 유지) · 체결 후 현금 $69 (불가피 잔여)"
    assert "SGOV 0/3" in lines[2] and "진입" not in lines[2]
    assert "드롭 1건" in lines[2] and f"노옵 {C.NOOP_FLAG_WEEKS}주 ⚠️" in lines[2]
    assert "변경없음 없음" in lines[2]                       # each decider proposed a name


def test_render_refuses_without_orders(tmp_path):
    state = tmp_path / "state"
    rd = _scenario(state, "20261008-1900")
    (rd / "orders.json").unlink()
    r = _render_cli(state, "20261008-1900")
    assert r.returncode == 2 and not (rd / "header.txt").exists()


def test_end_to_end_with_helpers_and_solver(tmp_path):
    """Team 1's make_run + real book.py --solve: scenario A gives the pinned orders and header line 1;
    a held 1-share NVDA voted HOLD (one decider silent) is not sold by share rounding."""
    import helpers
    state = tmp_path / "state"
    env = dict(os.environ, US_ADVISOR_STATE=str(state), PYTHONDONTWRITEBYTECODE="1")

    def pipeline(rid, scenario_docs):
        rd = helpers.make_run(str(state), rid) if scenario_docs[1] is None else \
            helpers.make_run(str(state), rid, holdings_text=scenario_docs[1])
        os.makedirs(os.path.join(rd, "deciders"), exist_ok=True)
        shutil.copy(SCRIPTS.parent / "tests" / "fixtures" / "mock_run" / "evidence.json", rd)
        for did, doc in scenario_docs[0].items():
            with open(os.path.join(rd, "deciders", f"{did}.json"), "w") as f:
                f.write(doc if isinstance(doc, str) else json.dumps(doc))
        for cmd in (["aggregate.py"], ["book.py", "--solve"], ["render.py"]):
            r = subprocess.run([sys.executable, str(SCRIPTS / cmd[0]), *cmd[1:], "--run-id", rid],
                               env=env, capture_output=True, text=True, timeout=120)
            assert r.returncode == 0, (cmd, r.stderr)
        return rd

    fx = SCRIPTS.parent / "tests" / "fixtures" / "mock_run" / "deciders"
    docs_a = {p.stem: p.read_text() for p in fx.glob("*.json")}
    rd = pipeline("20261008-1900", (docs_a, None))
    orders = json.load(open(os.path.join(rd, "orders.json")))
    assert [(o["action"], o["ticker"], o["shares"]) for o in orders["orders"]] == [("SELL", "QQQM", 1), ("BUY", "NVDA", 1)]
    head = open(os.path.join(rd, "header.txt")).read().split("\n")
    assert head[0] == "👉 이번 주 행동: QQQM 1주 매도 → NVDA 1주 매수 (~$237) · 체결 후 현금 $143 (불가피 잔여)"

    hold = {"ticker": "NVDA", "action": "HOLD", "reason_code": "GUIDANCE_RAISE", "evidence_ids": [],
            "horizon_weeks": 8, "kill_conditions": [{"type": "PRICE_BELOW", "value": 205.0}]}
    mk = lambda d, w: {"decider_id": d, "target_weights": {"CORE": 1 - w, "NVDA": w},  # noqa: E731
                       "positions": [hold] if w else [], "derisk": {"sgov_weight": 0.0}, "p_qqq_up_20d": 0.5}
    docs_h = {"D1_TREND": mk("D1_TREND", 0.125), "D2_FUNDAMENTAL": mk("D2_FUNDAMENTAL", 0.125),
              "D3_MACRO": mk("D3_MACRO", 0.0)}
    rd = pipeline("20261017-0830", (docs_h, "QQQ 2, NVDA 1, cash 143.06"))
    agg = json.load(open(os.path.join(rd, "aggregate_out.json")))
    assert agg["sleeve"]["NVDA"]["action"] == "HOLD" and agg["unchanged"] is True
    assert json.load(open(os.path.join(rd, "orders.json")))["orders"] == []


def test_render_forced_exit_labels_and_earnings_warn_window(tmp_path):
    """Review issue F: forced exits are labelled by `why`; the earnings watch uses EARNINGS_WARN_SESSIONS."""
    state = tmp_path / "state"
    rd = _scenario(state, "20261010-0830")
    agg_p = rd / "aggregate_out.json"
    base = json.loads(agg_p.read_text())

    def line(n, mutate):
        a = json.loads(json.dumps(base))
        mutate(a)
        agg_p.write_text(json.dumps(a))
        return R.header(str(rd)).split("\n")[n]

    def forced(why, action="EXIT"):
        def m(a):
            a["sleeve"]["NVDA"].update(action=action, why=why, weight=0.0 if action == "EXIT" else 0.1,
                                       exit_votes=0, votes=0)
        return m
    assert "NVDA 청산(킬) 0/3" in line(2, forced("KILL_FIRED"))
    assert "NVDA 청산(만기) 0/3" in line(2, forced("HORIZON"))
    assert "NVDA 축소 0/3" in line(2, forced("CAP_DRIFT", "TRIM"))
    assert "NVDA 청산(한도) 0/3" in line(2, forced("CAP_DRIFT"))  # full CAP_DRIFT sale
    voted = line(2, forced("VOTES_2_3"))
    assert "NVDA 청산 0/3" in voted and "청산(" not in voted

    def sgov_expired(a):
        a["exits"] = [{"ticker": "SGOV", "why": "SGOV_EXPIRED"}]
    assert "SGOV 청산(만료) 0/3" in line(2, sgov_expired)

    # earnings inside the menu window but outside the warn window -> no watchdog label
    agg_p.write_text(json.dumps(base))
    ind_p = rd / "indicators.json"
    ind = json.loads(ind_p.read_text())
    ind["tickers"]["NVDA"].update(next_earnings_date="2026-10-20", earnings_in_sessions=C.EARNINGS_WARN_SESSIONS + 3)
    assert C.EARNINGS_WARN_SESSIONS + 3 <= C.EARNINGS_MENU_SESSIONS
    ind_p.write_text(json.dumps(ind))
    assert "실적" not in R.header(str(rd)).split("\n")[3]
    ind["tickers"]["NVDA"]["earnings_in_sessions"] = C.EARNINGS_WARN_SESSIONS
    ind_p.write_text(json.dumps(ind))
    assert f"실적 D-{C.EARNINGS_WARN_SESSIONS} NVDA" in R.header(str(rd)).split("\n")[3]
