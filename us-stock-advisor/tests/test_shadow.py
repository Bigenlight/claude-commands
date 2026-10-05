"""Shadow (paper) satellite track: schema, rejection codes, isolation (metamorphic),
logging, grading, graduation."""
from __future__ import annotations

import copy
import json
import math
from datetime import timedelta

import pandas as pd
import pytest

import config as C
import score_recs as S
import validate as V
from helpers import (LIVE_PX, build_ledger, iso_hours_ago, make_baseline, mark, portfolio, run,
                     shadow_levels, utc_today, write_json)

UNI = {"AMD": 150.0, "NVDA": 180.0, "MU": 90.0, "PLTR": 30.0, "AVGO": 300.0}


def pick(t, **kw):
    p = {"ticker": t, "direction": "LONG", "thesis_en": "Dated catalyst.",
         "catalyst_url": f"https://news.example.com/{t.lower()}",
         "catalyst_timestamp": iso_hours_ago(3)}
    p.update(kw)
    return p


def confirm(alloc=None, picks=None, **kw):
    p = {"decision": "CONFIRM_BASELINE", "final_allocation": alloc or {"QQQ": 100.0},
         "satellite": [], "override": None, "vetoes": [], "rationale_en": "confirm"}
    if picks is not None:
        p["shadow_picks"] = picks
    p.update(kw)
    return p


@pytest.fixture
def base():
    return make_baseline(prices=dict(LIVE_PX, **UNI), levels=shadow_levels(UNI))


def do(b, p, state):
    return V.validate(copy.deepcopy(b), copy.deepcopy(p), str(state / "recommendations.jsonl"),
                      str(state / "last_run.json"))


def reasons(out):
    return {r["ticker"]: r["reason"].split(":")[0] for r in out["shadow"]["rejected"]}


def test_valid_pick_is_enriched_from_script_levels(base, state):
    out = do(base, confirm(picks=[pick("AMD")]), state)
    assert out["verdict"] == "PASS"
    acc = out["shadow"]["accepted"]
    assert len(acc) == 1 and out["final_plan"]["shadow_picks"] == acc
    a, lv = acc[0], base["satellite"]["levels"]["AMD"]
    assert a["entry_ref_price"] == lv["price"] and a["stop"] == lv["stop"]
    assert a["target"] == lv["target"] and a["atr_14"] == lv["atr_14"]
    assert a["rr"] == pytest.approx(C.SHADOW_TARGET_ATR_MULT / C.SATELLITE_STOP_ATR_MULT)
    assert a["horizon_days"] == C.SHADOW_HORIZON_DAYS
    assert a["levels_source"] == "core.py/atr"


@pytest.mark.parametrize("bad_key", sorted(V.SHADOW_FORBIDDEN_KEYS))
def test_llm_number_in_shadow_rejected(base, state, bad_key):
    out = do(base, confirm(picks=[pick("AMD", **{bad_key: 1.0})]), state)
    assert out["verdict"] == "PASS"                      # a bad pick never FAILs the run
    assert reasons(out) == {"AMD": "LLM_NUMBER_IN_SHADOW"}
    assert out["final_plan"]["shadow_picks"] == []


def test_rejection_codes(base, state):
    lv_no = copy.deepcopy(base)
    lv_no["satellite"]["levels"].pop("MU")
    picks = [pick("XLE"), pick("NVDA", direction="SHORT"), pick("PLTR", thesis_en=""),
             pick("AVGO", catalyst_url="not-a-url"), pick("MU"),
             pick("AMD", catalyst_timestamp=iso_hours_ago(C.FRESH_CATALYST_MAX_AGE_HOURS + 10))]
    r = reasons(do(lv_no, confirm(picks=picks), state))
    assert r == {"XLE": "TICKER_NOT_IN_UNIVERSE", "NVDA": "SHADOW_DIRECTION",
                 "PLTR": "SHADOW_NO_THESIS", "AVGO": "SHADOW_NO_CATALYST_URL",
                 "MU": "SHADOW_NO_LEVELS", "AMD": "SHADOW_CATALYST_STALE"}


