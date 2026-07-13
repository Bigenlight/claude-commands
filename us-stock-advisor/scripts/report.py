#!/usr/bin/env python3
"""us-stock-advisor v5 — Phase 4 logging + report header (DETERMINISTIC numbers).

Install path: ~/.claude/skills/us-stock-advisor/scripts/report.py

Does two things, both of which the LLM is forbidden from doing itself:
  1. append_run(): writes EXACTLY ONE portfolio_mark line per run to
     recommendations.jsonl — including no-ops — plus one line per order/override/
     satellite trade/veto. No run escapes the log.
  2. header(): computes the Korean report's mandatory top block (누적 vs QQQ,
     LLM-layer spread vs the mechanical baseline, override P&L). The LLM writes
     prose AROUND these numbers and may not restate them from memory.

  3. last_run(): writes state/last_run.json — STRUCTURED JSON ONLY. This is the
     entire continuity payload for the next run. Prior report PROSE is never fed
     back (that archive-as-prompt loop carried abolished v4.0 rules forward for
     7 runs and made the model invent cap violations that no longer existed).

Usage:
  python3 report.py --log --baseline baseline_plan.json --final final_plan.json
  python3 report.py --header
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C            # noqa: E402
import score_recs as S        # noqa: E402


def _mark_baseline_nav(baseline, prev):
    """Curve 2: the un-overridden mechanical baseline, compounded on the core
    ticker's price between runs at the baseline's own target weights. Without a
    producer here, `mechanical_baseline_cum_pct` is null forever, the LLM-layer
    spread is null forever, and the kill trigger can never fire — which is the
    state the draft shipped in."""
    # the price MUST be the CORE ticker's. orders[0] is frequently the satellite
    # trim SELL, and compounding the mechanical baseline on NVDA's price would make
    # curve 2 — the curve the kill trigger reads — measure the wrong asset.
    px = next((d["usd"] / d["shares"] for t, d in
               baseline["portfolio"]["positions"].items()
               if t == C.CORE_TICKER and d["shares"]), None)
    px = px or next((o["limit_ref_price"] for o in baseline["orders"]
                     if o["ticker"] == C.CORE_TICKER), None)
    if px is None:
        return prev.get("baseline_cum_return_pct")
    b = prev.get("baseline_nav") or {"start_px": px, "cum": 0.0, "eq": None}
    if b.get("eq") is not None and b.get("last_px"):
        b["cum"] = (1 + b["cum"] / 100) * (1 + b["eq"] / 100 * (px / b["last_px"] - 1)) * 100 - 100
    b["last_px"] = px
    b["eq"] = baseline["targets"]["equity_target_pct"]
    prev["baseline_nav"] = b
    prev["baseline_cum_return_pct"] = round(b["cum"], 2)
    return round(b["cum"], 2)


def append_run(baseline, final, deposit_usd=0.0, prices_csv=None):
    # accept either validate.py's full output or a bare final plan
    if "final_plan" in final:
        final = final["final_plan"]
    if not final.get("orders"):
        final["orders"] = baseline["orders"]     # CONFIRM_BASELINE == the baseline orders
    today = datetime.now(timezone.utc).date().isoformat()
    regime = baseline["regime"]["regime"]
    p = baseline["portfolio"]
    n = 0

    # prior structured state is read FIRST: the shadow-NAV marker mutates it and it
    # is re-persisted at the bottom of this function.
    os.makedirs(C.STATE_DIR, exist_ok=True)
    prev = {}
    if os.path.exists(C.LAST_RUN_JSON):
        try:
            prev = json.load(open(C.LAST_RUN_JSON))
        except Exception:
            prev = {}

    S.append_rec(C.RECS_JSONL, {
        "type": "portfolio_mark", "date": today, "regime": regime,
        "total_usd": p["total_usd"], "cash_usd": p["cash_usd"], "cash_pct": p["cash_pct"],
        "positions": p["positions"], "deposit_usd": deposit_usd,
        "baseline_cum_return_pct": _mark_baseline_nav(baseline, prev),
        "ticker": None, "action": "MARK",
    })
    n += 1

    orders = final.get("orders", []) or []
    if not orders:
        # A no-op is a decision and it gets logged like one. "Nothing happened"
        # was 84% of v4's runs and none of it was ever scored.
        S.append_rec(C.RECS_JSONL, {
            "type": "noop", "date": today, "regime": regime,
            "ticker": C.CORE_TICKER, "action": "NO_OP",
            "reason": "within drift band; baseline confirmed",
        })
        n += 1
    for o in orders:
        S.append_rec(C.RECS_JSONL, {
            "type": "order", "date": today, "regime": regime,
            "ticker": o["ticker"], "action": o["action"], "usd": o.get("usd"),
            "shares": o.get("shares"), "price": o.get("limit_ref_price"),
            "stop": o.get("stop"), "source": final.get("source", "BASELINE"),
        })
        n += 1

    ovr = final.get("override")
    if ovr:
        S.append_rec(C.RECS_JSONL, {
            "type": "override", "date": today, "regime": regime,
            "ticker": ovr.get("ticker", C.CORE_TICKER), "action": "OVERRIDE",
            "direction": ovr.get("direction"),
            "catalyst_url": ovr.get("catalyst_url"),
            "catalyst_timestamp": ovr.get("catalyst_timestamp"),
            "expected_cost_if_wrong_pct": ovr.get("expected_cost_if_wrong_pct"),
            "qqq_forward_20d_if_i_am_wrong": ovr.get("qqq_forward_20d_if_i_am_wrong"),
            "expires_after_trading_days": C.OVERRIDE_EXPIRY_TRADING_DAYS,
            "spread_vs_baseline_pp": None,   # filled by score_recs.py at +5/+20d
        })
        n += 1

    for v in final.get("vetoes", []) or []:
        S.append_rec(C.RECS_JSONL, {
            "type": "veto", "date": today, "regime": regime,
            "ticker": v.get("ticker"), "action": "VETO",
            "event": v.get("event"), "url": v.get("url"), "event_date": v.get("event_date"),
        })
        n += 1

    # structured continuity only
    flip_month = (baseline["regime"].get("eval_month")
                  if prev.get("regime") not in (None, regime)
                  else prev.get("regime_flip_month"))
    # EXTERNAL ledger anchor (Break A fail-safe): record the tip hash + line count of
    # recommendations.jsonl AFTER all of this run's appends, in a SEPARATE file. A
    # Phase-2 LLM that later deletes/truncates/re-links the ledger to clear its own
    # suspension cannot also silently match this anchor without editing last_run.json
    # too. This dump is the last write of the run, so tip_and_len sees every append.
    tip, ln = S.tip_and_len(C.RECS_JSONL)
    with open(C.LAST_RUN_JSON, "w") as f:
        json.dump({
            "date": today,
            "regime": regime,
            "regime_flip_month": flip_month,
            "positions": p["positions"],
            "cash_usd": p["cash_usd"],
            "targets": baseline["targets"],
            "active_override": ovr or None,
            "satellite_names": baseline.get("satellite", {}).get("names", []),
            # the ATR stops MUST persist: core.py --weekly compares today's price to
            # the PRIOR run's stop. An unpersisted stop can never be breached, which
            # would make the only stop in the system decorative.
            "satellite_stops_atr": baseline.get("satellite", {}).get("stops_atr", {}),
            "baseline_nav": prev.get("baseline_nav"),
            "baseline_cum_return_pct": prev.get("baseline_cum_return_pct"),
            "ledger_tip_hash": tip,
            "ledger_len": ln,
        }, f, indent=2)
    return n


def halt(reason, stage="phase0"):
    """E11: a halt is never silent. A yfinance flake on the month's ONLY run must
    not become an unlogged zero-entry month — 'nothing happened' being invisible
    was 84% of v4's runs."""
    S.append_rec(C.RECS_JSONL, {
        "type": "pipeline_halt", "date": datetime.now(timezone.utc).date().isoformat(),
        "stage": stage, "reason": reason, "ticker": None, "action": "HALT",
    })
    sys.stderr.write(f"pipeline_halt logged: {stage}: {reason}\n")
    return 1


