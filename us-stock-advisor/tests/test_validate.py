"""Phase 3: validate.py with the two-ticker core, CONFIRM pinning, cash/leverage/short
rejection, the override execution floor, and the real-money satellite switch."""
from __future__ import annotations

import copy
import json

import pytest

import config as C
import validate as V
from helpers import (LIVE_PX, iso_hours_ago, make_baseline, portfolio, run, shadow_levels,
                     write_json)


def codes(out):
    return {v.split(":")[0].split(" ")[0] for v in out["violations"]}


def do(baseline, proposal, state):
    return V.validate(copy.deepcopy(baseline), copy.deepcopy(proposal),
                      str(state / "recommendations.jsonl"), str(state / "last_run.json"))


def confirm(alloc, **kw):
    p = {"decision": "CONFIRM_BASELINE", "final_allocation": alloc, "satellite": [],
         "override": None, "vetoes": [], "rationale_en": "confirm"}
    p.update(kw)
    return p


def good_override(**kw):
    o = {"catalyst_description": "dated event", "catalyst_url": "https://example.com/x",
         "catalyst_timestamp": iso_hours_ago(2), "expected_cost_if_wrong_pct": 1.0,
         "qqq_forward_20d_if_i_am_wrong": 3.0, "direction": "de_risk"}
    o.update(kw)
    return o


def override(alloc, ovr=None, **kw):
    p = {"decision": "OVERRIDE", "final_allocation": alloc, "satellite": [],
         "override": ovr if ovr is not None else good_override(), "vetoes": [],
         "rationale_en": "override"}
    p.update(kw)
    return p


@pytest.fixture
def base():
    px = dict(LIVE_PX, NVDA=180.0, AMD=150.0)
    return make_baseline(prices=px, levels=shadow_levels({"NVDA": 180.0, "AMD": 150.0}))


@pytest.fixture
def urls_ok(monkeypatch):
    monkeypatch.setattr(V, "_url_resolves", lambda url: True)


# ---------------------------------------------------------------- CONFIRM
@pytest.mark.parametrize("alloc", [{"QQQ": 100.0}, {"QQQ": 79.0, "QQQM": 16.3},
                                   {"QQQM": 100.0}, {"QQQ": 95.3}, {"qqq": 60, "QQQM": 40}])
def test_confirm_equivalent_core_spellings_pass(base, state, alloc):
    out = do(base, confirm(alloc), state)
    assert out["verdict"] == "PASS", out["violations"]
    assert out["final_plan"]["orders"] == base["orders"]            # verbatim
    assert out["final_plan"]["source"] == "BASELINE"
    assert out["final_plan"]["execution"] == base["execution"]


def test_confirm_mismatch(base, state):
    out = do(base, confirm({"QQQ": 90.0}), state)
    assert out["verdict"] == "FAIL"
    assert "CONFIRM_MISMATCH" in codes(out)
    assert out["final_plan"]["source"] == "BASELINE_ENFORCED"
    assert out["final_plan"]["orders"] == base["orders"]


def test_llm_literal_core_key_is_not_core(base, state):
    out = do(base, confirm({"CORE": 100.0}), state)
    assert out["verdict"] == "FAIL"
    assert "TICKER_NOT_IN_UNIVERSE" in codes(out)


def test_legacy_baseline_without_execution_does_not_crash(state):
    b = make_baseline()
    for k in ("prices", "execution"):
        b.pop(k)
    out = do(b, confirm({"QQQ": 100.0}), state)
    assert out["verdict"] in ("PASS", "FAIL")
    assert "final_plan" in out and out["final_plan"]["orders"] == b["orders"]


# ------------------------------------------------------- cash / proxies
@pytest.mark.parametrize("key", ["cash", "CASH", "USD", "KRW", "MMF", "SGOV", "BIL"])
@pytest.mark.parametrize("decision", ["CONFIRM", "OVERRIDE"])
def test_cash_and_cash_proxies_fail(base, state, urls_ok, key, decision):
    alloc = {"QQQ": 90.0, key: 10.0}
    p = confirm(alloc) if decision == "CONFIRM" else override(alloc)
    out = do(base, p, state)
    assert out["verdict"] == "FAIL"
    assert codes(out) & {"SCHEMA_VIOLATION", "TICKER_NOT_IN_UNIVERSE"}
    assert out["final_plan"]["orders"] == base["orders"]


def test_cash_field_in_proposal_fails(base, state):
    out = do(base, confirm({"QQQ": 100.0}, cash_pct=5.0), state)
    assert out["verdict"] == "FAIL" and "SCHEMA_VIOLATION" in codes(out)


def test_orders_field_in_proposal_fails(base, state):
    out = do(base, confirm({"QQQ": 100.0}, orders=[{"ticker": "QQQ"}]), state)
    assert out["verdict"] == "FAIL"


