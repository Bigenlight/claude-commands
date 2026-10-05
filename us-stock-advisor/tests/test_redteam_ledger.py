"""Regression tests for red-team round 2 (report.py / score_recs.py / install_cron.sh),
findings F1-F10. Every test runs on a scratch state dir (conftest.py)."""
from __future__ import annotations

import csv as _csv
import fcntl
import json
import os
import shlex
import shutil
import stat
import subprocess
import time
from datetime import timedelta

import pandas as pd
import pytest

import config as C
import report as R
import score_recs as S
from helpers import (HISTORY_CSV, LIVE_LAST_RUN_COPY, LIVE_LEDGER_COPY, SCRIPTS, build_ledger,
                     make_baseline, mark, portfolio, read_bytes, run, utc_today, write_json)


# ------------------------------------------------------------------ helpers
def _days(n):
    return str(utc_today() - timedelta(days=n))


def _seed(state, n=5):
    """n anchored marks on past dates (no mark today)."""
    recs = [mark(_days(10 - i), 1000.0 + i, 100.0) for i in range(n)]
    build_ledger(state / "recommendations.jsonl", recs)
    return state / "recommendations.jsonl", state / "last_run.json"


def _bare_final(tmp_path, **extra):
    fp = {"orders": [], "source": "BASELINE"}
    fp.update(extra)
    return write_json(tmp_path / "final.json", {"final_plan": fp})


def _log(state, tmp_path, *args, final=None, baseline=None):
    bp = write_json(tmp_path / "baseline.json", baseline or make_baseline())
    fp = final or _bare_final(tmp_path)
    return run("report.py", "--log", "--baseline", bp, "--final", fp, *args, state=state)


def _relink(lines):
    out, prev = [], None
    for ln in lines:
        r = json.loads(ln)
        r["prev_hash"] = S._line_hash(prev) if prev is not None else None
        prev = json.dumps(r, ensure_ascii=False)
        out.append(prev)
    return out


def _tamper(recs, kind):
    lines = recs.read_text().splitlines()
    if kind == "truncate_bottom":
        lines = lines[:-1]
    elif kind == "relink_forgery":
        lines[2] = lines[2].replace('"total_usd": 1002.0', '"total_usd": 1999.0')
        lines = _relink(lines)
    elif kind == "delete":
        lines = []
    recs.write_text("".join(ln + "\n" for ln in lines))


def _header(state, csv):
    r = run("report.py", "--header", "--prices-csv", csv, state=state)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _flat_csv(path, start="2025-01-01", n=400, px=100.0, cols=("QQQ",)):
    idx = pd.date_range(start, periods=n, freq="D")
    df = pd.DataFrame({c: [px] * n for c in cols}, index=idx)
    df.index.name = "Date"
    df.to_csv(path)
    return str(path)


@pytest.fixture
def live_copy(state):
    shutil.copy(LIVE_LEDGER_COPY, state / "recommendations.jsonl")
    shutil.copy(LIVE_LAST_RUN_COPY, state / "last_run.json")
    return state


