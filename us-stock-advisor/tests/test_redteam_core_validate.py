"""Regression tests for red-team RT1 findings 1-10 (+ the stale-baseline HALT) on
config.py / core.py / validate.py. One block per finding; each test reproduces the
original break and asserts the fixed behaviour."""
from __future__ import annotations

import copy
import json
import math
import random
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

import config as C
import core as CORE
import score_recs as S
import validate as V
from helpers import (LIVE_PX, iso_hours_ago, make_baseline, make_prices_csv, portfolio, run,
                     shadow_levels, write_json)


def codes(out):
    return {x.split(":")[0].split(" ")[0] for x in out["violations"]}


def do(baseline, proposal, state):
    return V.validate(copy.deepcopy(baseline), copy.deepcopy(proposal),
                      str(state / "recommendations.jsonl"), str(state / "last_run.json"))


def ovr(direction="de_risk", **kw):
    o = {"catalyst_description": "dated event", "catalyst_url": "https://example.com/x",
         "catalyst_timestamp": iso_hours_ago(2), "expected_cost_if_wrong_pct": 1.0,
         "qqq_forward_20d_if_i_am_wrong": 3.0, "direction": direction}
    o.update(kw)
    return o


def override(alloc, direction="de_risk", **kw):
    p = {"decision": "OVERRIDE", "final_allocation": alloc, "satellite": [],
         "override": ovr(direction), "vetoes": []}
    p.update(kw)
    return p


def confirm(alloc, **kw):
    p = {"decision": "CONFIRM_BASELINE", "final_allocation": alloc, "satellite": [],
         "override": None, "vetoes": []}
    p.update(kw)
    return p


def sat_buy(t, **kw):
    d = {"ticker": t, "action": "BUY", "size_pct": 2, "catalyst_url": "https://e.com/a",
         "catalyst_timestamp": iso_hours_ago(2)}
    d.update(kw)
    return d


def shadow(t):
    return {"ticker": t, "direction": "LONG", "thesis_en": "x",
            "catalyst_url": "https://example.com/c", "catalyst_timestamp": iso_hours_ago(2)}


def apply_orders(b):
    cash = b["portfolio"]["cash_usd"]
    pos = {t: d["shares"] for t, d in b["portfolio"]["positions"].items()}
    for o in b["orders"]:
        sgn = 1 if o["action"] == "BUY" else -1
        pos[o["ticker"]] = pos.get(o["ticker"], 0.0) + sgn * o["shares"]
        cash -= sgn * o["usd"]
    return cash, {t: s for t, s in pos.items() if s > 1e-9}


PX_SAT = dict(LIVE_PX, NVDA=227.21, AMD=150.0)


@pytest.fixture
def urls_ok(monkeypatch):
    monkeypatch.setattr(V, "_url_resolves", lambda url: True)


# ------------------------------------------------------------------ RT1-1 (HIGH)
def test_rt1_1_defensive_relabelled_override_cannot_deepen(state, urls_ok):
    b = make_baseline(regime="DEFENSIVE", pf=portfolio(0.0, QQQ=100))
    assert b["execution"]["post_trade"]["equity_pct"] == pytest.approx(50.0, abs=0.1)
    # original break: {"QQQ":40} labelled re_risk -> PASS, SELL 60
    out = do(b, override({"QQQ": 40.0}, "re_risk"), state)
    assert out["verdict"] == "FAIL"
    assert out["final_plan"]["orders"] == b["orders"]
    # inside the old 10pp allowance but still below the baseline's realised equity
    out = do(b, override({"QQQ": 47.0}, "re_risk"), state)
    assert out["verdict"] == "FAIL" and "OVERRIDE_DEEPENS_DEFENSIVE" in codes(out)
    assert out["final_plan"]["orders"] == b["orders"]
    # a genuine re-risk still passes and realises more equity than the baseline
    out = do(b, override({"QQQ": 60.0}, "re_risk"), state)
    assert out["verdict"] == "PASS", out["violations"]
    assert out["final_plan"]["execution"]["post_trade"]["equity_pct"] >= 50.0