# ------------------------------------------------------ leverage / short
def test_leverage_rejected(base, state, urls_ok):
    out = do(base, override({"QQQ": 140.0}), state)
    assert out["verdict"] == "FAIL"
    assert {"ALLOCATION_SUM_OVER_100", "WEIGHT_EXCEEDS_BOOK"} <= codes(out)


def test_short_rejected(base, state, urls_ok):
    out = do(base, override({"QQQ": 140.0, "NVDA": -40.0}), state)
    assert out["verdict"] == "FAIL"
    assert "NEGATIVE_WEIGHT" in codes(out)


def test_non_numeric_weight_is_listed_not_crash(base, state):
    out = do(base, confirm({"QQQ": "ninety"}), state)
    assert out["verdict"] == "FAIL" and "SCHEMA_VIOLATION" in codes(out)


# --------------------------------------------------------- priceability
def test_qqqm_priceable_while_unheld_but_unpriced_etf_is_not(base, state):
    assert "QQQM" not in base["portfolio"]["positions"]
    assert do(base, confirm({"QQQM": 100.0}), state)["verdict"] == "PASS"
    out = do(base, confirm({"VOO": 100.0}), state)
    assert out["verdict"] == "FAIL" and "UNPRICEABLE_IN_ALLOCATION" in codes(out)


# ------------------------------------------------------ override channel
def test_override_catalyst_rules(base, state, monkeypatch):
    monkeypatch.setattr(V, "_url_resolves", lambda url: False)
    out = do(base, override({"QQQ": 100.0}), state)
    assert out["verdict"] == "FAIL" and "OVERRIDE_URL_UNREACHABLE" in codes(out)
    monkeypatch.setattr(V, "_url_resolves", lambda url: True)
    stale = good_override(catalyst_timestamp=iso_hours_ago(C.OVERRIDE_CATALYST_MAX_AGE_HOURS + 5))
    assert "OVERRIDE_STALE_CATALYST" in codes(do(base, override({"QQQ": 100.0}, stale), state))
    fut = good_override(catalyst_timestamp=iso_hours_ago(-5))
    assert "OVERRIDE_FUTURE_CATALYST" in codes(do(base, override({"QQQ": 100.0}, fut), state))
    miss = good_override()
    miss.pop("catalyst_url")
    out = do(base, override({"QQQ": 100.0}, miss), state)
    assert {"OVERRIDE_INCOMPLETE", "OVERRIDE_NO_URL"} <= codes(out)
    for o in (stale, fut, miss):
        assert do(base, override({"QQQ": 100.0}, o), state)["final_plan"]["orders"] == base["orders"]


def test_override_execution_below_floor_at_this_account_size(base, state, urls_ok):
    """A 15pp de-risk is legal on paper but no whole share gets bought, so realised
    equity drops ~16pp below what the baseline realises -> FAIL (documented)."""
    out = do(base, override({"QQQ": 85.0}), state)
    assert out["verdict"] == "FAIL"
    assert "OVERRIDE_EXECUTION_BELOW_FLOOR" in codes(out)
    assert out["final_plan"]["orders"] == base["orders"]


def test_legal_override_passes_with_integer_orders(base, state, urls_ok):
    # v5.1.2 (N3): {"QQQ": 100} on the live book derives exactly the baseline's orders:
    # not an override -> FAIL OVERRIDE_NO_EFFECT, the baseline executes unmodified
    out = do(base, override({"QQQ": 100.0}), state)
    assert out["verdict"] == "FAIL" and "OVERRIDE_NO_EFFECT" in codes(out)
    assert out["final_plan"]["orders"] == base["orders"]
    b = make_baseline(prices=dict(LIVE_PX), pf=portfolio(2000.0, QQQ=10))
    out = do(b, override({"QQQ": 90.0}), state)
    assert out["verdict"] == "PASS", out["violations"]
    fp = out["final_plan"]
    assert fp["source"] == "OVERRIDE_VALIDATED"
    assert [(o["ticker"], o["action"], o["shares"]) for o in fp["orders"]] == \
        [("QQQ", "BUY", 1.0), ("QQQM", "BUY", 1.0)]
    assert all(float(o["shares"]).is_integer() and o["pre_approved"] is False for o in fp["orders"])
    assert fp["execution"]["post_trade"]["equity_pct"] == pytest.approx(89.8, abs=0.1)


def test_defensive_override_must_re_risk(state, urls_ok):
    b = make_baseline(regime="DEFENSIVE", pf=portfolio(737.93, QQQ=1))
    out = do(b, override({"QQQ": 40.0}), state)
    assert out["verdict"] == "FAIL" and "OVERRIDE_FORBIDDEN" in codes(out)


