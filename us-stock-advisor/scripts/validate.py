#!/usr/bin/env python3
"""us-stock-advisor v5 — Phase 3 DETERMINISTIC VALIDATOR.

Install path: ~/.claude/skills/us-stock-advisor/scripts/validate.py

Replaces v4's Phase 3 + Phase 4 LLM judges (67% PASS_WITH_WARNINGS, 2.2% FAIL —
theater; and the one FAIL fired *against* the merged fix). An LLM judge can be
test-retest reliable AND position-biased at the same time: consistently wrong is
not the same as right. So the gate is code.

Contract: FAIL is not a veto that produces cash. FAIL means
    THE BASELINE PLAN EXECUTES UNMODIFIED.
The LLM cannot argue with this file.

Usage:
  python3 validate.py --baseline baseline_plan.json --proposal phase2.json \
      [--recs ~/.claude/skills/us-stock-advisor/state/recommendations.jsonl]
  -> stdout: {"verdict":"PASS"|"FAIL","violations":[...],"final_plan":{...}}
  exit 0 = proposal accepted · exit 1 = FAIL, baseline enforced (still exit-0-safe
  for the pipeline; the caller reads verdict, not just the code) · 2 = bad input
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C          # noqa: E402
import core as CORE         # noqa: E402  (order derivation + allocation normalizer)
import score_recs as S      # noqa: E402  (ledger integrity chain)

REQUIRED_OVERRIDE_FIELDS = [
    "catalyst_description", "catalyst_url", "catalyst_timestamp",
    "expected_cost_if_wrong_pct", "qqq_forward_20d_if_i_am_wrong",
]


def _parse_ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def _aware(dt):
    """Naive ISO dates ('2026-07-13') parse tz-naive; comparing them to an aware
    utcnow() is a TypeError. Every timestamp entering a comparison goes through here."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _url_resolves(url):
    """Resolvability only. CONTENT verification ('the page contains the claim') is
    an LLM job (Phase 1/2 have WebFetch); a deterministic gate doing NLP in Python
    is scope creep. This gate answers exactly one question: does the URL exist?"""
    import urllib.request
    try:
        req = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": "us-stock-advisor/5"})
        with urllib.request.urlopen(req, timeout=C.URL_CHECK_TIMEOUT_S) as r:
            return 200 <= r.status < 400
    except Exception:
        return False


