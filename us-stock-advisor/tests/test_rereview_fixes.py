"""Regression tests for the re-review (cycle 2) findings N1-N7, D1-D3 and the RT1-9
leftover (v5.1.2). Every test runs in a scratch state dir (conftest `state`)."""
from __future__ import annotations

import copy
import json
import os
import random
import shutil
import stat
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

import config as C
import core as CORE
import report as R
import score_recs as S
import validate as V
from helpers import (HISTORY_CSV, LIVE_LAST_RUN_COPY, LIVE_LEDGER_COPY, LIVE_PX, SCRIPTS,
                     build_ledger, iso_hours_ago, make_baseline, make_prices_csv, mark,
                     portfolio, read_bytes, run, utc_today, write_json)


# ------------------------------------------------------------------ helpers
def _days(n):
    return str(utc_today() - timedelta(days=n))


@pytest.fixture
def live_copy(state):
    shutil.copy(LIVE_LEDGER_COPY, state / "recommendations.jsonl")
    shutil.copy(LIVE_LAST_RUN_COPY, state / "last_run.json")
    return state


@pytest.fixture
def urls_ok(monkeypatch):
    monkeypatch.setattr(V, "_url_resolves", lambda url: True)


def _flat_csv(path, start="2025-01-01", n=400, px=100.0, cols=("QQQ",)):
    idx = pd.date_range(start, periods=n, freq="D")
    df = pd.DataFrame({c: [px] * n for c in cols}, index=idx)
    df.index.name = "Date"
    df.to_csv(path)
    return str(path)


def _ovr_obj(direction="de_risk"):
    return {"catalyst_description": "dated event", "catalyst_url": "https://example.com/x",
            "catalyst_timestamp": iso_hours_ago(2), "expected_cost_if_wrong_pct": 1.0,
            "qqq_forward_20d_if_i_am_wrong": 3.0, "direction": direction}


def _override(alloc, direction="de_risk"):
    return {"decision": "OVERRIDE", "final_allocation": alloc, "satellite": [],
            "override": _ovr_obj(direction), "vetoes": []}


def _seed(state, n=5):
    recs = [mark(_days(10 - i), 1000.0 + i, 100.0) for i in range(n)]
    build_ledger(state / "recommendations.jsonl", recs)
    return state / "recommendations.jsonl", state / "last_run.json"


def _log(state, tmp_path, *args, final=None, baseline=None):
    bp = write_json(tmp_path / "baseline.json", baseline or make_baseline())
    fp = final or write_json(tmp_path / "final.json",
                             {"final_plan": {"orders": [], "source": "BASELINE"}})
    return run("report.py", "--log", "--baseline", bp, "--final", fp, *args, state=state)


def _cum(state, csv=HISTORY_CSV):
    return S.cumulative(str(state / "recommendations.jsonl"), csv)


# ======================================================================= N1
def _launder_la2_case_c(state):
    """rr/la2.py case C, in process: one --basis declared adjustment per effective
    mark, each sized implied − 0.98 × tolerance (the most negative that the old
    proximity-only rule verified)."""
    recs = S.load_recs(str(state / "recommendations.jsonl"))
    eff = [r for r in S.effective_recs(recs) if r["type"] == "portfolio_mark"]
    n = 0
    for i in range(1, len(eff)):
        b = eff[i]
        imp = S.implied_flow(eff[i - 1], b)
        if imp is None:
            continue
        tol = C.UNEXPLAINED_FLOW_WARN_PCT * b["total_usd"]
        already = 176.67 if b["date"] == "2026-08-21" else 0.0
        amt = round(imp - 0.98 * tol - already, 2)
        if b["date"] == "2026-07-23":
            amt = round(-0.98 * tol, 2)
        R.adjust_flow(b["date"], amt, basis="declared", reason="fx/fees", force=True)
        n += 1
    return n


def test_n1_rr_la2_case_c_cannot_launder_conservative(live_copy):
    before = _cum(live_copy)
    assert before["actual_cum_pct_conservative"] == pytest.approx(-11.13, abs=0.02)
    assert _launder_la2_case_c(live_copy) == 11
    c = _cum(live_copy)
    assert c["actual_cum_pct"] > 15                         # the fakes "explain" everything
    assert c["actual_cum_pct_conservative"] <= -11.13 + 1e-9
    assert c["llm_layer_spread_pp_conservative"] <= before["llm_layer_spread_pp_conservative"] + 1e-9
    assert all(e["verified"] is False for e in c["flows"] if e["kind"] == "adjustment")