# ------------------------------------------------------------------ RT1-2
@pytest.mark.parametrize("pick", ["QQQ", "QQQM", " qqq "])
def test_rt1_2_core_ticker_shadow_pick_does_not_change_verdict(state, urls_ok, pick):
    # v5.1.2 (N3): an OVERRIDE must change the orders, so the passing override here is
    # a real one on a book where {"QQQ": 90} derives different whole-share orders
    b = make_baseline(pf=portfolio(2000.0, QQQ=10))
    for prop in (confirm({"QQQ": 100.0}), override({"QQQ": 90.0})):
        plain = do(b, prop, state)
        with_pick = do(b, dict(prop, shadow_picks=[shadow(pick)]), state)
        assert plain["verdict"] == "PASS", plain["violations"]
        assert with_pick["verdict"] == plain["verdict"]
        assert with_pick["violations"] == plain["violations"]
        assert with_pick["final_plan"]["orders"] == plain["final_plan"]["orders"]
        assert with_pick["shadow"]["accepted"] == []
        assert any("TICKER_NOT_IN_UNIVERSE" in r["reason"]
                   for r in with_pick["shadow"]["rejected"])


# ------------------------------------------------------------------ RT1-3
def test_rt1_3a_override_cannot_keep_satellite_in_defensive(state, urls_ok):
    b = make_baseline(regime="DEFENSIVE", prices=PX_SAT, pf=portfolio(50.0, QQQ=2, NVDA=1))
    assert any(o["ticker"] == "NVDA" and o["action"] == "SELL" for o in b["orders"])
    out = do(b, override({"QQQ": 50.0, "NVDA": 10.0}, "re_risk"), state)
    assert out["verdict"] == "FAIL" and "SATELLITE_IN_DEFENSIVE" in codes(out)
    assert out["final_plan"]["orders"] == b["orders"]          # force-close still runs


def test_rt1_3b_override_cannot_cancel_stop_breach(state, urls_ok):
    b = make_baseline(prices=PX_SAT, pf=portfolio(20.0, QQQ=2, NVDA=1),
                      satellite_state={"stops_atr": {"NVDA": 240.0}})
    assert b["satellite"]["stop_breaches"] == ["NVDA"]
    out = do(b, override({"QQQ": 90.0, "NVDA": 10.0}), state)
    assert out["verdict"] == "FAIL" and "STOP_BREACH_IN_ALLOCATION" in codes(out)
    assert any(o.get("stop_breach") for o in out["final_plan"]["orders"])


def test_rt1_3c_allocation_cut_of_held_satellite_needs_declared_sell(state, urls_ok,
                                                                     monkeypatch):
    b = make_baseline(prices=PX_SAT, pf=portfolio(20.0, QQQ=40, NVDA=1))
    assert not any(o["ticker"] == "NVDA" for o in b["orders"])
    out = do(b, override({"QQQ": 100.0}), state)
    assert out["verdict"] == "FAIL" and "SATELLITE_SELL_NOT_DECLARED" in codes(out)
    # declared, not recently bought -> legal
    sell = [{"ticker": "NVDA", "action": "SELL"}]
    out = do(b, override({"QQQ": 100.0}, satellite=sell), state)
    assert out["verdict"] == "PASS", out["violations"]
    assert any(o["ticker"] == "NVDA" and o["action"] == "SELL"
               for o in out["final_plan"]["orders"])
    # declared but inside min-hold -> the churn guard now actually runs
    recent = datetime.now(timezone.utc) - timedelta(days=3)
    monkeypatch.setattr(V, "_satellite_history", lambda p: ({"NVDA": recent}, {}))
    out = do(b, override({"QQQ": 100.0}, satellite=sell), state)
    assert out["verdict"] == "FAIL" and "SATELLITE_MIN_HOLD_VIOLATION" in codes(out)


# ------------------------------------------------------------------ RT1-4
def test_rt1_4_defensive_sell_never_oversells_into_a_rebuy():
    px = {"QQQ": 700.0, "QQQM": 288.19}
    b1 = make_baseline(regime="DEFENSIVE", prices=px, pf=portfolio(1330.0, QQQ=5))
    assert [(o["action"], o["ticker"], o["shares"]) for o in b1["orders"]] == [("SELL", "QQQ", 1.0)]
    cash, pos = apply_orders(b1)
    b2 = make_baseline(regime="DEFENSIVE", prices=px, pf=portfolio(cash, **pos))
    assert not any(o["action"] == "BUY" for o in b2["orders"])
    assert b2["events"]["cash_over_max"] is False


