"""SKILL.md acceptance tests 1, 3, 7, 8, 9 and the whole-share end-to-end dry run
(acceptance 10) on the real portfolio numbers, all in a scratch state dir."""
from __future__ import annotations

import ast
import json
import os
import re

import pytest

import config as C
import score_recs as S
from helpers import (SCRIPTS, SK, iso_hours_ago, make_prices_csv, portfolio, read_bytes, run,
                     write_json)

SKILL_MD = os.path.join(SK, "SKILL.md")


def skill_text():
    with open(SKILL_MD, encoding="utf-8") as f:
        return f.read()


def config_symbols():
    tree = ast.parse(open(os.path.join(SCRIPTS, "config.py")).read())
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id.isupper():
                    try:
                        out[t.id] = ast.literal_eval(node.value)
                    except Exception:
                        out[t.id] = None
    return out


# --------------------------------------------------------------- acceptance 8
def test_acceptance_8_every_config_symbol_is_read_by_a_script():
    srcs = ""
    for fn in os.listdir(SCRIPTS):
        if fn.endswith(".py") and fn != "config.py":
            srcs += open(os.path.join(SCRIPTS, fn)).read()
    exempt = {"SKILL_DIR"}          # used only inside config.py to build STATE_DIR
    unread = [s for s in config_symbols() if s not in exempt
              and not re.search(rf"\bC\.{s}\b", srcs)]
    assert not unread, f"config symbols no script reads (v4 double bookkeeping): {unread}"


def test_skill_md_config_citations_resolve():
    """Every `config.X` SKILL.md cites must exist (a dangling citation is the same
    double-bookkeeping bug from the other side)."""
    syms = set(config_symbols())
    cited = set(re.findall(r"config\.([A-Z][A-Z0-9_]+)", skill_text()))
    cited = {c for c in cited if not c.endswith("_")}             # e.g. `config.BACKTEST_*`
    assert cited - syms == set(), f"SKILL.md cites unknown config symbols: {cited - syms}"


# --------------------------------------------------------------- acceptance 1
def test_acceptance_1_no_general_purpose_invocation():
    """The grep's only allowed hit is the DELETE-list entry (intended prose)."""
    text = skill_text()
    deleted = text.split("## What v5 DELETED", 1)[1].split("\n## ", 1)[0]
    hits = [ln for ln in text.splitlines()
            if re.search(r'subagent_type\s*[=:]\s*"?general-purpose', ln)
            and ln not in deleted.splitlines()]
    assert hits == []
    assert "Never `general-purpose`" in text                  # the instruction survives


# --------------------------------------------------------------- acceptance 7
ALLOWED_COMPARATOR_LINES = (          # historical / schema prose, not gates
    "<= 6 sentences", "<= 2 sentences", ">60%", "data_age_hours >= 0", "R/R ≥ 2.0 hard gate",
)


def test_acceptance_7_no_gating_threshold_values():
    text = skill_text()
    bad = [ln for ln in text.splitlines()
           if re.search(r"(?<![A-Z_])(>=|<=|≥|≤|>|<|=)\s*[0-9]", ln)   # not ENV_VAR=1
           and not any(a in ln for a in ALLOWED_COMPARATOR_LINES)]
    assert bad == [], bad
    # no config symbol is ever written next to its own value
    for sym, val in config_symbols().items():
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            continue
        forms = {repr(val), f"{val:g}"}
        if sym.endswith("_PCT") and abs(val) < 1:
            forms |= {f"{val * 100:g}%", f"{val * 100:g}pp"}
        for f_ in forms:
            pat = rf"{sym}`?\s*[=(:]\s*{re.escape(f_)}(?![0-9])"
            assert not re.search(pat, text), (sym, f_)


def test_action_block_has_no_fractional_shares():
    assert not re.search(r"\d+\.\d+\s*주", skill_text())