def test_override_on_tampered_ledger_is_suspended(base, state, urls_ok):
    from helpers import build_ledger, mark
    recs = state / "recommendations.jsonl"
    build_ledger(recs, [mark("2026-01-02", 100.0, 100.0), mark("2026-01-03", 100.0, 100.0)])
    lines = recs.read_text().splitlines()
    lines[0] = lines[0].replace('"total_usd": 100.0', '"total_usd": 999.0')
    recs.write_text("\n".join(lines) + "\n")
    out = do(base, override({"QQQ": 100.0}), state)
    assert out["verdict"] == "FAIL" and "LEDGER_TAMPER_DETECTED" in codes(out)
    assert out["ledger_tamper"] is True


# ----------------------------------------------- real-money satellite switch
def test_satellite_buy_not_graduated(base, state, urls_ok):
    sat = [{"ticker": "NVDA", "action": "BUY", "size_pct": 5, "catalyst_url": "https://x.y",
            "catalyst_timestamp": iso_hours_ago(2)}]
    out = do(base, override({"QQQ": 95.0, "NVDA": 5.0}, satellite=sat), state)
    assert out["verdict"] == "FAIL" and "SATELLITE_NOT_GRADUATED" in codes(out)
    out = do(base, confirm({"QQQ": 100.0}, satellite=sat), state)
    assert out["verdict"] == "FAIL" and "SATELLITE_NOT_GRADUATED" in codes(out)
    out = do(base, confirm({"QQQ": 90.0, "NVDA": 10.0}), state)
    assert "SATELLITE_NOT_GRADUATED" in codes(out)


def test_script_levels_gate_when_switch_on(base, state, urls_ok, monkeypatch):
    """With the switch flipped (a human edit), R/R, stop and target are the script's;
    an LLM-supplied number that differs is LLM_INVENTED_NUMBER."""
    monkeypatch.setattr(C, "SATELLITE_REAL_MONEY_ENABLED", True)
    lv = base["satellite"]["levels"]["NVDA"]
    good = {"ticker": "NVDA", "action": "BUY", "size_pct": 5, "catalyst_url": "https://x.y",
            "catalyst_timestamp": iso_hours_ago(2)}
    out = do(base, override({"QQQ": 95.0, "NVDA": 5.0}, satellite=[dict(good)]), state)
    assert "LLM_INVENTED_NUMBER" not in codes(out) and "SATELLITE_NO_SCRIPT_LEVELS" not in codes(out)
    same = dict(good, rr=lv["rr"], stop=lv["stop"], target=lv["target"])
    assert "LLM_INVENTED_NUMBER" not in codes(do(base, override({"QQQ": 95.0, "NVDA": 5.0},
                                                               satellite=[same]), state))
    for k, bad in (("rr", 5.0), ("stop", lv["stop"] - 3), ("target", lv["target"] * 2)):
        out = do(base, override({"QQQ": 95.0, "NVDA": 5.0}, satellite=[dict(good, **{k: bad})]), state)
        assert out["verdict"] == "FAIL" and "LLM_INVENTED_NUMBER" in codes(out), k
    nolv = copy.deepcopy(base)
    nolv["satellite"]["levels"].pop("NVDA")
    out = do(nolv, override({"QQQ": 95.0, "NVDA": 5.0}, satellite=[dict(good)]), state)
    assert "SATELLITE_NO_SCRIPT_LEVELS" in codes(out)


# ---------------------------------------------------- malformed proposals
@pytest.mark.parametrize("prop", [[], "hello", 123, None])
def test_non_dict_proposal_fails_closed(base, state, prop):
    out = V.validate(copy.deepcopy(base), prop, str(state / "recommendations.jsonl"),
                     str(state / "last_run.json"))
    assert out["verdict"] == "FAIL"
    assert out["final_plan"]["orders"] == base["orders"]
    assert out["final_plan"]["shadow_picks"] == []


def test_cli_unparseable_proposal(state, tmp_path, base):
    bp = write_json(tmp_path / "b.json", base)
    pp = tmp_path / "p.json"
    pp.write_text("{not json")
    r = run("validate.py", "--baseline", bp, "--proposal", pp, "--recs",
            state / "recommendations.jsonl", "--last-run", state / "last_run.json", state=state)
    assert r.returncode == 1, r.stderr
    out = json.loads(r.stdout)
    assert out["verdict"] == "FAIL" and out["final_plan"]["orders"] == base["orders"]
    assert out["final_plan"]["shadow_picks"] == []


def test_cli_confirm_pass(state, tmp_path, base):
    bp = write_json(tmp_path / "b.json", base)
    pp = write_json(tmp_path / "p.json", confirm({"QQQ": 100.0}))
    r = run("validate.py", "--baseline", bp, "--proposal", pp, state=state)
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(r.stdout)["final_plan"]["orders"] == base["orders"]