def test_over_limit_and_same_run_duplicate(base, state):
    names = list(UNI)[: C.SHADOW_MAX_PICKS_PER_RUN + 1]
    out = do(base, confirm(picks=[pick("AMD")] + [pick(t) for t in names]), state)
    acc = [a["ticker"] for a in out["shadow"]["accepted"]]
    assert len(acc) == C.SHADOW_MAX_PICKS_PER_RUN
    rs = [r["reason"].split(":")[0] for r in out["shadow"]["rejected"]]
    assert "SHADOW_ALREADY_OPEN" in rs and "SHADOW_OVER_LIMIT" in rs


def test_defensive_weekly_blackout_held(state):
    d = make_baseline(regime="DEFENSIVE", prices=dict(LIVE_PX, **UNI), levels=shadow_levels(UNI),
                      pf=portfolio(737.93, QQQ=1))
    assert reasons(do(d, confirm({"QQQ": 50.0}, picks=[pick("AMD")]), state)) == \
        {"AMD": "SHADOW_IN_DEFENSIVE"}
    w = make_baseline(prices=dict(LIVE_PX, **UNI), levels=shadow_levels(UNI), weekly=True)
    assert reasons(do(w, confirm(picks=[pick("AMD")]), state)) == {"AMD": "SHADOW_WEEKLY"}
    bl = make_baseline(prices=dict(LIVE_PX, **UNI),
                       levels=shadow_levels(UNI, dte={"AMD": C.EARNINGS_BLACKOUT_SESSIONS}))
    assert reasons(do(bl, confirm(picks=[pick("AMD")]), state)) == {"AMD": "EARNINGS_BLACKOUT"}
    h = make_baseline(prices=dict(LIVE_PX, **UNI), levels=shadow_levels(UNI),
                      pf=portfolio(100.0, QQQ=2, NVDA=1))
    ta = h["targets"]["target_allocation"]
    alloc = {("QQQ" if k == "CORE" else k): v for k, v in ta.items()}
    out = do(h, confirm(alloc, picks=[pick("NVDA")]), state)
    assert reasons(out) == {"NVDA": "SHADOW_HELD_REAL"}


def test_cooldown_from_ledger(base, state):
    recent = str(utc_today() - timedelta(days=C.SHADOW_REPICK_COOLDOWN_DAYS - 3))
    old = str(utc_today() - timedelta(days=C.SHADOW_REPICK_COOLDOWN_DAYS + 3))
    build_ledger(state / "recommendations.jsonl", [
        mark(old, 1000.0, 0.0), {"type": "shadow_pick", "date": old, "ticker": "NVDA"},
        mark(recent, 1000.0, 0.0), {"type": "shadow_pick", "date": recent, "ticker": "AMD"}])
    out = do(base, confirm(picks=[pick("AMD"), pick("NVDA")]), state)
    assert reasons(out) == {"AMD": "SHADOW_ALREADY_OPEN"}
    assert [a["ticker"] for a in out["shadow"]["accepted"]] == ["NVDA"]


def test_non_list_shadow_picks(base, state):
    out = do(base, confirm(picks="AMD please"), state)
    assert out["verdict"] == "PASS"
    assert out["shadow"]["accepted"] == [] and out["shadow"]["rejected"]
    assert out["shadow"]["rejected"][0]["reason"].startswith("SHADOW_SCHEMA")


# ------------------------------------------------ metamorphic isolation
def _proposals():
    ovr = {"catalyst_description": "x", "catalyst_url": "https://example.com/y",
           "catalyst_timestamp": iso_hours_ago(2), "expected_cost_if_wrong_pct": 1.0,
           "qqq_forward_20d_if_i_am_wrong": 2.0, "direction": "de_risk"}
    return {
        "confirm_ok": confirm(),
        "confirm_split": confirm({"QQQ": 79.0, "QQQM": 16.3}),
        "confirm_mismatch": confirm({"QQQ": 90.0}),
        "cash": confirm({"QQQ": 90.0, "CASH": 10.0}),
        "override_ok": {"decision": "OVERRIDE", "final_allocation": {"QQQ": 100.0},
                        "override": ovr, "satellite": [], "vetoes": []},
        "override_below_floor": {"decision": "OVERRIDE", "final_allocation": {"QQQ": 85.0},
                                 "override": ovr, "satellite": [], "vetoes": []},
        "not_graduated": confirm({"QQQ": 90.0, "NVDA": 10.0}),
        "garbage_decision": confirm(decision="YOLO"),
    }