def header(prices_csv=None):
    cum = S.cumulative(C.RECS_JSONL, prices_csv) or {}
    tr = S.track_record(C.RECS_JSONL, C.SCORECARD_CSV, prices_csv) if os.path.exists(C.RECS_JSONL) else {}
    return {
        "누적_수익률_pct": cum.get("actual_cum_pct"),
        "QQQ_누적_pct": cum.get("qqq_cum_pct"),
        "vs_QQQ_pp": cum.get("vs_qqq_pp"),
        "기계적_베이스라인_누적_pct": cum.get("mechanical_baseline_cum_pct"),
        "LLM_레이어_기여_pp": cum.get("llm_layer_spread_pp"),
        "적중률_vs_QQQ": tr.get("hit_rate_vs_qqq"),
        "다트판_기준선": C.DARTBOARD_BASE_RATE,
        "킬_트리거_발동": cum.get("kill_trigger_armed"),
        "오버라이드_권한_정지": cum.get("override_privileges_at_risk"),
        "보드_재소집": cum.get("board_reconvene_armed"),
        "새틀라이트_확대_가능": tr.get("expand_satellite_ok"),
        "원장_변조_감지": cum.get("ledger_tamper_detected", not S.ledger_intact(C.RECS_JSONL)),
        "양도세_공제한도_KRW": C.CGT_ALLOWANCE_KRW,
        "기준일": cum.get("since"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", action="store_true")
    ap.add_argument("--header", action="store_true")
    ap.add_argument("--halt", help="log a pipeline_halt line with this reason (E11)")
    ap.add_argument("--stage", default="phase0")
    ap.add_argument("--baseline")
    ap.add_argument("--final")
    ap.add_argument("--deposit", type=float, default=0.0)
    ap.add_argument("--prices-csv")
    a = ap.parse_args()
    if a.halt:
        halt(a.halt, a.stage)
        return
    if a.log:
        if not (a.baseline and a.final):
            sys.stderr.write("ERROR: --log needs --baseline and --final\n")
            sys.exit(2)
        n = append_run(json.load(open(a.baseline)), json.load(open(a.final)),
                       a.deposit, a.prices_csv)
        sys.stderr.write(f"appended {n} lines -> {C.RECS_JSONL}\n")
    if a.header:
        print(json.dumps(header(a.prices_csv), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