def test_n1_live_historical_deposit_and_0723_correction_still_verified(live_copy):
    c = _cum(live_copy)
    assert [(e["receiving_mark"], e["amount_usd"], e["verified"]) for e in c["flows"]] == \
        [("2026-08-21", 176.67, True)]
    r = run("report.py", "--adjust-flow", "--effective-date", "2026-07-23", "--flow-usd",
            "-325.18", "--reason", "07-23 withdrawal", state=live_copy)
    assert r.returncode == 0 and "verified=True" in r.stderr, r.stderr
    c = _cum(live_copy)
    assert c["actual_cum_pct"] == pytest.approx(6.68, abs=0.02)
    assert c["actual_cum_pct_conservative"] == pytest.approx(6.68, abs=0.02)
    assert c["pnl_usd"] == pytest.approx(121.35, abs=0.05)
    assert all(e["verified"] is True for e in c["flows"])


def test_n1_near_zero_implied_cannot_verify_a_declaration(tmp_path):
    """Inside the absolute tolerance but NOT corroborated: the ledger implies ~0."""
    recs = tmp_path / "recommendations.jsonl"
    build_ledger(recs, [
        mark("2025-02-01", 2000.0, 100.0, {"QQQ": {"shares": 19.0, "usd": 1900.0}}),
        mark("2025-03-01", 2000.0, 100.0, {"QQQ": {"shares": 19.0, "usd": 1900.0}}),
        {"type": "cash_flow_adjustment", "date": "2025-03-02", "effective_date": "2025-03-01",
         "flow_usd": -55.0, "reason": "fees?", "ticker": None, "action": "ADJUST"}])
    c = S.cumulative(str(recs), _flat_csv(tmp_path / "q.csv"))
    e = c["flows"][0]
    assert abs(e["deviation_usd"]) <= e["tolerance_usd"]          # the old rule verified it
    assert e["verified"] is False and "FLOW_VERIFY_MAX_REL_DEV" in e["note"]
    assert c["actual_cum_pct_conservative"] == pytest.approx(0.0, abs=1e-6)


def test_n1_many_small_flows_attack_is_capped(tmp_path):
    """12 intervals, each with a small genuine outflow (−20, inside the tolerance) the
    attacker over-declares by 25% (−25): each passes the absolute AND relative legs,
    but the cumulative leg stops them once Σ|dev| reaches FLOW_VERIFY_CUM_DEV_MAX_PCT
    of the book, so the laundered gain is bounded by that cap."""
    csv = _flat_csv(tmp_path / "q.csv")
    marks, cash = [], 1000.0
    for i in range(13):
        marks.append(mark(f"2025-{1 + i // 4:02d}-{1 + 7 * (i % 4):02d}", cash + 1000.0, cash,
                          {"QQQ": {"shares": 10.0, "usd": 1000.0}}))
        cash -= 20.0
    honest, attack = tmp_path / "h", tmp_path / "a"
    honest.mkdir(), attack.mkdir()
    adj = lambda m, amt: {"type": "cash_flow_adjustment", "date": m["date"],  # noqa: E731
                          "effective_date": m["date"], "flow_usd": amt, "reason": "w",
                          "ticker": None, "action": "ADJUST"}
    build_ledger(honest / "recommendations.jsonl", marks + [adj(m, -20.0) for m in marks[1:]])
    build_ledger(attack / "recommendations.jsonl", marks + [adj(m, -25.0) for m in marks[1:]])
    h = S.cumulative(str(honest / "recommendations.jsonl"), csv)
    a = S.cumulative(str(attack / "recommendations.jsonl"), csv)
    assert h["actual_cum_pct_conservative"] == pytest.approx(0.0, abs=1e-6)
    ver = [e for e in a["flows"] if e["verified"]]
    assert 0 < len(ver) < len(a["flows"])                             # the cap bit
    assert sum(abs(e["deviation_usd"]) for e in ver) <= \
        C.FLOW_VERIFY_CUM_DEV_MAX_PCT * min(m["total_usd"] for m in marks) + 1e-6
    capped = [e for e in a["flows"] if not e["verified"]]
    assert all("FLOW_VERIFY_CUM_DEV_MAX_PCT" in e["note"] for e in capped)
    bound = C.FLOW_VERIFY_CUM_DEV_MAX_PCT * 100 * 1.01                # ≈ cap / book, in %
    assert a["actual_cum_pct_conservative"] - h["actual_cum_pct_conservative"] <= bound