def test_rt1_4_property_no_two_run_conversion():
    rng = random.Random(4)
    for _ in range(1500):
        qqq = rng.uniform(300, 900)
        px = {"QQQ": round(qqq, 2), "QQQM": round(qqq * 0.4117, 2)}
        scale = rng.choice([1, 3, 10])
        pf = portfolio(rng.choice([0.0, rng.uniform(0, 3000) * scale]),
                       QQQ=rng.choice([1, 2, 3, 5]) * scale, QQQM=rng.randint(0, 6) * scale)
        b1 = make_baseline(regime="DEFENSIVE", prices=px, pf=pf)
        if not any(o["action"] == "SELL" and o["ticker"] in C.CORE_TICKERS for o in b1["orders"]):
            continue
        cash, pos = apply_orders(b1)
        b2 = make_baseline(regime="DEFENSIVE", prices=px, pf=portfolio(cash, **pos))
        assert not any(o["action"] == "BUY" for o in b2["orders"]), (pf, b1["orders"], b2["orders"])
        assert b2["events"]["cash_over_max"] is False


# ------------------------------------------------------------------ RT1-5
def test_rt1_5_fresh_last_price_unit():
    idx = pd.date_range("2026-09-01", periods=10, freq="D")
    s = pd.Series([1.0] * 4 + [float("nan")] * 6, index=idx)
    assert CORE.fresh_last_price(s, idx[-1])[0] is None
    assert CORE.fresh_last_price(s.fillna(2.0), idx[-1])[0] == 2.0


def _stale_csv(tmp_path, ticker, n=6):
    p = make_prices_csv(tmp_path / "px.csv", extra={"NVDA": 227.21})
    df = pd.read_csv(p, parse_dates=["Date"]).set_index("Date")
    df.loc[df.index[-n:], ticker] = float("nan")
    df.to_csv(p)
    return p


def test_rt1_5_stale_unheld_qqqm_falls_back_to_qqq_only(state, tmp_path):
    csv = _stale_csv(tmp_path, "QQQM")
    pf = write_json(tmp_path / "pf.json", portfolio(800.0, QQQ=2))
    out = tmp_path / "b.json"
    r = run("core.py", "--prices-csv", csv, "--portfolio", pf, "--out", out, state=state)
    assert r.returncode == 0, r.stderr
    b = json.loads(out.read_text())
    assert "QQQM" not in b["prices"]
    assert all(o["ticker"] != "QQQM" for o in b["orders"])
    assert any("QQQM" in w and "stale" in w for w in b["execution"]["warnings"])


def test_rt1_5_stale_held_ticker_is_fatal(state, tmp_path):
    for t, pf_ in (("QQQM", portfolio(0.0, QQQ=2, QQQM=1)),
                   ("NVDA", portfolio(0.0, QQQ=2, NVDA=1))):
        csv = _stale_csv(tmp_path, t)
        pf = write_json(tmp_path / "pf.json", pf_)
        r = run("core.py", "--prices-csv", csv, "--portfolio", pf,
                "--out", tmp_path / "b.json", state=state)
        assert r.returncode == 3, (t, r.stderr)
        assert "fresh price" in r.stderr


def test_rt1_5_stale_universe_name_gets_no_levels(state, tmp_path):
    csv = _stale_csv(tmp_path, "NVDA")
    pf = write_json(tmp_path / "pf.json", portfolio(392.25, QQQ=2))
    out = tmp_path / "b.json"
    r = run("core.py", "--prices-csv", csv, "--portfolio", pf, "--out", out, state=state)
    assert r.returncode == 0, r.stderr
    assert "NVDA" not in json.loads(out.read_text())["satellite"]["levels"]


# ------------------------------------------------------------------ RT1-6
def test_rt1_6_unfunded_satellite_buy_fails(state, urls_ok, monkeypatch):
    monkeypatch.setattr(C, "SATELLITE_REAL_MONEY_ENABLED", True)
    b = make_baseline(prices=PX_SAT, pf=portfolio(0.0, QQQ=100),
                      levels=shadow_levels({"NVDA": 227.21}))
    prop = override({"QQQ": 98.1, "NVDA": 1.9}, satellite=[sat_buy("NVDA", size_pct=1.9)])
    out = do(b, prop, state)
    assert out["verdict"] == "FAIL"
    assert out["final_plan"]["orders"] == b["orders"]
    ex = CORE.derive_execution({"QQQ": 98.1, "NVDA": 1.9}, b)
    assert ex["execution"]["post_trade"]["cash_usd"] >= 0