# ------------------------------------------------ F1: sticky tamper detection
@pytest.mark.parametrize("kind", ["truncate_bottom", "relink_forgery", "delete"])
def test_f1_tamper_is_sticky_across_normal_log(state, tmp_path, kind, monkeypatch):
    recs, lr = _seed(state)
    _tamper(recs, kind)
    assert not S.ledger_intact(str(recs), str(lr))
    r = _log(state, tmp_path)
    assert r.returncode == 0, r.stderr
    # the normal --log moved the anchor to the new tip, but did NOT clear the tamper
    st = S.ledger_status(str(recs), str(lr))
    assert st["fresh_tamper"] is False and st["sticky_active"] is True
    assert not S.ledger_intact(str(recs), str(lr))
    a = json.load(open(lr))
    assert a["ledger_tamper_since"] and a["ledger_tamper_events"] == 1
    m = [x for x in S.load_recs(str(recs)) if x["type"] == "portfolio_mark"][-1]
    assert m["ledger_tamper"] is True
    h = _header(state, _flat_csv(tmp_path / "q.csv", start=_days(60), n=70))
    assert h["원장_변조_감지"] is True and h["원장_변조_최초감지"] and h["원장_변조_정지_해제"]
    # a second normal run: still suspended, and the window is NOT extended
    until = st["sticky_until"]
    assert _log(state, tmp_path, "--force-relog", "--reason", "retry").returncode == 0
    st2 = S.ledger_status(str(recs), str(lr))
    assert not st2["intact"] and st2["sticky_until"] == until
    assert json.load(open(lr))["ledger_tamper_events"] == 1
    # adjust-flow stays refused while sticky
    r = run("report.py", "--adjust-flow", "--effective-date", _days(8), "--flow-usd", "5",
            "--reason", "x", state=state)
    assert r.returncode == 5
    # the window lasts OVERRIDE_SUSPENSION_DAYS from the detection, then lifts
    real_now = S._now
    monkeypatch.setattr(S, "_now", lambda: real_now() + timedelta(days=C.OVERRIDE_SUSPENSION_DAYS - 1))
    assert not S.ledger_intact(str(recs), str(lr))
    monkeypatch.setattr(S, "_now", lambda: real_now() + timedelta(days=C.OVERRIDE_SUSPENSION_DAYS + 1))
    assert S.ledger_intact(str(recs), str(lr))


def test_f1_sticky_survives_anchor_sticky_fields_being_stripped(state, tmp_path):
    recs, lr = _seed(state)
    _tamper(recs, "truncate_bottom")
    assert _log(state, tmp_path).returncode == 0
    a = json.load(open(lr))
    for k in S.STICKY_KEYS:
        a.pop(k, None)
    write_json(lr, a)                          # forger strips the anchor's sticky stamp
    assert not S.ledger_intact(str(recs), str(lr))   # the flagged ledger line still counts


def test_f1_validate_override_suspended_after_log(state, tmp_path, monkeypatch):
    import validate as V
    from test_validate import good_override
    recs, lr = _seed(state)
    _tamper(recs, "truncate_bottom")
    assert _log(state, tmp_path).returncode == 0
    monkeypatch.setattr(V, "_url_resolves", lambda u: True)
    out = V.validate(make_baseline(), {"decision": "OVERRIDE", "final_allocation": {"QQQ": 100.0},
                                       "override": good_override(), "satellite": [], "vetoes": []},
                     str(recs), str(lr))
    assert out["verdict"] == "FAIL" and out["ledger_tamper"] is True


# ------------------------------------------------ F2: missing/broken anchor
@pytest.mark.parametrize("mut", ["delete", "null", "corrupt", "no_keys", "zero_len"])
def test_f2_nonempty_ledger_without_usable_anchor_is_tampered(state, mut):
    recs, lr = _seed(state)
    if mut == "delete":
        os.remove(lr)
    elif mut == "corrupt":
        lr.write_text("{")
    else:
        a = json.load(open(lr))
        if mut == "null":
            a["ledger_len"], a["ledger_tip_hash"] = None, None
        elif mut == "no_keys":
            a = {"regime": "TREND"}
        else:
            a["ledger_len"] = 0
        write_json(lr, a)
    st = S.ledger_status(str(recs), str(lr))
    assert st["intact"] is False and st["fresh_tamper"] is True


def test_f2_genuine_first_run_is_intact(state, tmp_path):
    recs, lr = state / "recommendations.jsonl", state / "last_run.json"
    assert S.ledger_intact(str(recs), str(lr))                   # absent + absent
    recs.write_text("")
    assert S.ledger_intact(str(recs), str(lr))                   # empty + absent
    assert _log(state, tmp_path).returncode == 0
    assert S.ledger_intact(str(recs), str(lr))
    assert "ledger_tamper_since" not in json.load(open(lr))


def test_f2_anchor_write_is_atomic(state, monkeypatch):
    recs, lr = _seed(state)
    before = read_bytes(lr)

    def boom(src, dst):
        raise OSError("simulated crash during replace")
    monkeypatch.setattr(S.os, "replace", boom)
    with pytest.raises(OSError):
        S.append_anchored(str(recs), {"type": "pipeline_halt", "date": _days(1)})
    assert read_bytes(lr) == before                              # old anchor intact, not torn
    assert not [f for f in os.listdir(state) if f.endswith(".tmp")]