def test_n1_poisoning_a_genuine_deposit_group_does_not_help(live_copy):
    """A fake withdrawal on the 08-21 receiving mark makes the genuine +176.67 group
    unverified; the conservative curve still removes the ledger-implied +176.67."""
    before = _cum(live_copy)["actual_cum_pct_conservative"]
    R.adjust_flow("2026-08-21", -52.78, basis="declared", reason="fx", force=True)
    c = _cum(live_copy)
    assert all(e["verified"] is False for e in c["flows"] if e["receiving_mark"] == "2026-08-21")
    assert c["actual_cum_pct_verified_flows"] > before                # the naive view inflates
    assert c["actual_cum_pct_conservative"] <= before + 1e-9          # the gate does not


def test_n1_undeclared_implied_inflow_counts_as_deposit_in_conservative(tmp_path):
    recs = tmp_path / "recommendations.jsonl"
    build_ledger(recs, [mark("2025-02-01", 1000.0, 1000.0), mark("2025-03-01", 2000.0, 2000.0)])
    c = S.cumulative(str(recs), _flat_csv(tmp_path / "q.csv"))
    assert c["actual_cum_pct"] == pytest.approx(100.0)                # undeclared deposit = "gain"
    assert c["unexplained_flow_marks"] == ["2025-03-01"]
    assert c["actual_cum_pct_conservative"] == pytest.approx(0.0)


def test_n1_withdraw_flag_inside_tolerance_is_not_verified(state, tmp_path):
    """--log --withdraw just under the absolute tolerance with no cash change."""
    build_ledger(state / "recommendations.jsonl",
                 [mark(_days(1), 1475.86 + 392.25, 392.25, {"QQQ": {"shares": 2.0, "usd": 1475.86}})])
    assert _log(state, tmp_path, "--withdraw", "50").returncode == 0
    c = S.cumulative(str(state / "recommendations.jsonl"),
                     _flat_csv(tmp_path / "q.csv", start=_days(30), n=31))
    e = [x for x in c["flows"] if x["kind"] == "declared"][0]
    assert abs(e["deviation_usd"]) <= e["tolerance_usd"] and e["verified"] is False
    assert c["actual_cum_pct_conservative"] <= 0.0 + 1e-9


# ======================================================================= N2
def _stale_baseline(tmp_path):
    b = make_baseline()
    b["generated_utc"] = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    return write_json(tmp_path / "stale.json", b), b


@pytest.mark.parametrize("ledger", ["dir", "bad_utf8"])
def test_n2_stale_baseline_halts_even_with_unreadable_ledger(state, tmp_path, ledger):
    bp, _ = _stale_baseline(tmp_path)
    recs = tmp_path / "ledger"
    if ledger == "dir":
        recs.mkdir()
    else:
        recs.write_bytes(b'{"type": "portfolio_mark", "date": "2026-09-01", "x": "\xff\xfe"}\n')
    pp = write_json(tmp_path / "p.json", {"decision": "CONFIRM_BASELINE",
                                          "final_allocation": {"QQQ": 100.0}})
    r = run("validate.py", "--baseline", bp, "--proposal", pp, "--recs", recs,
            "--last-run", tmp_path / "lr.json", state=state)
    out = json.loads(r.stdout)
    assert r.returncode == 2, r.stdout
    assert out["final_plan"]["source"] == "HALT" and out["final_plan"]["orders"] == []
    assert any("BASELINE_STALE" in v for v in out["violations"])