SHADOW_VARIANTS = [
    [pick("AMD")],
    [pick("AMD"), pick("MU", stop=1.0), pick("XLE"), pick("PLTR")],
    [pick(t) for t in UNI] + [{"ticker": None}, "junk"],
    "not a list",
]


@pytest.mark.parametrize("name", list(_proposals()))
@pytest.mark.parametrize("variant", range(len(SHADOW_VARIANTS)))
def test_shadow_isolation_metamorphic(base, state, monkeypatch, name, variant):
    monkeypatch.setattr(V, "_url_resolves", lambda url: True)
    p = _proposals()[name]
    with_s = dict(copy.deepcopy(p), shadow_picks=copy.deepcopy(SHADOW_VARIANTS[variant]))
    a, b = do(base, p, state), do(base, with_s, state)
    assert a["verdict"] == b["verdict"]
    # The ONLY permitted difference: SHADOW_IN_ALLOCATION, which fires when a picked
    # ticker is also given real weight (that proposal already FAILs without it).
    extra = [v for v in b["violations"] if v.startswith("SHADOW_IN_ALLOCATION")]
    if extra:
        assert a["verdict"] == "FAIL"
    assert a["violations"] == [v for v in b["violations"] if v not in extra]
    assert a["final_plan"]["orders"] == b["final_plan"]["orders"]
    assert a["final_plan"].get("final_allocation") == b["final_plan"].get("final_allocation")
    assert a["final_plan"].get("execution") == b["final_plan"].get("execution")


def test_shadow_ticker_in_allocation_fails(base, state):
    out = do(base, confirm({"QQQ": 90.0, "AMD": 10.0}, picks=[pick("AMD")]), state)
    assert out["verdict"] == "FAIL"
    cs = {v.split(":")[0] for v in out["violations"]}
    assert {"SHADOW_IN_ALLOCATION", "SATELLITE_NOT_GRADUATED"} <= cs
    assert out["final_plan"]["orders"] == base["orders"]
    # rejected-or-not, a pick never becomes an order
    assert all(o["ticker"] in C.CORE_TICKERS for o in out["final_plan"]["orders"])


# -------------------------------------------------- logging + grading
def test_report_logs_shadow_pick_lines(base, state, tmp_path):
    out = do(base, confirm(picks=[pick("AMD"), pick("NVDA")]), state)
    bp = write_json(tmp_path / "b.json", base)
    fp = write_json(tmp_path / "f.json", out)
    r = run("report.py", "--log", "--baseline", bp, "--final", fp, state=state)
    assert r.returncode == 0, r.stderr
    recs = S.load_recs(str(state / "recommendations.jsonl"))
    sp = [x for x in recs if x["type"] == "shadow_pick"]
    assert {x["ticker"] for x in sp} == {"AMD", "NVDA"}
    for x in sp:
        assert x["action"] == "SHADOW_LONG" and x["real_money"] is False
        lv = base["satellite"]["levels"][x["ticker"]]
        assert x["stop"] == lv["stop"] and x["target"] == lv["target"]
    # a shadow pick is never an order line
    assert not [x for x in recs if x["type"] == "order" and x["ticker"] in ("AMD", "NVDA")]


def _grading_csv(path, n_days=260):
    idx = pd.date_range("2025-01-01", periods=n_days, freq="D")
    df = pd.DataFrame({"QQQ": [100.0] * n_days,
                       "HIT": [100.0 * 1.001 ** i for i in range(n_days)],
                       "MISS": [100.0 * 0.999 ** i for i in range(n_days)]}, index=idx)
    df.index.name = "Date"
    df.to_csv(path)
    return str(path)