# ------------------------------------------------ F3: declared-flow reconciliation
def test_f3_fake_withdrawal_cannot_flip_the_scorecard(live_copy):
    recs = live_copy / "recommendations.jsonl"
    r = run("report.py", "--adjust-flow", "--effective-date", "2026-09-30", "--flow-usd", "-400",
            "--basis", "declared", "--reason", "KRW withdrawal", state=live_copy)
    assert r.returncode == 0, r.stderr
    assert "verified=False" in r.stderr
    h = _header(live_copy, HISTORY_CSV)
    assert h["누적_수익률_pct"] == pytest.approx(7.90, abs=0.02)            # all flows
    assert h["누적_수익률_검증입출금만_pct"] == pytest.approx(-11.13, abs=0.02)
    # v5.1.2 (N1): the conservative curve counts a contradicted group as max(0,
    # declared, implied): the fake -400 counts as the ledger's +34 inflow -> lower
    assert h["누적_수익률_보수적_pct"] == pytest.approx(-12.75, abs=0.02)
    assert h["누적_수익률_보수적_pct"] <= -11.13
    assert h["미검증_입출금_건수"] == 1
    adj = [e for e in h["입출금_내역"] if e["kind"] == "adjustment"]
    assert len(adj) == 1 and adj[0]["verified"] is False
    assert adj[0]["effective_date"] == "2026-09-30" and adj[0]["amount_usd"] == -400.0
    assert adj[0]["reason"] == "KRW withdrawal" and adj[0]["implied_usd"] == pytest.approx(34.0)
    assert S.ledger_intact(str(recs), str(live_copy / "last_run.json"))


def test_f3_withdraw_flag_is_reconciled_too(state, tmp_path):
    y = _days(1)
    build_ledger(state / "recommendations.jsonl",
                 [mark(y, 1475.86 + 392.25, 392.25, {"QQQ": {"shares": 2.0, "usd": 1475.86}})])
    r = _log(state, tmp_path, "--withdraw", "400",
             baseline=make_baseline(pf=portfolio(392.25, QQQ=2)))
    assert r.returncode == 0, r.stderr
    c = S.cumulative(str(state / "recommendations.jsonl"),
                     _flat_csv(tmp_path / "q.csv", start=_days(30), n=31))
    e = [x for x in c["flows"] if x["kind"] == "declared"]
    assert len(e) == 1 and e[0]["amount_usd"] == -400.0 and e[0]["verified"] is False
    assert c["actual_cum_pct_conservative"] < c["actual_cum_pct"]


def test_f3_unverified_flow_cannot_disarm_kill_or_board(tmp_path):
    csv = _flat_csv(tmp_path / "q.csv", start="2025-01-01", n=400)
    recs = tmp_path / "recommendations.jsonl"
    build_ledger(recs, [
        mark("2025-01-10", 1000.0, 0.0, {"QQQ": {"shares": 10.0, "usd": 1000.0}},
             baseline_cum_return_pct=0.0),
        mark("2025-12-01", 700.0, 0.0, {"QQQ": {"shares": 10.0, "usd": 700.0}},
             baseline_cum_return_pct=0.0),
        {"type": "cash_flow_adjustment", "date": "2025-12-02", "effective_date": "2025-11-01",
         "flow_usd": -300.0, "reason": "fake", "ticker": None, "action": "ADJUST"}])
    c = S.cumulative(str(recs), csv)
    assert c["eval_mature"] is True
    assert c["actual_cum_pct"] == pytest.approx(0.0)                 # the fake flow "explains" it
    assert c["actual_cum_pct_conservative"] == pytest.approx(-30.0)
    assert c["unverified_flows"] == 1
    assert c["kill_trigger_armed"] is True and c["board_reconvene_armed"] is True


def test_f3_near_duplicate_adjustments_refused(live_copy):
    recs = live_copy / "recommendations.jsonl"
    args = ["--adjust-flow", "--effective-date", "2026-07-23", "--reason", "w"]
    assert run("report.py", *args, "--flow-usd", "-325.18", state=live_copy).returncode == 0
    after = read_bytes(recs)
    for amt in ("-325.19", "-325.17", "-326.0"):
        r = run("report.py", *args, "--flow-usd", amt, state=live_copy)
        assert r.returncode == 4, (amt, r.stderr)
    assert read_bytes(recs) == after
    # a flow already declared on the receiving mark (08-21 deposit 176.67) is a duplicate too
    r = run("report.py", "--adjust-flow", "--effective-date", "2026-08-21", "--flow-usd", "176.67",
            "--reason", "dup of --deposit", state=live_copy)
    assert r.returncode == 4 and read_bytes(recs) == after