def _load_recs(recs_path):
    rows = []
    if not os.path.exists(recs_path):
        return rows
    with open(recs_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


def _satellite_history(recs_path):
    """Entry dates and 12-month roundtrip counts per satellite name, read from the
    decision log. This is what makes SATELLITE_MIN_HOLD_DAYS and
    SATELLITE_MAX_ROUNDTRIPS_PER_YEAR enforceable instead of decorative."""
    rows = _load_recs(recs_path)
    now = datetime.now(timezone.utc)
    last_buy, roundtrips = {}, {}
    for r in rows:
        if r.get("type") != "order":
            continue
        t = str(r.get("ticker", "")).upper()
        if t == C.CORE_TICKER or t in C.BROAD_ETFS:
            continue
        d = _aware(_parse_ts(r.get("date")))
        if d is None:
            continue
        if r.get("action") == "BUY":
            last_buy[t] = d
        elif r.get("action") == "SELL":
            if (now - d).days <= 365:
                roundtrips[t] = roundtrips.get(t, 0) + 1
    return last_buy, roundtrips


def override_suspended(recs_path):
    """Escalating cost, §6.5: after N consecutive overrides with negative spread
    vs the un-overridden baseline, override privileges are suspended for a
    quarter. A COUNTER, not a prompt."""
    if not os.path.exists(recs_path):
        return None
    rows = _load_recs(recs_path)
    ovr = [r for r in rows if r.get("type") == "override" and r.get("spread_vs_baseline_pp") is not None]
    ovr = ovr[-C.OVERRIDE_SUSPEND_AFTER_N_BAD:]
    if len(ovr) == C.OVERRIDE_SUSPEND_AFTER_N_BAD and all(
            r["spread_vs_baseline_pp"] < 0 for r in ovr):
        last = _aware(_parse_ts(ovr[-1].get("date"))) or datetime.now(timezone.utc)
        until = last + timedelta(days=C.OVERRIDE_SUSPENSION_DAYS)
        if until > datetime.now(timezone.utc):
            return until.isoformat()
    return None


def _baseline_enforced(baseline, violations, ledger_tamper):
    """The one FAIL shape: the pre-approved baseline executes unmodified. Used by
    both the normal FAIL path and the fail-closed guards below, so no input can
    produce zero-bytes-on-stdout with a FAIL exit code (Break B)."""
    return {"verdict": "FAIL", "violations": violations,
            "final_plan": {"source": "BASELINE_ENFORCED",
                           "reason": "validator FAIL — the pre-approved baseline executes unmodified",
                           "orders": baseline.get("orders", []),
                           "targets": baseline.get("targets", {})},
            "override_active": False, "ledger_tamper": ledger_tamper,
            "override_expires_after_trading_days": C.OVERRIDE_EXPIRY_TRADING_DAYS}


def validate(baseline, proposal, recs_path, last_run_path=None):
    # Break A: the anchored integrity check. Unknown/missing anchor with a non-empty
    # ledger is still internally checked; a first run (no last_run.json) with an
    # empty ledger passes. Tamper is fail-safe: it suspends overrides below.
    if last_run_path is None:
        last_run_path = C.LAST_RUN_JSON
    ledger_tamper = not S.ledger_intact(recs_path, last_run_path)

    # Break B: a non-object proposal ([], "hello", 123, null) must not crash the
    # gate. Fail closed to the baseline with a real verdict JSON, never a traceback.
    if not isinstance(proposal, dict):
        return _baseline_enforced(
            baseline,
            [f"SCHEMA_VIOLATION: proposal must be a JSON object, got "
             f"{type(proposal).__name__}"],
            ledger_tamper)

    v = []
    total = baseline["portfolio"]["total_usd"]
    tgt = baseline["targets"]
    regime = baseline["regime"]["regime"]

    # 0. data integrity — negative age = partial bar leaked = hard abort
    age = baseline.get("data", {}).get("data_age_hours")
    if age is None or age < 0:
        v.append(f"DATA_AGE_INVALID: {age} (negative age = partial bar; hard abort)")
    elif age > C.MAX_DATA_AGE_HOURS:
        v.append(f"DATA_STALE: {age}h > {C.MAX_DATA_AGE_HOURS}h")

    # 0b. LEDGER INTEGRITY (Break 6 / Break A). `ledger_tamper` was computed above
    #     from the anchored check; an OVERRIDE against a tampered ledger is suspended
    #     fail-safe below, and the header flags it.

    # 1. THERE IS NO CASH FIELD. If the LLM invented one, that alone is a FAIL.
    if "cash_pct" in proposal or "cash" in proposal or "cash_usd" in proposal:
        v.append("SCHEMA_VIOLATION: proposal contains a cash allocation field. "
                 "Cash is not a choosable allocation in v5.")

    # 1a. DECISION must be one of the two legal verbs (BREAK 3a). A missing decision
    #     or a novel one ("LIQUIDATE_ALL") otherwise skips the override protocol —
    #     including the suspension counter — entirely.
    decision = proposal.get("decision")
    if decision not in ("CONFIRM_BASELINE", "OVERRIDE"):
        v.append(f"SCHEMA_VIOLATION: decision must be CONFIRM_BASELINE or OVERRIDE, "
                 f"got {decision!r}")

    # 1b. ALLOCATION is parsed exactly once, safely (BREAK 4): non-numeric weights
    #     become listed SCHEMA_VIOLATIONs (not a raw crash), and case-variant keys
    #     ({"QQQ","qqq","Qqq"}) collapse to one canonical ticker (not three orders).
    raw_alloc = proposal.get("final_allocation", {})
    if not raw_alloc:
        v.append("SCHEMA_VIOLATION: final_allocation is required. A proposal that "
                 "omits it cannot be gated and therefore cannot be executed.")
    alloc, alloc_errs = CORE.normalize_alloc(raw_alloc)
    for e in alloc_errs:
        v.append(f"SCHEMA_VIOLATION: {e}")

    _CASHY = {"CASH", "USD", "KRW", "MMF"}
    _LEGAL = {C.CORE_TICKER} | set(C.BROAD_ETFS) | set(C.SATELLITE_UNIVERSE)
    priceable = ({C.CORE_TICKER}
                 | {str(t).upper() for t in baseline["portfolio"]["positions"]}
                 | {str(o["ticker"]).upper() for o in baseline.get("orders", [])})
    for tk, w in alloc.items():
        if tk in _CASHY:
            v.append(f"SCHEMA_VIOLATION: '{tk}' in final_allocation. Cash is a residual, not a decision.")
            continue
        if tk not in _LEGAL:
            v.append(f"TICKER_NOT_IN_UNIVERSE: {tk} in final_allocation "
                     f"(cash-proxy or off-universe tickers are not allocatable)")
        # BREAK 1: a ticker the plan cannot price would have its BUY silently dropped
        # by orders_from_allocation and its target would leak straight to cash.
        if abs(w) > 1e-9 and tk not in priceable:
            v.append(f"UNPRICEABLE_IN_ALLOCATION: {tk} is neither the core, a held "
                     "position, nor otherwise priced by Phase 0; its target would "
                     "silently execute as cash")
        # BREAK 2: no shorts, and no single line item larger than the whole book.
        if w < -1e-6:
            v.append(f"NEGATIVE_WEIGHT: {tk} {w:.1f}% (weights are long-only; a "
                     "negative weight is a naked short)")
        if w > 100.0 + 1e-6:
            v.append(f"WEIGHT_EXCEEDS_BOOK: {tk} {w:.1f}% > 100% of the portfolio")

    # BREAK 2: the book cannot exceed 100% (leverage). It MAY be under 100% — the
    # shortfall is the cash residual (the whole design). So this is a ceiling, not
    # an equality: '{"QQQ":140,"NVDA":-40}' sums to 100 yet is caught by the short
    # leg above, and '{"QQQ":140}' is caught here and by the equity ceiling.
    if alloc:
        wsum = sum(alloc.values())
        if wsum > 100.0 + C.ALLOCATION_SUM_TOLERANCE_PP:
            v.append(f"ALLOCATION_SUM_OVER_100: weights sum to {wsum:.1f}% > 100% "
                     "(leverage; the book cannot exceed itself)")

    equity_pct = (sum(w for tk, w in alloc.items() if tk not in _CASHY)
                  if alloc else None)
    sat_alloc_pct = sum(w for tk, w in alloc.items()
                        if tk not in C.BROAD_ETFS and tk not in _CASHY and tk != C.CORE_TICKER)
    if sat_alloc_pct > C.SATELLITE_MAX_PCT * 100 + 1e-6:
        v.append(f"SATELLITE_OVER_BUDGET_IN_ALLOCATION: {sat_alloc_pct:.1f}% non-core equity "
                 f"> {C.SATELLITE_MAX_PCT*100:.0f}% (declared satellite[] list does not define "
                 f"exposure; the allocation does)")

    ovr = proposal.get("override")
    allowance = 0.0

    # 1c. The LLM may not author orders at all — validate.py derives them.
    if decision == "CONFIRM_BASELINE" and ovr:
        v.append("SCHEMA_VIOLATION: decision=CONFIRM_BASELINE cannot carry an override")
    if proposal.get("orders"):
        v.append("SCHEMA_VIOLATION: proposals may not contain orders; validate.py derives them")

    # 2. override protocol. The suspension counter (and the tamper fail-safe) run on
    #    ANY deviation from baseline — i.e. any OVERRIDE decision — not only when an
    #    `override` object happens to be attached (BREAK 3c). A CONFIRM_BASELINE
    #    executes the baseline orders and cannot deviate, so it is exempt.
    if decision == "OVERRIDE":
        if not ovr:
            v.append("OVERRIDE_MISSING_OBJECT: decision=OVERRIDE requires an 'override' "
                     "object so the override protocol (incl. the suspension counter) runs")
        susp = override_suspended(recs_path)
        if susp:
            v.append(f"OVERRIDE_SUSPENDED until {susp} "
                     f"({C.OVERRIDE_SUSPEND_AFTER_N_BAD} consecutive negative-spread overrides)")
        if ledger_tamper:
            v.append("LEDGER_TAMPER_DETECTED: recommendations.jsonl hash chain broken; "
                     "override privileges SUSPENDED (fail-safe)")
    if ovr:
        for f_ in REQUIRED_OVERRIDE_FIELDS:
            if ovr.get(f_) is None:   # BREAK 5: 0 is a legitimate value, not "missing"
                v.append(f"OVERRIDE_INCOMPLETE: missing '{f_}'")
        url = str(ovr.get("catalyst_url", ""))
        if not url.startswith("http"):
            v.append("OVERRIDE_NO_URL: catalyst_url must be a resolvable http(s) URL")
        elif not _url_resolves(url):
            v.append(f"OVERRIDE_URL_UNREACHABLE: {url} did not resolve "
                     f"(HEAD, {C.URL_CHECK_TIMEOUT_S}s)")
        ts = _aware(_parse_ts(ovr.get("catalyst_timestamp")))
        if ts is None:
            v.append("OVERRIDE_BAD_TIMESTAMP: not ISO-8601")
        else:
            hrs = (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0
            if hrs < 0:
                v.append("OVERRIDE_FUTURE_CATALYST: timestamp is in the future")
            elif hrs > C.OVERRIDE_CATALYST_MAX_AGE_HOURS:
                v.append(f"OVERRIDE_STALE_CATALYST: {hrs:.1f}h > "
                         f"{C.OVERRIDE_CATALYST_MAX_AGE_HOURS}h")
        d = ovr.get("direction", "de_risk")
        if regime == "DEFENSIVE" and d != "re_risk":
            v.append("OVERRIDE_FORBIDDEN: in DEFENSIVE the override channel is "
                     "re-risking ONLY. The LLM may never deepen a de-risk.")
        if ovr.get("touches_regime"):
            v.append("OVERRIDE_FORBIDDEN: the SMA regime is not overridable.")
        if not v:
            allowance = C.OVERRIDE_MAX_EQUITY_REDUCTION_PCT * 100

    # 3. equity floor. The operational float (config.CASH_MAX_PCT) is subtracted:
    #    without it the floor is 100.0% in TREND and any real account — which always
    #    carries settlement/FX change — FAILs. The ceiling is the regime dial, except
    #    in DEFENSIVE where the override channel is re-risk-ONLY and must therefore
    #    be able to move UP (otherwise the board-mandated channel is arithmetically
    #    impossible: every re-risk trips the ceiling).
    equity_target = float(tgt["equity_target_pct"])
    floor = equity_target - C.CASH_MAX_PCT * 100 - allowance
    ceiling = equity_target
    if (regime == "DEFENSIVE" and ovr and ovr.get("direction") == "re_risk" and allowance):
        ceiling = min((C.TARGETS["TREND"][0] + C.TARGETS["TREND"][1]) * 100,
                      equity_target + allowance)
    if equity_pct is not None and equity_pct < floor - 1e-6:
        v.append(f"EQUITY_BELOW_FLOOR: {equity_pct:.1f}% < {floor:.1f}% "
                 f"(regime target {equity_target:.1f}% − float {C.CASH_MAX_PCT*100:.0f}% "
                 f"− allowance {allowance:.0f}pp)")
    if equity_pct is not None and equity_pct > ceiling + 1e-6:
        v.append(f"EQUITY_ABOVE_CEILING: {equity_pct:.1f}% > {ceiling:.1f}% "
                 f"(the LLM may not lever above the regime dial)")

    # 4. cash ceiling. There is no >60%-cash trigger and no 40–60% dead zone
    #    anymore. In TREND the ceiling is the operational float; in DEFENSIVE it
    #    is the regime's own cash-equivalent sleeve plus that float.
    if equity_pct is not None:
        implied_cash = 100.0 - equity_pct
        max_cash = (100.0 - equity_target) + C.CASH_MAX_PCT * 100 + allowance
        if implied_cash > max_cash + 1e-6:
            v.append(f"CASH_OVER_MAX: implied cash {implied_cash:.1f}% > {max_cash:.1f}%")

    # 5. satellite limits
    sat = proposal.get("satellite", []) or []
    if len(sat) > C.SATELLITE_MAX_NAMES:
        v.append(f"SATELLITE_TOO_MANY_NAMES: {len(sat)} > {C.SATELLITE_MAX_NAMES}")
    sat_pct = sum(float(s.get("size_pct", 0)) for s in sat)
    if sat_pct > C.SATELLITE_MAX_PCT * 100 + 1e-6:
        v.append(f"SATELLITE_OVER_BUDGET: {sat_pct:.1f}% > {C.SATELLITE_MAX_PCT*100:.0f}%")
    if regime == "DEFENSIVE" and sat:
        v.append("SATELLITE_IN_DEFENSIVE: satellite must be force-closed")

    d2e = baseline.get("satellite", {}).get("days_to_earnings", {})
    base_rsi = baseline.get("satellite", {}).get("rsi", {}) or {}
    breached = set(baseline.get("satellite", {}).get("stop_breaches", []) or [])
    last_buy, roundtrips = _satellite_history(recs_path)
    vetoed = {str(x.get("ticker", "")).upper() for x in (proposal.get("vetoes") or [])}
    now = datetime.now(timezone.utc)

    for s in sat:
        t = str(s.get("ticker", "")).upper()
        if t not in C.SATELLITE_UNIVERSE:
            v.append(f"TICKER_NOT_IN_UNIVERSE: {t} (the LLM may not add tickers)")
        if s.get("action") == "BUY":
            rr = s.get("rr")
            if rr is None or float(rr) < C.SATELLITE_MIN_RR:
                v.append(f"SATELLITE_RR_BELOW_MIN: {t} rr={rr} < {C.SATELLITE_MIN_RR}")
            cts = _aware(_parse_ts(s.get("catalyst_timestamp")))
            if cts is None:
                v.append(f"SATELLITE_NO_CATALYST_TS: {t}")
            else:
                h = (now - cts).total_seconds() / 3600.0
                if h < 0 or h > C.FRESH_CATALYST_MAX_AGE_HOURS:
                    v.append(f"SATELLITE_CATALYST_STALE: {t} {h:.1f}h")
            if not str(s.get("catalyst_url", "")).startswith("http"):
                v.append(f"SATELLITE_NO_CATALYST_URL: {t}")
            dd = d2e.get(t)
            if dd is not None and 0 <= dd <= C.EARNINGS_BLACKOUT_SESSIONS:
                v.append(f"EARNINGS_BLACKOUT: {t} reports in {dd}d "
                         f"(<= {C.EARNINGS_BLACKOUT_SESSIONS})")
            # RSI is a SIZING input, never a veto: above the threshold the position
            # is HALVED, not refused. ("Overbought, don't chase" cost v4 -6.67pp.)
            r_ = base_rsi.get(t)
            if r_ is not None and float(r_) > C.SATELLITE_RSI_HALVE_ABOVE:
                cap = C.SATELLITE_MAX_PCT * 100 / 2
                if float(s.get("size_pct", 0)) > cap + 1e-6:
                    v.append(f"SATELLITE_RSI_SIZE_NOT_HALVED: {t} rsi={r_} > "
                             f"{C.SATELLITE_RSI_HALVE_ABOVE}; size_pct "
                             f"{s.get('size_pct')}% > {cap:.1f}% (halve, do not refuse)")
            if roundtrips.get(t, 0) >= C.SATELLITE_MAX_ROUNDTRIPS_PER_YEAR:
                v.append(f"SATELLITE_ROUNDTRIP_LIMIT: {t} has {roundtrips[t]} exits in the "
                         f"last 12mo >= {C.SATELLITE_MAX_ROUNDTRIPS_PER_YEAR}")
        if s.get("action") == "SELL":
            # churn guard: an LLM-initiated satellite SELL before min-hold is only
            # legal if the ATR stop broke or a dated veto landed on the name.
            lb = last_buy.get(t)
            held_days = (now - lb).days if lb else None
            if (held_days is not None and held_days < C.SATELLITE_MIN_HOLD_DAYS
                    and not (t in breached or t in vetoed)):
                v.append(f"SATELLITE_MIN_HOLD_VIOLATION: {t} held {held_days}d < "
                         f"{C.SATELLITE_MIN_HOLD_DAYS}d and no stop breach / no dated veto")

    # 5a. Satellite gates bind the ALLOCATION, not just the satellite[] array
    #     (BREAK 3b). A satellite name whose weight is NEW or INCREASED vs what is
    #     already held must be declared in satellite[] as a BUY, so the RR/catalyst/
    #     earnings/roundtrip/RSI gates above actually run on it. (A hold-or-reduce of
    #     an existing name needs no re-declaration, and a CONFIRM_BASELINE executes
    #     the baseline orders, so its allocation is cosmetic and exempt.)
    declared_buys = {str(s.get("ticker", "")).upper() for s in sat
                     if s.get("action") == "BUY"}
    if decision == "OVERRIDE":
        held_pct = {tk: d.get("pct", 0.0)
                    for tk, d in baseline["portfolio"]["positions"].items()}
        for tk, w in alloc.items():
            if tk in _CASHY or tk in C.BROAD_ETFS or tk == C.CORE_TICKER or w <= 1e-6:
                continue
            if w > held_pct.get(tk, 0.0) + 1e-6:   # a new or increased satellite bet
                if tk not in declared_buys:
                    v.append(f"SATELLITE_IN_ALLOCATION_NOT_DECLARED: {tk} carries "
                             f"{w:.1f}% (> held {held_pct.get(tk, 0.0):.1f}%) in "
                             "final_allocation but is not a declared satellite BUY; it "
                             "would escape every satellite gate")

    # 5b. Phase 1 vetoes must be dated and fresh, or they do not exist.
    for x in (proposal.get("vetoes") or []):
        ed = _aware(_parse_ts(x.get("event_date")))
        if ed is None:
            v.append(f"VETO_UNDATED: {x.get('ticker')} (a veto without a dated event "
                     "is prose, not evidence)")
        elif (now - ed).days > C.VETO_MAX_AGE_DAYS:
            v.append(f"VETO_STALE: {x.get('ticker')} event {(now - ed).days}d old > "
                     f"{C.VETO_MAX_AGE_DAYS}d")

    # 6. single-name cap — SINGLE NAMES ONLY. Broad ETFs are uncapped, forever.
    for tk, w in alloc.items():
        if tk in C.BROAD_ETFS and C.BROAD_ETF_UNCAPPED:
            continue
        if w > C.SINGLE_NAME_MAX_PCT * 100 + 1e-6:
            v.append(f"SINGLE_NAME_OVER_CAP: {tk} {w:.1f}% > {C.SINGLE_NAME_MAX_PCT*100:.0f}%")

    # 7. DERIVE-AND-RE-CHECK (BREAK 1, the root fix). Gating the allocation % while
    #    trusting the derivation is the hole: derive the orders now, and if they do
    #    not reproduce the intended equity within tolerance, FAIL — a plan that is
    #    100% equity on paper and 100% cash in execution must not PASS. Derivation
    #    is wrapped so any residual coercion error is a listed FAIL, never a crash.
    derived = None
    if not v:
        try:
            derived = (baseline["orders"] if decision == "CONFIRM_BASELINE"
                       else CORE.orders_from_allocation(alloc, baseline))
        except Exception as e:
            v.append(f"SCHEMA_VIOLATION: order derivation failed ({e})")
        if derived is not None and decision != "CONFIRM_BASELINE":
            realized = CORE.realized_equity_pct(baseline, derived)
            intended = equity_pct if equity_pct is not None else 0.0
            if abs(realized - intended) > C.EXECUTION_DIVERGENCE_TOLERANCE_PP:
                v.append(f"EXECUTION_DIVERGES: derived orders realize {realized:.1f}% "
                         f"equity vs intended {intended:.1f}% "
                         f"(> {C.EXECUTION_DIVERGENCE_TOLERANCE_PP:.1f}pp); the paper "
                         "allocation does not survive execution")
                derived = None

    verdict = "PASS" if not v else "FAIL"
    if verdict == "PASS":
        # E3: orders are DERIVED, never authored.
        proposal.pop("orders", None)
        proposal["orders"] = derived
        proposal["source"] = ("BASELINE" if decision == "CONFIRM_BASELINE"
                              else "OVERRIDE_VALIDATED")
    final = proposal if verdict == "PASS" else {
        "source": "BASELINE_ENFORCED",
        "reason": "validator FAIL — the pre-approved baseline executes unmodified",
        "orders": baseline["orders"],
        "targets": baseline["targets"],
    }
    return {"verdict": verdict, "violations": v, "final_plan": final,
            "override_active": bool(ovr) and verdict == "PASS",
            "ledger_tamper": ledger_tamper,
            "override_expires_after_trading_days": C.OVERRIDE_EXPIRY_TRADING_DAYS}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--proposal", required=True)
    ap.add_argument("--recs", default=C.RECS_JSONL)
    ap.add_argument("--last-run", default=C.LAST_RUN_JSON)
    a = ap.parse_args()

    # The baseline is Phase 0's trusted output; if it cannot even be read there is
    # no approved plan and the pipeline must halt (exit 2), not fake a baseline.
    try:
        baseline = json.load(open(a.baseline))
    except Exception as e:
        print(json.dumps({"verdict": "FAIL", "violations": [f"HALT: cannot read baseline ({e})"],
                          "final_plan": {"source": "HALT", "orders": []}},
                         ensure_ascii=False))
        sys.exit(2)

    try:
        proposal = json.load(open(a.proposal))   # may be a list/str/int/None -> guarded
    except Exception as e:
        # unparseable proposal is a schema FAIL, baseline enforced
        proposal = {"__unparseable__": str(e)}

    # Belt-and-suspenders (Break B): ANY residual uncaught exception still prints a
    # baseline-enforced verdict JSON to stdout with the normal FAIL code. No path may
    # emit zero bytes while returning a FAIL exit status.
    try:
        out = validate(baseline, proposal, a.recs, a.last_run)
    except Exception as e:
        try:
            tamper = not S.ledger_intact(a.recs, a.last_run)
        except Exception:
            tamper = None
        out = _baseline_enforced(
            baseline, [f"SCHEMA_VIOLATION: uncaught {type(e).__name__}: {e}"], tamper)

    print(json.dumps(out, indent=2, ensure_ascii=False))
    sys.exit(0 if out["verdict"] == "PASS" else 1)


if __name__ == "__main__":
    main()