def _shadow_ledger(path, hits, misses):
    recs, d0 = [], pd.Timestamp("2025-01-01")
    for i in range(hits + misses):
        d = str((d0 + pd.Timedelta(days=i)).date())
        t = "HIT" if i < hits else "MISS"
        recs += [mark(d, 1000.0, 0.0),
                 {"type": "shadow_pick", "date": d, "ticker": t, "action": "SHADOW_LONG",
                  "stop": 90.0, "target": 101.5, "real_money": False}]
    build_ledger(path, recs)


def test_score_grades_shadow_picks(state, tmp_path):
    csv = _grading_csv(tmp_path / "g.csv")
    recs = state / "recommendations.jsonl"
    _shadow_ledger(recs, 1, 1)
    rows = S.score(str(recs), str(state / "scorecard.csv"), csv)
    sp = [r for r in rows if r["type"] == "shadow_pick"]
    hit = next(r for r in sp if r["ticker"] == "HIT")
    miss = next(r for r in sp if r["ticker"] == "MISS")
    for h in (1, 5, 20):
        assert hit[f"fwd_{h}d"] == pytest.approx(round((1.001 ** h - 1) * 100, 2))
        assert hit[f"qqq_{h}d"] == 0.0
        assert miss[f"excess_{h}d"] < 0
    assert hit["bracket_outcome"] == "TARGET"       # 101.5 reached within 20 sessions
    assert miss["bracket_outcome"] == "OPEN"        # never touches 90 within 20 sessions


def test_binom_sf_hand_values():
    assert S.binom_sf(0, 5, 0.3) == 1.0
    assert S.binom_sf(6, 5, 0.3) == 0.0
    assert S.binom_sf(1, 1, 0.4) == pytest.approx(0.4)
    assert S.binom_sf(2, 2, 0.5) == pytest.approx(0.25)
    assert S.binom_sf(2, 3, 0.5) == pytest.approx(0.5)
    assert S.binom_sf(1, 3, 0.398) == pytest.approx(1 - 0.602 ** 3)
    # 14 of 20 vs the dartboard, by an independent complement sum
    p = C.DARTBOARD_BASE_RATE
    lower = sum(math.comb(20, i) * p ** i * (1 - p) ** (20 - i) for i in range(0, 14))
    assert S.binom_sf(14, 20, p) == pytest.approx(1 - lower, rel=1e-9)


def test_graduation(state, tmp_path):
    csv = _grading_csv(tmp_path / "g.csv")
    recs = state / "recommendations.jsonl"
    _shadow_ledger(recs, 14, C.SHADOW_GRADUATION_MIN_PICKS - 14)
    st = S.shadow_stats(str(recs), csv)
    assert st["n_graded"] == C.SHADOW_GRADUATION_MIN_PICKS and st["hits"] == 14
    assert st["p_value"] <= C.SHADOW_GRADUATION_MAX_PVALUE
    assert st["mean_excess_20d_pp"] > C.SHADOW_GRADUATION_MIN_MEAN_EXCESS_PP
    assert st["graduation_ok"] is True
    # graduation is REPORTED, never applied
    assert C.SATELLITE_REAL_MONEY_ENABLED is False


def test_no_graduation_below_min_picks(state, tmp_path):
    csv = _grading_csv(tmp_path / "g.csv")
    recs = state / "recommendations.jsonl"
    _shadow_ledger(recs, 14, C.SHADOW_GRADUATION_MIN_PICKS - 15)
    st = S.shadow_stats(str(recs), csv)
    assert st["n_graded"] == C.SHADOW_GRADUATION_MIN_PICKS - 1
    assert st["graduation_ok"] is False


def test_score_cli_shadow(state, tmp_path):
    csv = _grading_csv(tmp_path / "g.csv")
    _shadow_ledger(state / "recommendations.jsonl", 2, 1)
    r = run("score_recs.py", "--shadow", "--prices-csv", csv, state=state)
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    assert d["n_total"] == 3 and d["hits"] == 2 and d["graduation_ok"] is False