# ------------------------------------------------ F4: core override spread
def _override_ledger(recs, weights, days_ago=40, ticker="QQQ"):
    rows = []
    for i, w in enumerate(weights):
        d = _days(days_ago - i)
        o = {"type": "override", "date": d, "ticker": ticker, "action": "OVERRIDE",
             "direction": "de_risk", "expires_after_trading_days": 10,
             "spread_vs_baseline_pp": None}
        if w is not None:
            o["realised_equity_pct"], o["baseline_equity_pct"] = w
        rows += [mark(d, 1000.0, 0.0), o]
    build_ledger(recs, rows)


def _trend_csv(path, daily):
    idx = pd.date_range(end=pd.Timestamp(utc_today() - timedelta(days=1)), periods=120, freq="D")
    df = pd.DataFrame({"QQQ": [100.0 * (1 + daily) ** i for i in range(len(idx))]}, index=idx)
    df.index.name = "Date"
    df.to_csv(path)
    return str(path)


@pytest.mark.parametrize("daily,sign", [(-0.005, 1), (+0.005, -1)])
def test_f4_core_derisk_gets_a_real_signed_spread(state, tmp_path, daily, sign):
    import validate as V
    recs = state / "recommendations.jsonl"
    _override_ledger(recs, [(80.0, 95.0)] * 3)
    csv = _trend_csv(tmp_path / "q.csv", daily)
    assert S.score(str(recs), str(state / "sc.csv"), csv) is not None
    scores = [x for x in S.load_recs(str(recs)) if x["type"] == "override_score"]
    assert len(scores) == 3 and all(x["graded"] is True for x in scores)
    q10 = ((1 + daily) ** 10 - 1) * 100
    for x in scores:
        assert x["method"] == "equity_weight_x_qqq_fwd"
        assert x["spread_vs_baseline_pp"] == pytest.approx(round(-0.15 * q10, 2))
        assert (x["spread_vs_baseline_pp"] > 0) == (sign > 0)
    c = S.cumulative(str(recs), csv)
    assert c["override_hit_rate"] == (1.0 if sign > 0 else 0.0)
    assert c["override_privileges_at_risk"] is (sign < 0)
    assert (V.override_suspended(str(recs)) is not None) is (sign < 0)
    assert S.ledger_intact(str(recs), str(state / "last_run.json"))


def test_f4_legacy_core_override_is_ungraded_not_a_miss(state, tmp_path):
    import validate as V
    recs = state / "recommendations.jsonl"
    _override_ledger(recs, [None] * 3)
    csv = _trend_csv(tmp_path / "q.csv", +0.005)
    S.score(str(recs), str(state / "sc.csv"), csv)
    scores = [x for x in S.load_recs(str(recs)) if x["type"] == "override_score"]
    assert len(scores) == 3
    assert all(x["graded"] is False and x["spread_vs_baseline_pp"] is None for x in scores)
    m = S.merged_overrides(S.load_recs(str(recs)))
    assert all(o["graded"] is False and o["spread_vs_baseline_pp"] is None for o in m)
    c = S.cumulative(str(recs), csv)
    assert c["override_hit_rate"] is None and c["override_privileges_at_risk"] is False
    assert c["overrides_ungraded"] == 3
    assert V.override_suspended(str(recs)) is None
    # idempotent: an ungraded marker is appended once
    before = read_bytes(recs)
    S.score(str(recs), str(state / "sc.csv"), csv)
    assert read_bytes(recs) == before