def test_rt1_6_derive_never_negative_cash():
    rng = random.Random(6)
    for _ in range(800):
        pf = portfolio(rng.choice([0.0, rng.uniform(0, 500)]), QQQ=rng.choice([1, 2, 10, 100]),
                       NVDA=rng.choice([0, 1, 3]))
        b = make_baseline(prices=PX_SAT, pf=pf)
        core_w = rng.uniform(40, 100)
        alloc = {"QQQ": core_w, "NVDA": rng.uniform(0, 100 - core_w), "AMD": rng.uniform(0, 5)}
        ex = CORE.derive_execution(alloc, b)
        assert ex["execution"]["post_trade"]["cash_usd"] >= -0.005, (pf, alloc, ex["orders"])


# ------------------------------------------------------------------ RT1-7
def test_rt1_7_override_against_weekly_baseline_fails(state, urls_ok):
    w = make_baseline(pf=portfolio(392.25, QQQ=2), weekly=True)
    assert w["orders"] == []
    out = do(w, override({"QQQ": 100.0}), state)
    assert out["verdict"] == "FAIL" and "OVERRIDE_IN_WEEKLY" in codes(out)
    assert out["final_plan"]["orders"] == []


# ------------------------------------------------------------------ RT1-8
def test_rt1_8_confirm_matching_baseline_reference_passes(state):
    # held satellite the whole-share trim cannot bring under budget
    b = make_baseline(prices=PX_SAT, pf=portfolio(20.0, QQQ=2, NVDA=3))
    post = b["execution"]["post_trade_allocation"]
    assert post["NVDA"] > C.SATELLITE_MAX_PCT * 100
    out = do(b, confirm(dict(post)), state)
    assert out["verdict"] == "PASS", out["violations"]
    # all-cash book too small to buy one share -> post-trade allocation {}
    e = make_baseline(pf=portfolio(250.0))
    assert e["orders"] == [] and e["execution"]["post_trade_allocation"] == {}
    out = do(e, confirm({}), state)
    assert out["verdict"] == "PASS", out["violations"]
    # a mismatching CONFIRM still fails, and an empty OVERRIDE is still refused
    assert do(e, confirm({"NVDA": 5.0}), state)["verdict"] == "FAIL"
    b2 = make_baseline(pf=portfolio(392.25, QQQ=2))
    assert "SCHEMA_VIOLATION" in codes(do(b2, confirm({}), state))


# ------------------------------------------------------------------ RT1-9
def test_rt1_9_deployable_false_after_any_buy():
    rng = random.Random(9)
    for _ in range(3000):
        qqq = rng.uniform(300, 900)
        px = {"QQQ": qqq, "QQQM": qqq * 0.4117}
        if rng.random() < 0.2:
            px.pop("QQQM")
        scale = rng.choice([1, 3, 10, 50])
        pf = portfolio(rng.uniform(0, 3000) * scale, QQQ=rng.choice([0, 1, 2, 5]) * scale)
        b = make_baseline(regime=rng.choice(["TREND", "DEFENSIVE"]), prices=px, pf=pf)
        if any(o["action"] == "BUY" for o in b["orders"]):
            assert b["execution"]["residual"]["deployable"] is False, (pf, px, b["orders"])


def test_rt1_9_residual_reason_is_honest_about_the_band():
    w = make_baseline(pf=portfolio(392.25, QQQ=2), weekly=True)
    res = w["execution"]["residual"]
    assert res["deployable"] is True
    assert "exceeds" in res["reason"] and "within" not in res["reason"]


def test_rt1_9_share_cheaper_than_min_order_no_empty_event():
    px = {"QQQ": 15.0, "QQQM": 6.0}
    b = make_baseline(prices=px, pf=portfolio(15.0, QQQ=10))
    assert b["orders"] == []
    assert b["events"]["cash_over_max"] is False
    # v5.1.2 (RT1-9 leftover): the threshold is the smallest EMITTABLE line: 4 QQQM
    # (24.00 >= MIN_ORDER_USD) plus the buffer, not MIN_ORDER_USD itself
    assert CORE.deploy_threshold_usd(px) == pytest.approx(4 * 6.0 * (1 + C.WHOLE_SHARE_PRICE_BUFFER_PCT))


# ------------------------------------------------------------------ RT1-10
@pytest.mark.parametrize("extra", [
    {"override": "yes"}, {"override": ["x"]}, {"satellite": ["NVDA"]}, {"satellite": "NVDA"},
    {"satellite": [None, 5]}, {"vetoes": ["x"]}, {"vetoes": "x"}, {"vetoes": [{"ticker": 1}]},
    {"satellite": [{"ticker": "NVDA", "action": "SELL", "size_pct": "x"}]},
    {"satellite": [{"ticker": ["NVDA"], "action": "BUY", "size_pct": float("nan")}]},
    {"decision": ["OVERRIDE"]}, {"decision": {"a": 1}},
])
def test_rt1_10_validate_never_raises_on_malformed_items(state, urls_ok, extra):
    b = make_baseline(pf=portfolio(392.25, QQQ=2))
    p = override({"QQQ": 100.0})
    p.update(extra)
    out = V.validate(copy.deepcopy(b), p, str(state / "recommendations.jsonl"),
                     str(state / "last_run.json"))
    assert out["verdict"] == "FAIL"
    assert out["final_plan"]["orders"] == b["orders"]