# --------------------------------------------------------------- acceptance 9
def test_acceptance_9_slack_delivery_is_specified():
    lines = [ln for ln in skill_text().splitlines() if "slack" in ln.lower()]
    joined = "\n".join(lines)
    assert "U0AD7V4SWD9" in joined
    assert re.search(r"send-message tool|slack_send_message", joined, re.I)
    assert re.search(r"authoriz", joined, re.I)


# --------------------------------------------------------------- acceptance 3
@pytest.mark.parametrize("key", ["cash", "USD", "SGOV"])
def test_acceptance_3_cash_rejected_cli(state, tmp_path, key):
    csv = make_prices_csv(tmp_path / "px.csv")
    pf = write_json(tmp_path / "pf.json", portfolio(392.25, QQQ=2))
    bp = tmp_path / "b.json"
    assert run("core.py", "--prices-csv", csv, "--portfolio", pf, "--out", bp,
               state=state).returncode == 0
    pp = write_json(tmp_path / "p.json", {"decision": "CONFIRM_BASELINE",
                                          "final_allocation": {"QQQ": 90.0, key: 10.0}})
    r = run("validate.py", "--baseline", bp, "--proposal", pp, state=state)
    out = json.loads(r.stdout)
    assert r.returncode == 1 and out["verdict"] == "FAIL"
    assert out["final_plan"]["orders"] == json.load(open(bp))["orders"]


# ------------------------------------------------ acceptance 10 (end to end)
def test_end_to_end_whole_share_dry_run(state, tmp_path):
    """core -> CONFIRM (+ shadow pick) -> validate PASS -> report --log -> --header, on
    the real book. The executable order is an integer: BUY QQQM 1."""
    csv = make_prices_csv(tmp_path / "px.csv", extra={"AMD": 150.0, "NVDA": 180.0})
    pf = write_json(tmp_path / "pf.json", portfolio(392.25, QQQ=2))
    bp = state / "baseline_plan.json"
    r = run("core.py", "--prices-csv", csv, "--portfolio", pf, "--out", bp, state=state)
    assert r.returncode == 0, r.stderr
    b = json.load(open(bp))
    prop = {"decision": "CONFIRM_BASELINE", "final_allocation": {"QQQ": 100.0},
            "satellite": [], "override": None, "vetoes": [], "rationale_en": "Confirm.",
            "shadow_picks": [{"ticker": "AMD", "direction": "LONG", "thesis_en": "Dated event.",
                              "catalyst_url": "https://example.com/amd",
                              "catalyst_timestamp": iso_hours_ago(3)}]}
    pp = write_json(state / "proposal.json", prop)
    r = run("validate.py", "--baseline", bp, "--proposal", pp, state=state)
    assert r.returncode == 0, r.stdout
    out = json.loads(r.stdout)
    fp = write_json(state / "final_plan.json", out)
    orders = out["final_plan"]["orders"]
    assert [(o["ticker"], o["action"], o["shares"]) for o in orders] == [("QQQM", "BUY", 1.0)]
    assert orders[0]["usd"] * (1 + C.WHOLE_SHARE_PRICE_BUFFER_PCT) <= b["portfolio"]["cash_usd"]
    assert [p["ticker"] for p in out["final_plan"]["shadow_picks"]] == ["AMD"]
    r = run("report.py", "--log", "--baseline", bp, "--final", fp, state=state)
    assert r.returncode == 0, r.stderr
    types = [x["type"] for x in S.load_recs(str(state / "recommendations.jsonl"))]
    assert types == ["portfolio_mark", "order", "shadow_pick"]
    before = read_bytes(state / "recommendations.jsonl")
    r = run("report.py", "--header", "--prices-csv", csv, state=state)
    assert r.returncode == 0, r.stderr
    h = json.loads(r.stdout)
    assert h["섀도우_표본수"] == "0/1" and h["실전_승격_가능"] is False
    assert h["불가피_잔여현금_USD"] == pytest.approx(88.42, abs=0.01)
    assert read_bytes(state / "recommendations.jsonl").startswith(before)