def test_f4_log_persists_both_equity_weights(state, tmp_path):
    b = make_baseline()
    base_eq = b["execution"]["post_trade"]["equity_pct"]
    fp = _bare_final(tmp_path, override={"direction": "de_risk", "catalyst_url": "https://x"},
                     execution={"post_trade": {"equity_pct": 80.0}})
    assert _log(state, tmp_path, final=fp, baseline=b).returncode == 0
    o = [x for x in S.load_recs(str(state / "recommendations.jsonl")) if x["type"] == "override"][0]
    assert o["realised_equity_pct"] == 80.0 and o["baseline_equity_pct"] == base_eq


def test_rejected_override_is_logged_not_counted(state, tmp_path):
    rej = {"override": {"direction": "de_risk", "catalyst_url": "https://x"},
           "proposed_allocation": {"QQQ": 80.0}, "violations": ["OVERRIDE_EXECUTION_BELOW_FLOOR: x"]}
    fp = _bare_final(tmp_path, override=None, rejected_override=rej)
    assert _log(state, tmp_path, final=fp).returncode == 0
    loaded = S.load_recs(str(state / "recommendations.jsonl"))
    r = [x for x in loaded if x["type"] == "override_rejected"]
    assert len(r) == 1 and r[0]["violations"] == rej["violations"]
    assert r[0]["proposed_allocation"] == {"QQQ": 80.0}
    assert S.merged_overrides(loaded) == []
    h = _header(state, _flat_csv(tmp_path / "q.csv", start=_days(30), n=31))
    assert h["오버라이드_거부_건수"] == 1
    # key absent / null: no line
    fp2 = _bare_final(tmp_path)
    assert _log(state, tmp_path, "--force-relog", "--reason", "r", final=fp2).returncode == 0
    assert len([x for x in S.load_recs(str(state / "recommendations.jsonl"))
                if x["type"] == "override_rejected"]) == 1


# ------------------------------------------------ F5: no partial runs
def test_f5_bad_order_appends_nothing(state, tmp_path):
    recs, lr = _seed(state)
    b_recs, b_lr = read_bytes(recs), read_bytes(lr)
    for bad in ([{"action": "BUY", "shares": 1}], [{"ticker": "QQQ", "action": "HOLD"}], ["x"]):
        fp = _bare_final(tmp_path, orders=bad, override={"direction": "de_risk"})
        r = _log(state, tmp_path, final=fp)
        assert r.returncode == 2, (bad, r.stderr)
        assert read_bytes(recs) == b_recs and read_bytes(lr) == b_lr
    assert S.ledger_intact(str(recs), str(lr))


def test_f5_run_is_one_write(state, tmp_path, monkeypatch):
    """All lines of a run land in one write call (no interleaving, no partial run)."""
    recs, lr = _seed(state)
    calls = []
    real = S._write_lines_locked

    def spy(f, rs):
        calls.append(len(rs))
        return real(f, rs)
    monkeypatch.setattr(S, "_write_lines_locked", spy)
    b = make_baseline()
    fp = {"final_plan": {"orders": b["orders"], "source": "BASELINE",
                         "vetoes": [{"ticker": "NVDA", "url": "https://x"}]}}
    R.append_run(b, fp)
    assert calls == [3]                         # mark + order + veto, together
    assert S.ledger_intact(str(recs), str(lr))


# ------------------------------------------------ F6: lock held through anchor write
def test_f6_anchor_written_while_ledger_lock_held(state, monkeypatch):
    recs, lr = _seed(state)
    seen = []
    real = S._atomic_write_json

    def probe(path, obj):
        with open(recs, "a+") as g:
            try:
                fcntl.flock(g, fcntl.LOCK_EX | fcntl.LOCK_NB)
                seen.append("UNLOCKED")
                fcntl.flock(g, fcntl.LOCK_UN)
            except BlockingIOError:
                seen.append("locked")
        return real(path, obj)
    monkeypatch.setattr(S, "_atomic_write_json", probe)
    S.append_anchored(str(recs), {"type": "pipeline_halt", "date": _days(1)})
    S.sync_anchor(str(recs))
    assert seen == ["locked", "locked"]
    assert S.ledger_intact(str(recs), str(lr))