def test_n2_exception_path_rechecks_staleness(state, tmp_path, monkeypatch):
    _, b = _stale_baseline(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("simulated ledger failure")
    monkeypatch.setattr(V, "_validate", boom)
    out = V.validate(b, {"decision": "CONFIRM_BASELINE", "final_allocation": {"QQQ": 100.0}},
                     str(state / "recommendations.jsonl"), str(state / "last_run.json"))
    assert out["final_plan"]["source"] == "HALT" and out["final_plan"]["orders"] == []
    fresh = make_baseline()
    out = V.validate(fresh, {"decision": "CONFIRM_BASELINE"},
                     str(state / "recommendations.jsonl"), str(state / "last_run.json"))
    assert out["final_plan"]["source"] == "BASELINE_ENFORCED"
    assert out["final_plan"]["orders"] == fresh["orders"]


# ======================================================================= N3
def test_n3_no_effect_override_fails_named(state, urls_ok):
    b = make_baseline(pf=portfolio(392.25, QQQ=2))
    out = V.validate(copy.deepcopy(b), _override({"QQQ": 100.0}),
                     str(state / "recommendations.jsonl"), str(state / "last_run.json"))
    assert out["verdict"] == "FAIL"
    assert any(v.startswith("OVERRIDE_NO_EFFECT") for v in out["violations"])
    assert out["final_plan"]["source"] == "BASELINE_ENFORCED"
    assert out["final_plan"]["orders"] == b["orders"]
    assert out["final_plan"]["rejected_override"]["violations"]


def _ovr_line(d, spread, **extra):
    r = {"type": "override", "date": d, "ticker": "QQQ", "action": "OVERRIDE",
         "direction": "de_risk", "spread_vs_baseline_pp": spread,
         "baseline_equity_pct": 95.3, "realised_equity_pct": 85.0}
    r.update(extra)
    return r


def test_n3_zero_spread_does_not_reset_suspension_streak(state):
    recs = []
    for i, sp in enumerate([-1.0, -0.5, 0.0, -0.7]):
        d = _days(30 - i)
        recs += [mark(d, 1000.0, 0.0), _ovr_line(d, sp)]
    build_ledger(state / "recommendations.jsonl", recs)
    assert V.override_suspended(str(state / "recommendations.jsonl")) is not None
    # control: a real positive spread in the middle does break the streak
    recs2 = []
    for i, sp in enumerate([-1.0, -0.5, 0.3, -0.7]):
        d = _days(30 - i)
        recs2 += [mark(d, 1000.0, 0.0), _ovr_line(d, sp)]
    other = state / "other"
    other.mkdir()
    build_ledger(other / "recommendations.jsonl", recs2)
    assert V.override_suspended(str(other / "recommendations.jsonl")) is None


# ======================================================================= N4
def test_n4a_anchor_write_failure_after_append_is_recovered_not_tamper(state, monkeypatch):
    recs, lr = _seed(state)
    real = S._atomic_write_json
    calls = {"n": 0}

    def fail_once(path, obj):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated: anchor write failed after the append")
        return real(path, obj)
    monkeypatch.setattr(S, "_atomic_write_json", fail_once)
    with pytest.raises(OSError):
        S.append_anchored(str(recs), {"type": "pipeline_halt", "date": _days(1)})
    assert len(S._read_lines(str(recs))) == 6 and json.load(open(lr))["ledger_len"] == 5
    assert os.path.exists(state / S.PENDING_NAME)
    st = S.ledger_status(str(recs), str(lr))
    assert st["intact"] and st["pending_commit"] and not st["fresh_tamper"]
    assert S.append_anchored(str(recs), {"type": "pipeline_halt", "date": _days(0)}) is True
    a = json.load(open(lr))
    assert a["ledger_len"] == 7 and "ledger_tamper_since" not in a
    assert not os.path.exists(state / S.PENDING_NAME)
    assert S.ledger_intact(str(recs), str(lr))
    assert not any(r.get("ledger_tamper") for r in S.load_recs(str(recs)))


def test_n4a_unwritable_state_dir_appends_nothing(state):
    recs, lr = _seed(state)
    before, before_a = read_bytes(recs), read_bytes(lr)
    os.chmod(state, 0o555)
    try:
        with pytest.raises(OSError):
            S.append_anchored(str(recs), {"type": "pipeline_halt", "date": _days(1)})
    finally:
        os.chmod(state, 0o755)
    assert read_bytes(recs) == before and read_bytes(lr) == before_a
    assert S.ledger_intact(str(recs), str(lr))


def _torn(recs, kind):
    raw = read_bytes(recs)
    if kind == "ascii":
        tail = b'{"type": "pipeline_halt", "date": "2026-09-3'
    else:                                   # a torn multi-byte (Korean) character
        tail = '{"type": "pipeline_halt", "reason": "가격'.encode("utf-8") + "데".encode("utf-8")[:2]
    with open(recs, "wb") as f:
        f.write(raw + tail)
    return raw, tail


@pytest.mark.parametrize("kind", ["ascii", "multibyte"])
def test_n4b_torn_tail_reads_not_intact_and_blocks_writers_without_crash(state, tmp_path, kind):
    recs, lr = _seed(state)
    raw, tail = _torn(recs, kind)
    torn_bytes, anchor_bytes = read_bytes(recs), read_bytes(lr)
    st = S.ledger_status(str(recs), str(lr))
    assert not st["intact"] and st["torn_tail"] and not st["fresh_tamper"]
    assert "TORN_TAIL" in st["reason"] and "--repair-torn-tail" in st["reason"]
    csv = _flat_csv(tmp_path / "q.csv", start=_days(60), n=70)
    h = run("report.py", "--header", "--prices-csv", csv, state=state)
    assert h.returncode == 0, h.stderr
    hd = json.loads(h.stdout)
    assert hd["원장_변조_감지"] is True and hd["원장_꼬리_손상"]["bytes"] == len(tail)
    assert _log(state, tmp_path).returncode == 5
    assert run("report.py", "--halt", "x", state=state).returncode == 5
    assert run("score_recs.py", "--score", "--prices-csv", csv, state=state).returncode == 5
    assert read_bytes(recs) == torn_bytes and read_bytes(lr) == anchor_bytes   # nothing written
    assert "ledger_tamper_since" not in json.load(open(lr))


@pytest.mark.parametrize("kind", ["ascii", "multibyte"])
def test_n4c_repair_drops_only_the_torn_line_and_is_logged(state, tmp_path, kind):
    recs, lr = _seed(state)
    raw, tail = _torn(recs, kind)
    r = run("report.py", "--repair-torn-tail", state=state)
    assert r.returncode == 2                                   # --reason required
    r = run("report.py", "--repair-torn-tail", "--reason", "power loss", state=state)
    assert r.returncode == 0, r.stderr
    after = read_bytes(recs)
    assert after.startswith(raw) and tail not in after
    lines = S.load_recs(str(recs))
    assert len(lines) == 6 and lines[-1]["type"] == "ledger_repair"
    rep = lines[-1]
    assert rep["removed_bytes"] == len(tail) and rep["reason"] == "power loss"
    assert read_bytes(state / rep["removed_copy"]) == tail     # exact bytes kept
    assert S.ledger_intact(str(recs), str(lr))
    hd = json.loads(run("report.py", "--header", "--prices-csv",
                        _flat_csv(tmp_path / "q.csv", start=_days(60), n=70), state=state).stdout)
    assert hd["원장_복구_이력"][0]["removed_bytes"] == len(tail)
    assert hd["원장_변조_감지"] is False
    # nothing torn any more: a second repair refuses and touches nothing
    before = read_bytes(recs)
    assert run("report.py", "--repair-torn-tail", "--reason", "again", state=state).returncode == 2
    assert read_bytes(recs) == before


def test_n4c_crash_mid_append_is_fully_recovered_by_repair(state, monkeypatch):
    """Intent written, the append torn half-way: repair drops the torn line, the stale
    intent is cleaned up, the ledger reads intact."""
    recs, lr = _seed(state)

    def torn_write(f, rs):
        out = S._chain_lines(S._read_lines(str(recs))[-1], rs)
        f.seek(0, os.SEEK_END)
        f.write(out[0][: len(out[0]) // 2])
        f.flush()
        raise OSError("simulated power loss mid-append")
    monkeypatch.setattr(S, "_write_lines_locked", torn_write)
    with pytest.raises(OSError):
        S.append_anchored(str(recs), {"type": "pipeline_halt", "date": _days(1), "reason": "중단"})
    monkeypatch.undo()
    assert os.path.exists(state / S.PENDING_NAME)
    assert S.ledger_status(str(recs), str(lr))["torn_tail"]
    S.repair_torn_tail(str(recs), str(lr), "crash")
    assert S.ledger_intact(str(recs), str(lr))
    assert not os.path.exists(state / S.PENDING_NAME)


@pytest.mark.parametrize("kind", ["truncate", "relink", "midfile"])
def test_n4c_repair_does_not_clear_a_real_tamper(state, tmp_path, kind):
    recs, lr = _seed(state)
    lines = recs.read_text().splitlines()
    if kind == "truncate":
        lines = lines[:-1]
    elif kind == "relink":
        lines[2] = lines[2].replace('"total_usd": 1002.0', '"total_usd": 1999.0')
        out, prev = [], None
        for ln in lines:
            r = json.loads(ln)
            r["prev_hash"] = S._line_hash(prev) if prev is not None else None
            prev = json.dumps(r, ensure_ascii=False)
            out.append(prev)
        lines = out
    else:
        lines[2] = lines[2].replace('"total_usd": 1002.0', '"total_usd": 1999.0')
    recs.write_text("".join(ln + "\n" for ln in lines) + '{"type": "pipeline_ha')
    assert run("report.py", "--repair-torn-tail", "--reason", "x", state=state).returncode == 0
    st = S.ledger_status(str(recs), str(lr))
    assert not st["intact"] and st["sticky_active"]
    a = json.load(open(lr))
    assert a["ledger_tamper_events"] == 1 and a["ledger_tamper_since"]
    assert S.load_recs(str(recs))[-1]["ledger_tamper"] is True


def test_n4d_sticky_window_is_not_extended_by_repeated_writes(state, tmp_path, monkeypatch):
    """A naive mid-file edit leaves a permanent chain break. One detection stamps it;
    later writes with the SAME evidence neither re-stamp nor extend the window, and it
    ends after OVERRIDE_SUSPENSION_DAYS. A NEW edit is fresh evidence again."""
    recs, lr = _seed(state)
    lines = recs.read_text().splitlines()
    lines[2] = lines[2].replace('"total_usd": 1002.0', '"total_usd": 1999.0')
    recs.write_text("".join(ln + "\n" for ln in lines))
    assert S.ledger_status(str(recs), str(lr))["fresh_tamper"]
    S.append_anchored(str(recs), {"type": "pipeline_halt", "date": _days(3)})
    st1 = S.ledger_status(str(recs), str(lr))
    assert not st1["fresh_tamper"] and st1["sticky_active"]
    real_now = S._now
    monkeypatch.setattr(S, "_now", lambda: real_now() + timedelta(days=5))
    for i in range(3):
        S.append_anchored(str(recs), {"type": "pipeline_halt", "date": _days(2 - i)})
    st2 = S.ledger_status(str(recs), str(lr))
    assert st2["sticky_until"] == st1["sticky_until"] and not st2["fresh_tamper"]
    assert json.load(open(lr))["ledger_tamper_events"] == 1
    assert sum(1 for r in S.load_recs(str(recs)) if r.get("ledger_tamper")) == 1
    monkeypatch.setattr(S, "_now", lambda: real_now() + timedelta(days=C.OVERRIDE_SUSPENSION_DAYS + 1))
    assert S.ledger_intact(str(recs), str(lr))
    # a new, different edit is new evidence
    monkeypatch.setattr(S, "_now", real_now)
    lines = recs.read_text().splitlines()
    lines[1] = lines[1].replace('"total_usd": 1001.0', '"total_usd": 1500.0')
    recs.write_text("".join(ln + "\n" for ln in lines))
    assert S.ledger_status(str(recs), str(lr))["fresh_tamper"]


# ======================================================================= N5
def test_n5_log_refuses_a_halt_plan(state, tmp_path):
    recs, lr = _seed(state)
    before, before_a = read_bytes(recs), read_bytes(lr)
    for fp in ({"final_plan": {"source": "HALT", "orders": []}, "halt": True},
               {"source": "HALT", "orders": []}):
        r = _log(state, tmp_path, final=write_json(tmp_path / "halt.json", fp))
        assert r.returncode == 2 and "HALT" in r.stderr, r.stderr
    assert read_bytes(recs) == before and read_bytes(lr) == before_a


# ======================================================================= N6
def test_n6_defensive_small_book_warns_when_it_cannot_derisk(state, tmp_path):
    b = make_baseline(regime="DEFENSIVE", pf=portfolio(0.0, QQQ=1))
    assert b["orders"] == []
    w = b["execution"]["warnings"]
    assert len(w) == 1 and "no whole-share core order" in w[0]
    ok = make_baseline(pf=portfolio(392.25, QQQ=2))
    assert ok["execution"]["warnings"] == []
    # the CLI keeps it next to its own freshness warnings
    csv = make_prices_csv(tmp_path / "px.csv", trend=-1.0)
    pf = write_json(tmp_path / "pf.json", portfolio(0.0, QQQ=1))
    r = run("core.py", "--prices-csv", csv, "--portfolio", pf, "--out", tmp_path / "b.json",
            state=state)
    assert r.returncode == 0, r.stderr
    plan = json.load(open(tmp_path / "b.json"))
    assert plan["regime"]["regime"] == "DEFENSIVE" and plan["orders"] == []
    assert any("no whole-share core order" in x for x in plan["execution"]["warnings"])


# ======================================================================= N7
def test_n7_state_files_keep_permission_bits(state, tmp_path):
    recs, lr = _seed(state)
    os.chmod(lr, 0o644)
    S.append_anchored(str(recs), {"type": "pipeline_halt", "date": _days(1)})
    assert stat.S_IMODE(os.stat(lr).st_mode) == 0o644
    os.chmod(lr, 0o640)
    S.sync_anchor(str(recs))
    assert stat.S_IMODE(os.stat(lr).st_mode) == 0o640
    out = state / "scorecard.csv"
    S.score(str(recs), str(out), _flat_csv(tmp_path / "q.csv", start=_days(60), n=70))
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o644
    new = state / "fresh.json"
    S._atomic_write_json(str(new), {"a": 1})
    assert stat.S_IMODE(os.stat(new).st_mode) == 0o644


def test_n7_relogged_override_counts_once_toward_streak(state):
    recs = []
    for i, sp in enumerate([-1.0, -0.5]):
        d = _days(30 - i)
        recs += [mark(d, 1000.0, 0.0), _ovr_line(d, sp)]
    d = _days(20)
    recs += [mark(d, 1000.0, 0.0), _ovr_line(d, -0.4, realised_equity_pct=80.0),
             mark(d, 1000.0, 0.0, supersedes_same_date=True),
             _ovr_line(d, -0.3, realised_equity_pct=82.0)]
    build_ledger(state / "recommendations.jsonl", recs)
    # three distinct DATES of negative spreads -> suspended
    assert V.override_suspended(str(state / "recommendations.jsonl")) is not None
    recs2 = recs[:2] + recs[4:]                  # only 2 dates, one of them re-logged
    other = state / "o"
    other.mkdir()
    build_ledger(other / "recommendations.jsonl", recs2)
    assert V.override_suspended(str(other / "recommendations.jsonl")) is None


def test_n7_override_score_matches_on_the_line_not_the_second(state):
    same = "2026-01-05T10:00:00+00:00"
    build_ledger(state / "recommendations.jsonl", [
        mark("2026-01-05", 1000.0, 0.0),
        _ovr_line("2026-01-05", None, logged_utc=same, baseline_equity_pct=95.0,
                  realised_equity_pct=85.0),
        mark("2026-01-05", 1000.0, 0.0, supersedes_same_date=True),
        _ovr_line("2026-01-05", None, logged_utc=same, baseline_equity_pct=95.0,
                  realised_equity_pct=90.0)])
    recs = S.load_recs(str(state / "recommendations.jsonl"))
    o1, o2 = [r for r in recs if r["type"] == "override"]
    S.append_anchored(str(state / "recommendations.jsonl"), {
        "type": "override_score", "date": "2026-02-01", "ref_logged_utc": same,
        "ref_prev_hash": o2["prev_hash"], "ticker": "QQQ", "spread_vs_baseline_pp": -1.5,
        "graded": True, "method": "t", "action": "SCORE"})
    m = {o["realised_equity_pct"]: o for o in S.merged_overrides(S.load_recs(str(state / "recommendations.jsonl")))}
    assert m[90.0]["spread_vs_baseline_pp"] == -1.5
    assert m[85.0]["graded"] is None                  # not cross-matched by logged_utc+ticker


# ================================================================ RT1-9 leftover
def test_rt1_9_cheap_core_share_never_fires_event_without_order():
    rng = random.Random(19)
    for _ in range(4000):
        px = {"QQQ": round(rng.uniform(1, 40), 2), "QQQM": round(rng.uniform(1, 40), 2)}
        sh = rng.randint(0, 30)
        pf = portfolio(round(rng.uniform(0, 120), 2), **({"QQQ": sh} if sh else {}))
        b = make_baseline(regime=rng.choice(["TREND", "DEFENSIVE"]), prices=px, pf=pf)
        if b["events"]["cash_over_max"]:
            assert b["orders"], (px, pf)
        assert all(o["usd"] >= C.MIN_ORDER_USD for o in b["orders"])
    # the case the re-review named: 1 QQQ @16.44 alone is under MIN_ORDER_USD
    b = make_baseline(prices={"QQQ": 16.44, "QQQM": 1.39}, pf=portfolio(31.79))
    assert b["events"]["cash_over_max"] and b["orders"]
    assert [o["ticker"] for o in b["orders"]] == ["QQQM"]


# ======================================================================= D1
def test_d1_gating_numbers_live_in_config(state, tmp_path):
    for sym in ("ADJ_DUP_ABS_USD", "ADJ_DUP_REL", "OVERRIDE_HITRATE_MIN_DECIDED",
                "CRON_LOG_MAX_KIB", "FLOW_VERIFY_MAX_REL_DEV", "FLOW_VERIFY_CUM_DEV_MAX_PCT"):
        assert hasattr(C, sym), sym
    assert not hasattr(S, "ADJ_DUP_ABS_USD") and not hasattr(S, "ADJ_DUP_REL")
    r = run("score_recs.py", "--cron-log-max-kib", state=state)
    assert r.returncode == 0 and r.stdout.strip() == str(C.CRON_LOG_MAX_KIB)
    src = open(os.path.join(SCRIPTS, "install_cron.sh")).read()
    assert "1024k" not in src and "--cron-log-max-kib" in src
    assert "decided >= 3" not in open(os.path.join(SCRIPTS, "score_recs.py")).read()


def test_d1_install_cron_uses_the_config_cap(state, tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    ct = bindir / "crontab"
    ct.write_text("#!/usr/bin/env bash\nif [ \"$1\" = \"-l\" ]; then exit 0; fi\nexit 97\n")
    ct.chmod(0o755)
    r = run("install_cron.sh", state=state, path_prefix=bindir)
    assert r.returncode == 0, r.stderr
    assert f"-size +{C.CRON_LOG_MAX_KIB}k" in r.stdout


def test_d1_hitrate_min_decided_is_the_config_symbol(tmp_path, monkeypatch):
    recs = tmp_path / "recommendations.jsonl"
    build_ledger(recs, [mark("2025-02-01", 1000.0, 0.0), _ovr_line("2025-02-01", -1.0),
                        mark("2025-03-01", 1000.0, 0.0), _ovr_line("2025-03-01", -1.0)])
    csv = _flat_csv(tmp_path / "q.csv")
    assert S.cumulative(str(recs), csv)["override_privileges_at_risk"] is False   # 2 < 3
    monkeypatch.setattr(C, "OVERRIDE_HITRATE_MIN_DECIDED", 2)
    assert S.cumulative(str(recs), csv)["override_privileges_at_risk"] is True


# ======================================================================= D2
def test_d2_valuation_lookup_failure_reads_unknown(state, tmp_path):
    build_ledger(state / "recommendations.jsonl", [
        mark("2025-01-10", 1000.0, 0.0, {"QQQ": {"shares": 10.0, "usd": 1000.0}},
             baseline_cum_return_pct=0.0),
        mark("2025-12-01", 700.0, 0.0, {"QQQ": {"shares": 10.0, "usd": 700.0}},
             baseline_cum_return_pct=0.0)])
    csv = _flat_csv(tmp_path / "q.csv", start="2025-06-01", n=300)   # starts AFTER mark 1
    c = S.cumulative(str(state / "recommendations.jsonl"), csv)
    assert c["price_data_ok"] is False
    assert c["kill_trigger_armed"] == "unknown" and c["board_reconvene_armed"] == "unknown"
    h = run("report.py", "--header", "--prices-csv", csv, state=state)
    hd = json.loads(h.stdout)
    assert hd["가격_데이터_정상"] is False
    assert hd["킬_트리거_발동"] == "unknown" and hd["보드_재소집"] == "unknown"


# ======================================================================= D3
def test_d3_relog_repeating_the_days_flow_is_refused(state, tmp_path):
    build_ledger(state / "recommendations.jsonl",
                 [mark(_days(1), 1868.11, 392.25, {"QQQ": {"shares": 2.0, "usd": 1475.86}})])
    assert _log(state, tmp_path, "--deposit", "200").returncode == 0
    recs = state / "recommendations.jsonl"
    before, before_a = read_bytes(recs), read_bytes(state / "last_run.json")
    for amt in ("200", "200.5"):
        r = _log(state, tmp_path, "--deposit", amt, "--force-relog", "--reason", "rerun")
        assert r.returncode == 4 and "DUPLICATE FLOW" in r.stderr, r.stderr
    assert read_bytes(recs) == before and read_bytes(state / "last_run.json") == before_a
    r = _log(state, tmp_path, "--deposit", "200", "--force-relog", "--reason", "2nd transfer",
             "--additional-flow")
    assert r.returncode == 0, r.stderr
    assert S.external_flows(S.load_recs(str(recs)))[str(utc_today())] == 400.0
    # a re-log that declares nothing still keeps the day's total, no flag needed
    assert _log(state, tmp_path, "--force-relog", "--reason", "no-op").returncode == 0
    assert S.external_flows(S.load_recs(str(recs)))[str(utc_today())] == 400.0