@pytest.mark.parametrize("age", [float("nan"), float("inf"), "abc", None, -1.0, True])
def test_rt1_10_non_finite_data_age_fails(state, age):
    b = make_baseline(pf=portfolio(392.25, QQQ=2))
    b["data"]["data_age_hours"] = age
    out = do(b, confirm({"QQQ": 100.0}), state)
    assert out["verdict"] == "FAIL" and "DATA_AGE_INVALID" in codes(out)


def test_rt1_10_validate_catches_residual_exceptions(state, monkeypatch):
    b = make_baseline(pf=portfolio(392.25, QQQ=2))

    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(V, "_priceable_set", boom)
    out = do(b, confirm({"QQQ": 100.0}), state)
    assert out["verdict"] == "FAIL" and out["final_plan"]["orders"] == b["orders"]


# ------------------------------------------------------------ stale baseline HALT
@pytest.mark.parametrize("gen", [
    lambda: (datetime.now(timezone.utc) - timedelta(hours=C.BASELINE_MAX_AGE_HOURS + 1)).isoformat(),
    lambda: (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    lambda: None, lambda: "not a date",
])
def test_stale_or_undated_baseline_halts(state, tmp_path, gen):
    b = make_baseline(pf=portfolio(392.25, QQQ=2))
    g = gen()
    if g is None:
        b.pop("generated_utc")
    else:
        b["generated_utc"] = g
    out = do(b, confirm({"QQQ": 100.0}), state)
    assert out["verdict"] == "FAIL" and out.get("halt") is True
    assert out["final_plan"]["source"] == "HALT" and out["final_plan"]["orders"] == []
    assert out["violations"][0].startswith("HALT:")
    bp = write_json(tmp_path / "b.json", b)
    pp = write_json(tmp_path / "p.json", confirm({"QQQ": 100.0}))
    r = run("validate.py", "--baseline", bp, "--proposal", pp, state=state)
    assert r.returncode == 2 and json.loads(r.stdout)["final_plan"]["orders"] == []


def test_fresh_baseline_does_not_halt(state):
    b = make_baseline(pf=portfolio(392.25, QQQ=2))
    out = do(b, confirm({"QQQ": 100.0}), state)
    assert out["verdict"] == "PASS" and not out.get("halt")


# ---------------------------------------------------- interface contracts (Fixer B)
def test_rejected_override_contract(state, urls_ok):
    b = make_baseline(pf=portfolio(392.25, QQQ=2))
    bad = do(b, override({"QQQ": 85.0}), state)
    assert bad["verdict"] == "FAIL"
    ro = bad["final_plan"]["rejected_override"]
    assert ro["override"]["direction"] == "de_risk"
    assert ro["proposed_allocation"] == {"CORE": 85.0}
    assert ro["violations"] == bad["violations"] and ro["violations"]
    assert bad["final_plan"]["orders"] == b["orders"]
    malformed = do(b, dict(override({"QQQ": 85.0}), override="yes"), state)
    assert malformed["final_plan"]["rejected_override"]["override"] is None
    ok = do(make_baseline(pf=portfolio(2000.0, QQQ=10)), override({"QQQ": 90.0}), state)  # N3
    assert ok["verdict"] == "PASS" and not ok["final_plan"].get("rejected_override")
    assert "equity_pct" in ok["final_plan"]["execution"]["post_trade"]
    cf = do(b, confirm({"QQQ": 50.0}), state)
    assert cf["verdict"] == "FAIL" and not cf["final_plan"].get("rejected_override")


def test_satellite_history_uses_score_recs_helper_when_present(state, urls_ok, monkeypatch):
    recent = (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()
    monkeypatch.setattr(S, "satellite_history_recs",
                        lambda recs: [{"type": "order", "date": recent, "ticker": "NVDA",
                                       "action": "BUY"}], raising=False)
    last_buy, _ = V._satellite_history(str(state / "recommendations.jsonl"))
    assert "NVDA" in last_buy