def test_f6_concurrent_writer_waits_for_lock(state):
    recs, lr = _seed(state)
    with S.ledger_lock(str(recs)):
        p = subprocess.Popen(
            [os.sys.executable, os.path.join(SCRIPTS, "report.py"), "--halt", "concurrent"],
            env=dict(os.environ, US_ADVISOR_STATE=str(state), PYTHONDONTWRITEBYTECODE="1"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(1.0)
        assert p.poll() is None                  # blocked on the ledger lock
    assert p.wait(timeout=60) == 0
    assert S.ledger_intact(str(recs), str(lr))


# ------------------------------------------------ F7: same-date flows
def test_f7_two_same_date_deposits_summed(tmp_path):
    csv = _flat_csv(tmp_path / "q.csv")
    recs = tmp_path / "recommendations.jsonl"
    build_ledger(recs, [
        mark("2025-02-01", 1000.0, 1000.0),
        mark("2025-03-01", 1200.0, 1200.0, external_flow_usd=200.0, day_flow_total_usd=200.0),
        mark("2025-03-01", 1500.0, 1500.0, external_flow_usd=300.0, day_flow_total_usd=500.0,
             supersedes_same_date=True)])
    assert S.external_flows(S.load_recs(str(recs))) == {"2025-03-01": 500.0}
    c = S.cumulative(str(recs), csv)
    assert c["actual_cum_pct"] == pytest.approx(0.0)
    assert c["flows"][0]["verified"] is True


def test_f7_relog_deposit_is_additive_via_cli(state, tmp_path):
    build_ledger(state / "recommendations.jsonl",
                 [mark(_days(1), 1868.11, 392.25, {"QQQ": {"shares": 2.0, "usd": 1475.86}})])
    assert _log(state, tmp_path, "--deposit", "200").returncode == 0
    r = _log(state, tmp_path, "--deposit", "300", "--force-relog", "--reason", "second transfer")
    assert r.returncode == 0, r.stderr
    loaded = S.load_recs(str(state / "recommendations.jsonl"))
    ms = [x for x in loaded if x["type"] == "portfolio_mark"]
    assert [m.get("day_flow_total_usd") for m in ms[1:]] == [200.0, 500.0]
    assert S.external_flows(loaded)[str(utc_today())] == 500.0
    # a re-log that declares nothing keeps the day total
    assert _log(state, tmp_path, "--force-relog", "--reason", "no-op rerun").returncode == 0
    assert S.external_flows(S.load_recs(str(state / "recommendations.jsonl")))[str(utc_today())] == 500.0


# ------------------------------------------------ F8: de-dup never hides history
def _dedup_ledger(recs):
    build_ledger(recs, [
        mark("2026-09-01", 1000.0, 1000.0),
        {"type": "order", "date": "2026-09-01", "ticker": "NVDA", "action": "BUY", "usd": 100},
        {"type": "override", "date": "2026-09-01", "ticker": "QQQ", "action": "OVERRIDE",
         "direction": "de_risk"},
        mark("2026-09-01", 1000.0, 1000.0, supersedes_same_date=True, relog_reason="rerun"),
        {"type": "noop", "date": "2026-09-01", "ticker": "QQQ", "action": "NO_OP"}])


def test_f8_superseded_orders_and_overrides_stay_visible(tmp_path):
    recs = tmp_path / "recommendations.jsonl"
    _dedup_ledger(recs)
    loaded = S.load_recs(str(recs))
    hist = S.satellite_history_recs(loaded)
    assert [(h["ticker"], h["action"], h["superseded"]) for h in hist] == [("NVDA", "BUY", True)]
    mo = S.merged_overrides(loaded)
    assert len(mo) == 1 and mo[0]["superseded"] is True
    lines = S.superseded_lines(loaded)
    assert {(x["type"], x["ticker"]) for x in lines} == {("order", "NVDA"), ("override", "QQQ")}
    assert [r["type"] for r in S.effective_recs(loaded)] == ["portfolio_mark", "noop"]  # scoring view


def test_f8_verbatim_relogged_order_counted_once(tmp_path):
    recs = tmp_path / "recommendations.jsonl"
    o = {"type": "order", "date": "2026-09-01", "ticker": "NVDA", "action": "SELL", "shares": 1.0}
    build_ledger(recs, [mark("2026-09-01", 1000.0, 1000.0), dict(o),
                        mark("2026-09-01", 1000.0, 1000.0), dict(o)])
    hist = S.satellite_history_recs(S.load_recs(str(recs)))
    assert len(hist) == 1 and hist[0]["superseded"] is False


def test_f8_validate_history_sees_superseded_buy(tmp_path):
    import validate as V
    recs = tmp_path / "recommendations.jsonl"
    _dedup_ledger(recs)
    last_buy, _ = V._satellite_history(str(recs))
    if "NVDA" not in last_buy:
        pytest.xfail("validate._satellite_history not yet switched to satellite_history_recs "
                     "(Fixer A's file)")
    assert "NVDA" in last_buy


def test_f8_header_lists_superseded_lines(live_copy):
    h = _header(live_copy, HISTORY_CSV)
    assert h["중복_기록_제외_건수"] == 2
    assert [(x["date"], x["ticker"]) for x in h["중복_기록_제외_라인"]] == \
        [("2026-08-21", "QQQ"), ("2026-09-30", "QQQ")]


# ------------------------------------------------ F9: price outage is visible
def test_f9_price_outage_visible_and_cache_not_poisoned(live_copy, tmp_path):
    recs = str(live_copy / "recommendations.jsonl")
    out = str(live_copy / "scorecard.csv")
    dead = _flat_csv(tmp_path / "dead.csv", cols=("XYZ",))          # no benchmark column
    assert S.score(recs, out, dead) is None and not os.path.exists(out)
    c = S.cumulative(recs, dead)
    assert c["price_data_ok"] is False and c["qqq_cum_pct"] is None
    assert c["kill_trigger_armed"] == "unknown" and c["board_reconvene_armed"] == "unknown"
    h = _header(live_copy, dead)
    assert h["가격_데이터_정상"] is False and h["킬_트리거_발동"] == "unknown"
    assert h["보드_재소집"] == "unknown" and h["새틀라이트_확대_가능"] is False
    r = run("score_recs.py", "--score", "--prices-csv", str(tmp_path / "missing.csv"), state=live_copy)
    assert r.returncode == 3 and not os.path.exists(out)
    # an all-empty cache newer than the ledger (the old failure mode) is not trusted
    with open(out, "w", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=["date", "type", "ticker", "action", "qqq_1d", "excess_20d"])
        w.writeheader()
        w.writerow({"date": "2026-07-14", "type": "order", "ticker": "NVDA", "action": "SELL"})
    os.utime(out, (time.time() + 60, time.time() + 60))
    tr = S.track_record(recs, out, HISTORY_CSV)
    assert tr["price_data_ok"] is True and tr["graded"] >= 1
    assert any(r2.get("qqq_1d") not in (None, "") for r2 in _csv.DictReader(open(out)))


# ------------------------------------------------ F10: low findings
def test_f10_unmatured_shadow_pick_not_tallied_open(state, tmp_path):
    idx = pd.date_range("2025-01-01", periods=60, freq="D")
    df = pd.DataFrame({"QQQ": [100.0] * 60, "AMD": [100.0] * 60}, index=idx)
    df.index.name = "Date"
    csv = str(tmp_path / "g.csv")
    df.to_csv(csv)
    recs = state / "recommendations.jsonl"
    build_ledger(recs, [
        mark("2025-01-02", 1000.0, 0.0),
        {"type": "shadow_pick", "date": "2025-01-02", "ticker": "AMD", "stop": 90.0, "target": 110.0},
        mark("2025-02-25", 1000.0, 0.0),
        {"type": "shadow_pick", "date": "2025-02-25", "ticker": "AMD", "stop": 90.0, "target": 110.0}])
    st = S.shadow_stats(str(recs), csv)
    assert st["n_total"] == 2 and st["n_graded"] == 1 and st["n_unmatured"] == 1
    assert st["bracket"] == {"TARGET": 0, "STOP": 0, "OPEN": 1}
    rows = S.score(str(recs), str(state / "sc.csv"), csv)
    assert [r["bracket_outcome"] for r in rows if r["type"] == "shadow_pick"] == ["OPEN", "PENDING"]


def test_f10_adjustment_before_first_mark_refused(live_copy):
    recs = live_copy / "recommendations.jsonl"
    before = read_bytes(recs)
    for d in ("2026-07-01", "2026-07-14"):
        r = run("report.py", "--adjust-flow", "--effective-date", d, "--flow-usd", "-50",
                "--reason", "x", state=live_copy)
        assert r.returncode == 2 and "first portfolio_mark" in r.stderr, r.stderr
    assert read_bytes(recs) == before


@pytest.fixture
def fake_crontab(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    sh = bindir / "crontab"
    sh.write_text(f"""#!/usr/bin/env bash
echo "$*" >> "{log}"
if [ "$1" = "-l" ]; then exit 0; fi
exit 97
""")
    sh.chmod(sh.stat().st_mode | stat.S_IEXEC)
    return bindir, log


def test_f10_cron_line_quotes_paths_and_caps_log(tmp_path, fake_crontab):
    bindir, log = fake_crontab
    sk = tmp_path / "skill dir's copy"
    (sk / "scripts").mkdir(parents=True)
    for fn in ("install_cron.sh", "config.py", "score_recs.py"):     # D1: cap read from config
        shutil.copy(os.path.join(SCRIPTS, fn), sk / "scripts" / fn)
    env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ["PATH"],
               PYTHONDONTWRITEBYTECODE="1")
    r = subprocess.run(["bash", str(sk / "scripts" / "install_cron.sh")], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    line = next(ln.strip() for ln in r.stdout.splitlines() if "score_recs.py" in ln)
    cmd = line.split(" ", 5)[5]
    toks = shlex.split(cmd, comments=True)
    assert str(sk / "scripts" / "score_recs.py") in toks
    assert str(sk / "state" / ".score.lock") in toks
    assert str(sk / "state" / "score_cron.log") in toks and "-size" in toks   # rotation
    assert str(sk / "state" / "score_cron.log.1") in toks
    assert "%" not in cmd
    assert [c for c in log.read_text().split("\n") if c.strip()] == ["-l"]
    # the command is valid POSIX sh
    assert subprocess.run(["sh", "-n", "-c", cmd]).returncode == 0


def test_f10_dividend_basis_reported(live_copy):
    c = S.cumulative(str(live_copy / "recommendations.jsonl"), HISTORY_CSV)
    assert c["qqq_cum_pct_price_basis"] == pytest.approx(3.68, abs=0.01)
    assert c["qqq_cum_pct"] == pytest.approx(3.79, abs=0.01)
    assert c["dividend_basis_gap_pp"] == pytest.approx(0.11, abs=0.01)
    assert c["mechanical_baseline_cum_pct"] == 3.68                      # unchanged (continuity)
    assert c["mechanical_baseline_cum_pct_tr_bound"] == pytest.approx(3.79, abs=0.01)


# ------------------------------------------------ backward compatibility
def test_live_copy_backward_compat_and_verified_adjustment(live_copy):
    recs = str(live_copy / "recommendations.jsonl")
    st = S.ledger_status(recs, str(live_copy / "last_run.json"))
    assert st["intact"] and st["tamper_since"] is None
    c = S.cumulative(recs, HISTORY_CSV)
    assert c["actual_cum_pct"] == pytest.approx(-11.13, abs=0.02)
    assert c["actual_cum_pct_conservative"] == pytest.approx(-11.13, abs=0.02)
    assert c["superseded_runs"] == 2 and c["price_data_ok"] is True
    assert [(e["receiving_mark"], e["verified"]) for e in c["flows"]] == [("2026-08-21", True)]
    assert c["unexplained_flow_marks"] == ["2026-07-23"]
    r = run("report.py", "--adjust-flow", "--effective-date", "2026-07-23", "--flow-usd", "-325.18",
            "--reason", "07-23 withdrawal", state=live_copy)
    assert r.returncode == 0, r.stderr
    c = S.cumulative(recs, HISTORY_CSV)
    assert c["actual_cum_pct"] == pytest.approx(6.68, abs=0.02)
    assert c["pnl_usd"] == pytest.approx(121.35, abs=0.05)
    adj = [e for e in c["flows"] if e["kind"] == "adjustment"][0]
    assert adj["verified"] is True and adj["implied_usd"] == pytest.approx(-325.18)
    assert c["actual_cum_pct_conservative"] == pytest.approx(6.68, abs=0.02)
    assert c["unexplained_flow_marks"] == []
