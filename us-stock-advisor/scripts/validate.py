#!/usr/bin/env python3
"""us-stock-advisor v5.1 — Phase 3 DETERMINISTIC VALIDATOR.

Install path: ~/.claude/skills/us-stock-advisor/scripts/validate.py

Replaces v4's Phase 3 + Phase 4 LLM judges (67% PASS_WITH_WARNINGS, 2.2% FAIL —
theater; and the one FAIL fired *against* the merged fix). An LLM judge can be
test-retest reliable AND position-biased at the same time: consistently wrong is
not the same as right. So the gate is code.

Contract: FAIL is not a veto that produces cash. FAIL means
    THE BASELINE PLAN EXECUTES UNMODIFIED.
The LLM cannot argue with this file.

v5.1: whole shares + a two-ticker core sleeve (config.CORE_TICKERS). Weights on any
core ticker are summed into ONE core weight — the QQQ/QQQM split is ignored, so the
LLM can neither steer it nor force a conversion. Shadow (paper) satellite picks are
validated AFTER the verdict is fixed and can never touch violations/orders/allocation.

Usage:
  python3 validate.py --baseline baseline_plan.json --proposal phase2.json \
      [--recs ~/.claude/skills/us-stock-advisor/state/recommendations.jsonl]
  -> stdout: {"verdict":"PASS"|"FAIL","violations":[...],"shadow":{...},"final_plan":{...}}
  exit 0 = proposal accepted · exit 1 = FAIL, baseline enforced (still exit-0-safe
  for the pipeline; the caller reads verdict, not just the code) · 2 = HALT (baseline
  unreadable, or older than config.BASELINE_MAX_AGE_HOURS / undated: final_plan.source
  "HALT", orders [] — nothing executes; log it with report.py --halt)
  A FAILed OVERRIDE carries final_plan.rejected_override {override, proposed_allocation,
  violations} for the ledger (report.py logs `override_rejected`); orders unaffected.
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

# A shadow pick carries NO numbers. Every level (entry/stop/target/R-R) is the
# script's, read from baseline.satellite.levels (Phase 0, before the LLM picked).
SHADOW_FORBIDDEN_KEYS = {"stop", "target", "rr", "price", "entry_price", "atr",
                         "atr_14", "size_pct", "shares", "usd"}
_CASHY = {"CASH", "USD", "KRW", "MMF"}
_EPS = 1e-6      # float-comparison epsilon, not a threshold


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
    with open(recs_path, encoding="utf-8", errors="replace") as f:   # never crash on a torn byte
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
    # score_recs.satellite_history_recs keeps the orders of SUPERSEDED same-date
    # runs too (an order that may have executed still counts toward min-hold and
    # round-trips); fall back to the effective view if that helper is absent.
    hist = getattr(S, "satellite_history_recs", None)
    recs = _load_recs(recs_path)
    rows = hist(recs) if callable(hist) else S.effective_recs(recs)
    now = datetime.now(timezone.utc)
    last_buy, roundtrips = {}, {}
    for r in rows:
        if r.get("type") != "order":     # shadow_pick lines are paper, never counted
            continue
        t = str(r.get("ticker", "")).upper()
        if t in C.CORE_TICKERS or t in C.BROAD_ETFS:
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
    # spreads live in append-only override_score lines; merged_overrides joins them.
    # N7: one override per run DATE (a re-logged override, even with different
    # weights, is one decision: the effective copy, else the last superseded one).
    per_date = {}
    for r in S.merged_overrides(rows):
        if r.get("type") != "override":
            continue
        cur = per_date.get(r.get("date"))
        if cur is None or not r.get("superseded") or cur.get("superseded"):
            per_date[r.get("date")] = r
    # N3: an exactly-zero spread (no effect) is neither good nor bad — it neither
    # extends nor RESETS the consecutive-negative streak
    ovr = [r for r in per_date.values()
           if r.get("spread_vs_baseline_pp") is not None
           and float(r["spread_vs_baseline_pp"]) != 0.0]
    ovr = ovr[-C.OVERRIDE_SUSPEND_AFTER_N_BAD:]
    if len(ovr) == C.OVERRIDE_SUSPEND_AFTER_N_BAD and all(
            r["spread_vs_baseline_pp"] < 0 for r in ovr):
        last = _aware(_parse_ts(ovr[-1].get("date"))) or datetime.now(timezone.utc)
        until = last + timedelta(days=C.OVERRIDE_SUSPENSION_DAYS)
        if until > datetime.now(timezone.utc):
            return until.isoformat()
    return None


def _num(x):
    """float(x) if finite, else None. Used for script-published levels only."""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _collapse(alloc, trusted=False):
    """collapse(alloc) = {"CORE": sum over CORE_TICKERS, every other ticker}. A
    literal "CORE" key is only honoured on the trusted (baseline) side, where
    targets.target_allocation is already collapsed; on the LLM side it is an
    off-universe ticker (FAILed elsewhere) and is never merged into the core."""
    norm, _ = CORE.normalize_alloc(alloc if isinstance(alloc, dict) else {})
    literal = norm.pop("CORE", 0.0)
    out = dict(CORE.collapse_alloc(norm))
    out["CORE"] = out.get("CORE", 0.0) + (literal if trusted else 0.0)
    return out


def _alloc_matches(a, b, tol):
    """Key-by-key equality within tol; a key absent on one side counts as 0."""
    return all(abs(a.get(k, 0.0) - b.get(k, 0.0)) <= tol + _EPS for k in set(a) | set(b))


def _fmt_alloc(a):
    return "{" + ", ".join(f"{k}: {w:.1f}" for k, w in sorted(a.items()) if abs(w) > _EPS) + "}"


def _priceable_set(baseline):
    """Every ticker Phase 0 priced (baseline.prices, v5.1) unioned with the legacy
    set (core, held, pre-approved order tickers). A zero/None price is not a price."""
    out = {C.CORE_TICKER}
    for t, px in (baseline.get("prices") or {}).items():
        f = _num(px)
        if f is not None and f > 0:
            out.add(str(t).upper())
    out |= {str(t).upper() for t in baseline["portfolio"]["positions"]}
    out |= {str(o["ticker"]).upper() for o in baseline.get("orders", [])}
    return out


def _held_pct(baseline):
    return {str(tk).upper(): float(d.get("pct", 0.0) or 0.0)
            for tk, d in baseline["portfolio"]["positions"].items()}


def _baseline_realized_equity_pct(baseline):
    """What the pre-approved baseline actually realises after whole-share rounding.
    Legacy baseline (no execution block): recompute from its own orders."""
    try:
        return float(baseline["execution"]["post_trade"]["equity_pct"])
    except (KeyError, TypeError, ValueError):
        return CORE.realized_equity_pct(baseline, baseline.get("orders", []))


def _raw_shadow_tickers(proposal):
    """Tickers named in proposal.shadow_picks, read from the raw list only (no
    enrichment, no gating) so the isolation checks never depend on validate_shadow."""
    picks = proposal.get("shadow_picks")
    if not isinstance(picks, list):
        return set()
    return {str(p.get("ticker", "")).strip().upper() for p in picks
            if isinstance(p, dict) and str(p.get("ticker", "")).strip()}


def validate_shadow(proposal, baseline, recs_path):
    """Shadow (paper) satellite picks. Runs AFTER the verdict is fixed: it never
    appends to violations and never reads or writes the allocation or the orders.
    A bad pick is rejected on its own (listed), it never FAILs the proposal.
    Accepted picks are enriched ONLY from baseline.satellite.levels — the LLM
    supplies a ticker, a thesis and a dated catalyst, never a number.
    Returns (accepted, rejected)."""
    picks = proposal.get("shadow_picks")
    if picks is None:
        return [], []
    if not isinstance(picks, list):
        return [], [{"ticker": None, "reason": "SHADOW_SCHEMA: shadow_picks must be a "
                     f"list of objects, got {type(picks).__name__}"}]

    sat_block = baseline.get("satellite", {}) or {}
    levels = sat_block.get("levels") or {}
    levels = {str(k).upper(): lv for k, lv in levels.items()} if isinstance(levels, dict) else {}
    d2e = sat_block.get("days_to_earnings", {}) or {}
    regime = (baseline.get("regime", {}) or {}).get("regime")
    held = {str(t).upper() for t, d in baseline["portfolio"]["positions"].items()
            if (_num((d or {}).get("shares")) or 0.0) > 0}
    now = datetime.now(timezone.utc)

    # evidence hygiene: a ticker with a live shadow pick inside the cooldown is not
    # re-picked (overlapping windows would inflate n toward graduation)
    recent = {}
    for r in S.effective_recs(_load_recs(recs_path)):
        if r.get("type") != "shadow_pick":
            continue
        d = _aware(_parse_ts(r.get("date")))
        if d is not None:
            t = str(r.get("ticker", "")).upper()
            recent[t] = max(recent.get(t, d), d)

    accepted, rejected, taken = [], [], set()
    for p in picks:
        t = str(p.get("ticker", "")).strip().upper() if isinstance(p, dict) else ""

        def rej(code, text):
            rejected.append({"ticker": t or None, "reason": f"{code}: {text}"})

        if not isinstance(p, dict) or not t:
            rej("SHADOW_SCHEMA", "each pick must be an object with a ticker")
            continue
        bad = sorted(k for k in p if str(k).strip().lower() in SHADOW_FORBIDDEN_KEYS)
        if bad:
            rej("LLM_NUMBER_IN_SHADOW", f"{t} carries {bad}; levels are the script's, "
                "never the LLM's")
            continue
        if t not in C.SATELLITE_UNIVERSE:
            rej("TICKER_NOT_IN_UNIVERSE", f"{t} (the LLM may not add tickers)")
            continue
        if baseline.get("mode") == "weekly":
            rej("SHADOW_WEEKLY", "weekly runs are stop-check only; no new picks")
            continue
        if regime == "DEFENSIVE":
            rej("SHADOW_IN_DEFENSIVE", "no satellite entries in DEFENSIVE, paper included")
            continue
        if str(p.get("direction", "")).strip().upper() != "LONG":
            rej("SHADOW_DIRECTION", f"{t} direction={p.get('direction')!r}; LONG only")
            continue
        thesis = p.get("thesis_en")
        if not isinstance(thesis, str) or not thesis.strip():
            rej("SHADOW_NO_THESIS", f"{t}")
            continue
        url = p.get("catalyst_url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            rej("SHADOW_NO_CATALYST_URL", f"{t}")
            continue
        cts = _aware(_parse_ts(p.get("catalyst_timestamp")))
        if cts is None:
            rej("SHADOW_SCHEMA", f"{t} catalyst_timestamp is not ISO-8601")
            continue
        h = (now - cts).total_seconds() / 3600.0
        if h < 0 or h > C.FRESH_CATALYST_MAX_AGE_HOURS:
            rej("SHADOW_CATALYST_STALE", f"{t} catalyst {h:.1f}h old (0..."
                f"{C.FRESH_CATALYST_MAX_AGE_HOURS}h, same rule as a real entry)")
            continue
        if t in held:
            rej("SHADOW_HELD_REAL", f"{t} is held with real money; a paper pick on it "
                "is not independent evidence")
            continue
        lv = levels.get(t)
        lv = lv if isinstance(lv, dict) else {}
        px, atr, stop, target, rr = (_num(lv.get(k)) for k in
                                     ("price", "atr_14", "stop", "target", "rr"))
        if (None in (px, atr, stop, target, rr) or not (0 < stop < px < target)
                or atr <= 0):
            rej("SHADOW_NO_LEVELS", f"{t} has no valid script levels in "
                "baseline.satellite.levels (Phase 0 fetch failed or name not priced)")
            continue
        dte = _num(lv.get("days_to_earnings"))
        if dte is None:
            dte = _num(d2e.get(t))
        if dte is not None and 0 <= dte <= C.EARNINGS_BLACKOUT_SESSIONS:
            rej("EARNINGS_BLACKOUT", f"{t} reports in {dte:.0f}d "
                f"(<= {C.EARNINGS_BLACKOUT_SESSIONS})")
            continue
        last = recent.get(t)
        if t in taken or (last is not None
                          and (now - last).days < C.SHADOW_REPICK_COOLDOWN_DAYS):
            rej("SHADOW_ALREADY_OPEN", f"{t} already picked "
                f"{'in this run' if t in taken else last.date().isoformat()} "
                f"(cooldown {C.SHADOW_REPICK_COOLDOWN_DAYS}d)")
            continue
        if len(accepted) >= C.SHADOW_MAX_PICKS_PER_RUN:
            rej("SHADOW_OVER_LIMIT", f"{t}: max {C.SHADOW_MAX_PICKS_PER_RUN} picks per run")
            continue
        taken.add(t)
        accepted.append({
            "ticker": t, "direction": "LONG", "thesis_en": thesis.strip(),
            "catalyst_url": url, "catalyst_timestamp": p.get("catalyst_timestamp"),
            "entry_ref_price": px, "atr_14": atr, "stop": stop, "target": target,
            "rr": rr, "rsi": _num(lv.get("rsi")),
            "days_to_earnings": None if dte is None else int(dte),
            "horizon_days": C.SHADOW_HORIZON_DAYS, "levels_source": "core.py/atr",
        })
    return accepted, rejected


def _shadow_block(proposal, baseline, recs_path):
    """validate_shadow, fail-closed for the SHADOW track only: an error drops every
    pick (nothing is logged as evidence) and can never touch the verdict."""
    if not isinstance(proposal, dict):
        return [], []
    try:
        return validate_shadow(proposal, baseline, recs_path)
    except Exception as e:
        return [], [{"ticker": None, "reason": f"SHADOW_SCHEMA: shadow validation error "
                     f"({type(e).__name__}: {e}); all picks dropped"}]


def _baseline_enforced(baseline, violations, ledger_tamper, shadow=None):
    """The one FAIL shape: the pre-approved baseline executes unmodified. Used by
    both the normal FAIL path and the fail-closed guards below, so no input can
    produce zero-bytes-on-stdout with a FAIL exit code (Break B)."""
    accepted, rejected = shadow if shadow is not None else ([], [])
    b = baseline if isinstance(baseline, dict) else {}
    return {"verdict": "FAIL", "violations": violations,
            "shadow": {"accepted": accepted, "rejected": rejected},
            "final_plan": {"source": "BASELINE_ENFORCED",
                           "reason": "validator FAIL — the pre-approved baseline executes unmodified",
                           "orders": b.get("orders", []),
                           "targets": b.get("targets", {}),
                           "execution": b.get("execution"),
                           "shadow_picks": accepted,
                           "rejected_override": None},
            "override_active": False, "ledger_tamper": ledger_tamper,
            "override_expires_after_trading_days": C.OVERRIDE_EXPIRY_TRADING_DAYS}


def _halt(violations, ledger_tamper):
    """A baseline that cannot be trusted as THIS run's approved plan (stale, undated,
    future-dated) is not executed at all — neither the proposal nor the baseline.
    Same shape as main()'s unreadable-baseline HALT; main() exits 2 on it."""
    return {"verdict": "FAIL", "halt": True, "violations": violations,
            "shadow": {"accepted": [], "rejected": []},
            "final_plan": {"source": "HALT",
                           "reason": "baseline_plan.json is not this run's plan; re-run core.py",
                           "orders": [], "shadow_picks": [], "rejected_override": None},
            "override_active": False, "ledger_tamper": ledger_tamper,
            "override_expires_after_trading_days": C.OVERRIDE_EXPIRY_TRADING_DAYS}


def _baseline_age_violation(baseline):
    """None if baseline.generated_utc is within config.BASELINE_MAX_AGE_HOURS of now;
    otherwise the HALT reason. A days-old baseline_plan.json left behind by a core.py
    that exited non-zero must never validate as this run's approved plan."""
    raw = baseline.get("generated_utc") if isinstance(baseline, dict) else None
    ts = _aware(_parse_ts(raw)) if raw else None
    if ts is None:
        return (f"HALT: BASELINE_UNDATED: baseline generated_utc={raw!r} is missing or "
                "not ISO-8601; its freshness cannot be established")
    hrs = (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0
    if hrs < 0:
        return f"HALT: BASELINE_FROM_FUTURE: generated_utc {raw} is {-hrs:.2f}h in the future"
    if hrs > C.BASELINE_MAX_AGE_HOURS:
        return (f"HALT: BASELINE_STALE: baseline generated {hrs:.1f}h ago > "
                f"{C.BASELINE_MAX_AGE_HOURS}h (config.BASELINE_MAX_AGE_HOURS); re-run core.py")
    return None


def _rejected_override(proposal, violations):
    """Contract with report.py: a FAILed OVERRIDE still reaches the ledger (as an
    `override_rejected` line). Never affects orders — the baseline executes."""
    if not isinstance(proposal, dict) or proposal.get("decision") != "OVERRIDE":
        return None
    ovr = proposal.get("override")
    prop_alloc = None
    try:
        norm, errs = CORE.normalize_alloc(proposal.get("final_allocation", {}))
        if not errs and norm:
            prop_alloc = {k: round(w, 4) for k, w in _collapse(norm).items()}
    except Exception:
        prop_alloc = None
    try:
        ovr_out = json.loads(json.dumps(ovr)) if isinstance(ovr, dict) else None
    except Exception:
        ovr_out = None
    return {"override": ovr_out, "proposed_allocation": prop_alloc,
            "violations": [str(x) for x in violations]}


def _with_rejected_override(out, proposal):
    if out.get("verdict") == "FAIL":
        out["final_plan"]["rejected_override"] = _rejected_override(proposal, out["violations"])
    return out


def validate(baseline, proposal, recs_path, last_run_path=None):
    """Never raises (RT1-10): any residual exception is a listed FAIL with the
    baseline enforced, exactly like main()'s belt-and-suspenders guard."""
    if last_run_path is None:
        last_run_path = C.LAST_RUN_JSON
    try:
        out = _validate(baseline, proposal, recs_path, last_run_path)
    except Exception as e:
        try:
            tamper = not S.ledger_intact(recs_path, last_run_path)
        except Exception:
            tamper = None
        # N2: the freshness HALT is re-checked on the exception path, so an
        # unreadable ledger can never turn a stale baseline into executable orders
        try:
            stale = _baseline_age_violation(baseline)
        except Exception as e2:
            stale = f"HALT: BASELINE_UNDATED: freshness check failed ({type(e2).__name__})"
        if stale:
            return _halt([stale, f"SCHEMA_VIOLATION: uncaught {type(e).__name__}: {e}"], tamper)
        out = _baseline_enforced(
            baseline, [f"SCHEMA_VIOLATION: uncaught {type(e).__name__}: {e}"], tamper)
    return _with_rejected_override(out, proposal)


def _obj_list(proposal, key, v):
    """proposal[key] as a list of dicts. A non-list, or a non-object item, is a
    listed SCHEMA_VIOLATION (never a crash); the bad items are dropped."""
    raw = proposal.get(key)
    if raw is None or (not raw and isinstance(raw, (list, dict, str))):
        return []                       # absent / empty: nothing declared (as before)
    if not isinstance(raw, list):
        v.append(f"SCHEMA_VIOLATION: {key} must be a list of objects, got {type(raw).__name__}")
        return []
    out = []
    for i, x in enumerate(raw):
        if isinstance(x, dict):
            out.append(x)
        else:
            v.append(f"SCHEMA_VIOLATION: {key}[{i}] must be an object, got {type(x).__name__}")
    return out


def _validate(baseline, proposal, recs_path, last_run_path):
    # A stale / undated baseline is not this run's approved plan: HALT, execute
    # nothing. N2: checked FIRST, before anything that can raise (the ledger read
    # below included); validate() re-checks it on the exception path.
    stale = _baseline_age_violation(baseline)
    if stale:
        try:
            tamper = not S.ledger_intact(recs_path, last_run_path)
        except Exception:
            tamper = None
        return _halt([stale], tamper)

    # Break A: the anchored integrity check. Unknown/missing anchor with a non-empty
    # ledger is still internally checked; a first run (no last_run.json) with an
    # empty ledger passes. Tamper is fail-safe: it suspends overrides below. (A torn
    # final line or an unreadable ledger is not intact either.)
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
    defensive = regime == "DEFENSIVE"
    weekly = baseline.get("mode") == "weekly"

    # 0. data integrity — negative age = partial bar leaked = hard abort. NaN / inf /
    #    non-numeric is not an age (NaN compares False to everything and would pass).
    raw_age = (baseline.get("data") or {}).get("data_age_hours")
    age = _num(raw_age) if not isinstance(raw_age, bool) else None
    if age is None or age < 0:
        v.append(f"DATA_AGE_INVALID: {raw_age!r} (negative/non-finite age = partial bar "
                 "or corrupt baseline; hard abort)")
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
    if not isinstance(decision, str) or decision not in ("CONFIRM_BASELINE", "OVERRIDE"):
        v.append(f"SCHEMA_VIOLATION: decision must be CONFIRM_BASELINE or OVERRIDE, "
                 f"got {decision!r}")
        decision = None if not isinstance(decision, str) else decision

    # 1b. ALLOCATION is parsed exactly once, safely (BREAK 4): non-numeric weights
    #     become listed SCHEMA_VIOLATIONs (not a raw crash), and case-variant keys
    #     ({"QQQ","qqq","Qqq"}) collapse to one canonical ticker (not three orders).
    raw_alloc = proposal.get("final_allocation", {})
    alloc, alloc_errs = CORE.normalize_alloc(raw_alloc)
    for e in alloc_errs:
        v.append(f"SCHEMA_VIOLATION: {e}")

    core_set = {str(t).upper() for t in C.CORE_TICKERS}

    # 1c. CONFIRM_BASELINE is PINNED, not floored (v5.1). The collapsed allocation
    #     must equal either the ideal target (targets.target_allocation, e.g. CORE
    #     100) or what the whole-share baseline actually realises
    #     (execution.post_trade_allocation, e.g. CORE 95.3). Either way the orders
    #     are the baseline's verbatim, so the unavoidable residual is reported, never
    #     widened into a choice. A legacy baseline (no execution block) keeps the
    #     paper floor/ceiling/cash checks below. A CONFIRM that matched one of the
    #     baseline's OWN references cannot be failed by an allocation-shape check the
    #     baseline itself would fail (RT1-8: untrimmable held satellite just over
    #     budget; an all-cash book too small to buy -> post-trade allocation {}).
    confirm_refs = []
    if decision == "CONFIRM_BASELINE" and isinstance(baseline.get("execution"), dict):
        if isinstance(tgt.get("target_allocation"), dict):
            confirm_refs.append(("targets.target_allocation",
                                 _collapse(tgt["target_allocation"], trusted=True)))
        if isinstance(baseline["execution"].get("post_trade_allocation"), dict):
            confirm_refs.append(("execution.post_trade_allocation",
                                 _collapse(baseline["execution"]["post_trade_allocation"],
                                           trusted=True)))
    confirm_matched = False
    if confirm_refs and isinstance(raw_alloc, dict) and not alloc_errs:
        confirm_matched = any(_alloc_matches(_collapse(alloc), ref,
                                             C.CONFIRM_ALLOCATION_TOLERANCE_PP)
                              for _, ref in confirm_refs)

    if not raw_alloc and not confirm_matched:
        v.append("SCHEMA_VIOLATION: final_allocation is required. A proposal that "
                 "omits it cannot be gated and therefore cannot be executed.")

    _LEGAL = ({C.CORE_TICKER} | core_set | set(C.BROAD_ETFS)
              | set(C.SATELLITE_UNIVERSE))
    priceable = _priceable_set(baseline)
    # the core is ONE sleeve: a weight on any core ticker is priced iff the sleeve is
    # (Phase 0 aborts without a CORE_TICKER price, so in practice always). The split
    # is ignored by the derivation, so an unpriced QQQM weight cannot leak to cash.
    core_priced = bool(priceable & core_set)
    for tk, w in alloc.items():
        if tk in _CASHY:
            v.append(f"SCHEMA_VIOLATION: '{tk}' in final_allocation. Cash is a residual, not a decision.")
            continue
        if tk not in _LEGAL:
            v.append(f"TICKER_NOT_IN_UNIVERSE: {tk} in final_allocation "
                     f"(cash-proxy or off-universe tickers are not allocatable)")
        # BREAK 1: a ticker the plan cannot price would have its BUY silently dropped
        # by the derivation and its target would leak straight to cash.
        if abs(w) > 1e-9 and not (tk in priceable or (tk in core_set and core_priced)):
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
                        if tk not in C.BROAD_ETFS and tk not in _CASHY and tk not in core_set)
    if sat_alloc_pct > C.SATELLITE_MAX_PCT * 100 + 1e-6 and not confirm_matched:
        v.append(f"SATELLITE_OVER_BUDGET_IN_ALLOCATION: {sat_alloc_pct:.1f}% non-core equity "
                 f"> {C.SATELLITE_MAX_PCT*100:.0f}% (declared satellite[] list does not define "
                 f"exposure; the allocation does)")

    # 1d. Allocation-side satellite gates (RT1-3). The force-close and the ATR stop
    #     are the baseline's; an allocation may not cancel either of them.
    sat_block = baseline.get("satellite", {}) or {}
    breached = {str(x).upper() for x in (sat_block.get("stop_breaches", []) or [])}
    if not confirm_matched:
        for tk, w in sorted(alloc.items()):
            if tk in _CASHY or tk in core_set or w <= _EPS:
                continue
            if defensive:
                v.append(f"SATELLITE_IN_DEFENSIVE: {tk} {w:.1f}% in final_allocation; in "
                         "DEFENSIVE every non-core position is force-closed (weight must be 0)")
            if tk in breached:
                v.append(f"STOP_BREACH_IN_ALLOCATION: {tk} {w:.1f}% in final_allocation but "
                         "its ATR stop is breached; the stop-out is not overridable")

    raw_ovr = proposal.get("override")
    if raw_ovr is not None and not isinstance(raw_ovr, dict):
        v.append(f"SCHEMA_VIOLATION: override must be an object, got {type(raw_ovr).__name__}")
    ovr = raw_ovr if isinstance(raw_ovr, dict) and raw_ovr else None
    allowance = 0.0

    # 1e. The LLM may not author orders at all — validate.py derives them.
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
        if weekly:
            v.append("OVERRIDE_IN_WEEKLY: a weekly run is a satellite stop-check only; "
                     "an override would derive a full rebalance outside the monthly cadence")
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
        if defensive and d != "re_risk":
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
    #    RT1-1: `direction` is a SELF-DECLARED label. In DEFENSIVE the de-risk
    #    allowance is therefore never granted downward (floor, cash ceiling and the
    #    execution floor carry no allowance), and the realised equity must not fall
    #    below what the baseline realises — enforced on numbers, not on the label.
    equity_target = float(tgt["equity_target_pct"])
    down_allowance = 0.0 if defensive else allowance
    floor = equity_target - C.CASH_MAX_PCT * 100 - down_allowance
    ceiling = equity_target
    if (defensive and ovr and ovr.get("direction") == "re_risk" and allowance):
        ceiling = min((C.TARGETS["TREND"][0] + C.TARGETS["TREND"][1]) * 100,
                      equity_target + allowance)

    if confirm_refs:
        if not confirm_matched:
            mine = _collapse(alloc)
            v.append(f"CONFIRM_MISMATCH: final_allocation collapses to {_fmt_alloc(mine)}; "
                     "CONFIRM_BASELINE must equal "
                     + " or ".join(f"{n} {_fmt_alloc(r)}" for n, r in confirm_refs)
                     + f" within {C.CONFIRM_ALLOCATION_TOLERANCE_PP}pp per line "
                     f"(core tickers {sorted(core_set)} count as one line)")
    else:
        if equity_pct is not None and equity_pct < floor - 1e-6:
            v.append(f"EQUITY_BELOW_FLOOR: {equity_pct:.1f}% < {floor:.1f}% "
                     f"(regime target {equity_target:.1f}% − float {C.CASH_MAX_PCT*100:.0f}% "
                     f"− allowance {down_allowance:.0f}pp)")
        if equity_pct is not None and equity_pct > ceiling + 1e-6:
            v.append(f"EQUITY_ABOVE_CEILING: {equity_pct:.1f}% > {ceiling:.1f}% "
                     f"(the LLM may not lever above the regime dial)")

        # 4. cash ceiling. There is no >60%-cash trigger and no 40–60% dead zone
        #    anymore. In TREND the ceiling is the operational float; in DEFENSIVE it
        #    is the regime's own cash-equivalent sleeve plus that float. This is the
        #    PAPER float: the whole-share residual never widens it.
        if equity_pct is not None:
            implied_cash = 100.0 - equity_pct
            max_cash = (100.0 - equity_target) + C.CASH_MAX_PCT * 100 + down_allowance
            if implied_cash > max_cash + 1e-6:
                v.append(f"CASH_OVER_MAX: implied cash {implied_cash:.1f}% > {max_cash:.1f}%")

    # 5. satellite limits (malformed satellite[] / vetoes[] items are listed
    #    SCHEMA_VIOLATIONs and dropped — never a crash, RT1-10)
    sat = _obj_list(proposal, "satellite", v)
    if len(sat) > C.SATELLITE_MAX_NAMES:
        v.append(f"SATELLITE_TOO_MANY_NAMES: {len(sat)} > {C.SATELLITE_MAX_NAMES}")
    sat_pct = 0.0
    for s in sat:
        if s.get("size_pct") is None:
            continue
        f = _num(s.get("size_pct"))
        if f is None or isinstance(s.get("size_pct"), bool):
            v.append(f"SCHEMA_VIOLATION: satellite {str(s.get('ticker', '')).upper()} "
                     f"size_pct={s.get('size_pct')!r} is not a finite number")
            continue
        sat_pct += f
    if sat_pct > C.SATELLITE_MAX_PCT * 100 + 1e-6:
        v.append(f"SATELLITE_OVER_BUDGET: {sat_pct:.1f}% > {C.SATELLITE_MAX_PCT*100:.0f}%")
    if defensive and sat:
        v.append("SATELLITE_IN_DEFENSIVE: satellite must be force-closed")

    d2e = sat_block.get("days_to_earnings", {}) or {}
    d2e = d2e if isinstance(d2e, dict) else {}
    base_rsi = sat_block.get("rsi", {}) or {}
    base_rsi = base_rsi if isinstance(base_rsi, dict) else {}
    sat_levels = sat_block.get("levels") or {}
    sat_levels = ({str(k).upper(): lv for k, lv in sat_levels.items()}
                  if isinstance(sat_levels, dict) else {})
    last_buy, roundtrips = _satellite_history(recs_path)
    vetoes = _obj_list(proposal, "vetoes", v)
    vetoed = {str(x.get("ticker", "")).upper() for x in vetoes}
    now = datetime.now(timezone.utc)

    # 5-. REAL-MONEY SATELLITE IS SWITCHED OFF until graduation (human edit to
    #     config.py). While off: no satellite BUY of any spelling, and no non-core
    #     weight above what is already held — any decision. Existing gates below
    #     keep running so the switch stays a one-symbol change.
    held_pct = _held_pct(baseline)
    if not C.SATELLITE_REAL_MONEY_ENABLED:
        for s in sat:
            if str(s.get("action", "")).strip().upper() == "BUY":
                v.append(f"SATELLITE_NOT_GRADUATED: satellite BUY "
                         f"{str(s.get('ticker', '')).upper()} while "
                         "config.SATELLITE_REAL_MONEY_ENABLED is off (shadow_picks only)")
        for tk, w in alloc.items():
            if tk in _CASHY or tk in core_set or w <= _EPS:
                continue
            if w > held_pct.get(tk, 0.0) + _EPS:
                v.append(f"SATELLITE_NOT_GRADUATED: {tk} {w:.1f}% > held "
                         f"{held_pct.get(tk, 0.0):.1f}% in final_allocation while "
                         "config.SATELLITE_REAL_MONEY_ENABLED is off")

    # 5--. SHADOW ISOLATION: a shadow-picked ticker may never carry real weight
    #      above what is held. Read from the raw ticker list, before and independent
    #      of validate_shadow. A core ticker is never a legal shadow pick (it is
    #      rejected inside validate_shadow as TICKER_NOT_IN_UNIVERSE) and the core
    #      weight is gated elsewhere, so naming one must not change the verdict (RT1-2).
    for tk in sorted(_raw_shadow_tickers(proposal) - core_set):
        w = alloc.get(tk, 0.0)
        if w > held_pct.get(tk, 0.0) + _EPS:
            v.append(f"SHADOW_IN_ALLOCATION: {tk} is a shadow pick and carries {w:.1f}% "
                     f"(> held {held_pct.get(tk, 0.0):.1f}%) in final_allocation; "
                     "paper picks never move real money")

    for s in sat:
        t = str(s.get("ticker", "")).upper()
        if t not in C.SATELLITE_UNIVERSE:
            v.append(f"TICKER_NOT_IN_UNIVERSE: {t} (the LLM may not add tickers)")
        if s.get("action") == "BUY":
            # R/R and levels are the SCRIPT's (baseline.satellite.levels, Phase 0).
            # An LLM-supplied rr/stop/target that is not the script's number is an
            # invented number, not an input.
            lv = sat_levels.get(t)
            rr = _num(lv.get("rr")) if isinstance(lv, dict) else None
            if rr is None:
                v.append(f"SATELLITE_NO_SCRIPT_LEVELS: {t} has no baseline.satellite.levels "
                         "entry; R/R cannot be gated")
            else:
                for k in ("rr", "stop", "target"):
                    if s.get(k) is None:
                        continue
                    mine, script = _num(s.get(k)), _num(lv.get(k))
                    if mine is None or script is None or round(mine, 2) != round(script, 2):
                        v.append(f"LLM_INVENTED_NUMBER: {t} {k}={s.get(k)!r} but the script "
                                 f"level is {lv.get(k)!r}")
                if rr < C.SATELLITE_MIN_RR:
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
            dd = _num(d2e.get(t))
            if dd is None and isinstance(lv, dict):
                dd = _num(lv.get("days_to_earnings"))
            if dd is not None and 0 <= dd <= C.EARNINGS_BLACKOUT_SESSIONS:
                v.append(f"EARNINGS_BLACKOUT: {t} reports in {dd}d "
                         f"(<= {C.EARNINGS_BLACKOUT_SESSIONS})")
            # RSI is a SIZING input, never a veto: above the threshold the position
            # is HALVED, not refused. ("Overbought, don't chase" cost v4 -6.67pp.)
            r_ = _num(base_rsi.get(t))
            if r_ is not None and r_ > C.SATELLITE_RSI_HALVE_ABOVE:
                cap = C.SATELLITE_MAX_PCT * 100 / 2
                if (_num(s.get("size_pct")) or 0.0) > cap + 1e-6:
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
    #     the baseline orders verbatim and is pinned by CONFIRM_MISMATCH, so exempt.)
    declared_buys = {str(s.get("ticker", "")).upper() for s in sat
                     if s.get("action") == "BUY"}
    declared_sells = {str(s.get("ticker", "")).upper() for s in sat
                      if s.get("action") == "SELL"}
    if decision == "OVERRIDE":
        for tk, w in alloc.items():
            if tk in _CASHY or tk in C.BROAD_ETFS or tk in core_set or w <= 1e-6:
                continue
            if w > held_pct.get(tk, 0.0) + 1e-6:   # a new or increased satellite bet
                if tk not in declared_buys:
                    v.append(f"SATELLITE_IN_ALLOCATION_NOT_DECLARED: {tk} carries "
                             f"{w:.1f}% (> held {held_pct.get(tk, 0.0):.1f}%) in "
                             "final_allocation but is not a declared satellite BUY; it "
                             "would escape every satellite gate")

    # 5b. Phase 1 vetoes must be dated and fresh, or they do not exist.
    for x in vetoes:
        ed = _aware(_parse_ts(x.get("event_date")))
        if ed is None:
            v.append(f"VETO_UNDATED: {x.get('ticker')} (a veto without a dated event "
                     "is prose, not evidence)")
        elif (now - ed).days > C.VETO_MAX_AGE_DAYS:
            v.append(f"VETO_STALE: {x.get('ticker')} event {(now - ed).days}d old > "
                     f"{C.VETO_MAX_AGE_DAYS}d")

    # 6. single-name cap — SINGLE NAMES ONLY. Broad ETFs are uncapped, forever.
    #    (Not applied to a CONFIRM that matched the baseline's own reference: a held
    #    single share the whole-share trim cannot cut below the cap is the baseline's
    #    residual, and the baseline executes regardless — RT1-8 class.)
    for tk, w in alloc.items():
        if (tk in C.BROAD_ETFS and C.BROAD_ETF_UNCAPPED) or confirm_matched:
            continue
        if w > C.SINGLE_NAME_MAX_PCT * 100 + 1e-6:
            v.append(f"SINGLE_NAME_OVER_CAP: {tk} {w:.1f}% > {C.SINGLE_NAME_MAX_PCT*100:.0f}%")

    # 7. DERIVE-AND-RE-CHECK (BREAK 1, the root fix). Gating the allocation % while
    #    trusting the derivation is the hole: derive the orders now, and if they do
    #    not reproduce the intended equity within tolerance, FAIL — a plan that is
    #    100% equity on paper and 100% cash in execution must not PASS. Derivation
    #    is wrapped so any residual coercion error is a listed FAIL, never a crash.
    #    v5.1: two numbers are checked, because whole shares make them differ.
    #      (i)  fractional (unconstrained) equity vs intended  -> EXECUTION_DIVERGES
    #           (dropped / unpriceable lines; not confused by lot rounding)
    #      (ii) realised whole-share equity vs the baseline's realised equity
    #           -> an override may cut REAL equity by at most the allowance (none in
    #           DEFENSIVE); lot rounding is never a free extra de-risk.
    derived, execution = None, None
    if not v:
        if decision == "CONFIRM_BASELINE":
            derived, execution = baseline["orders"], baseline.get("execution")
        else:
            try:
                ex = CORE.derive_execution(alloc, baseline)
                derived, execution = ex["orders"], ex["execution"]
                fractional = float(ex["fractional_equity_pct"])
                # never trust the derivation's self-report alone: recompute from the
                # emitted orders and take the side least favourable to the proposal
                recomputed = CORE.realized_equity_pct(baseline, derived)
                realized_lo = min(float(ex["realized_equity_pct"]), recomputed)
                realized_hi = max(float(ex["realized_equity_pct"]), recomputed)
            except Exception as e:
                v.append(f"SCHEMA_VIOLATION: order derivation failed ({e})")
                derived, execution = None, None
        if derived is not None and decision != "CONFIRM_BASELINE":
            tol = C.EXECUTION_DIVERGENCE_TOLERANCE_PP
            intended = equity_pct if equity_pct is not None else 0.0
            if abs(fractional - intended) > tol:
                v.append(f"EXECUTION_DIVERGES: derived orders realize {fractional:.1f}% "
                         f"equity vs intended {intended:.1f}% "
                         f"(> {tol:.1f}pp); the paper allocation does not survive execution")
            base_real = _baseline_realized_equity_pct(baseline)
            exec_floor = base_real - down_allowance - tol
            if realized_lo < exec_floor - _EPS:
                v.append(f"OVERRIDE_EXECUTION_BELOW_FLOOR: whole-share execution realizes "
                         f"{realized_lo:.1f}% equity < {exec_floor:.1f}% (baseline realizes "
                         f"{base_real:.1f}% − allowance {down_allowance:.0f}pp − {tol:.1f}pp); "
                         "lot rounding may not deepen the override")
            if defensive:
                base_exact = CORE.realized_equity_pct(baseline, baseline.get("orders", []))
                ref = min(base_exact, base_real)
                if realized_lo < ref - _EPS:
                    v.append(f"OVERRIDE_DEEPENS_DEFENSIVE: whole-share execution realizes "
                             f"{realized_lo:.2f}% equity < the baseline's {ref:.2f}%; in "
                             "DEFENSIVE an override may only re-risk, whatever its label")
            if realized_hi > ceiling + tol + _EPS:
                v.append(f"OVERRIDE_EXECUTION_ABOVE_CEILING: whole-share execution realizes "
                         f"{realized_hi:.1f}% equity > ceiling {ceiling:.1f}% + {tol:.1f}pp")
            # RT1-6: an unfunded order set is not executable (margin). The derivation
            # caps BUYs by cash; this re-checks the book it actually produces.
            try:
                pt_cash = float(execution["post_trade"]["cash_usd"])
            except (KeyError, TypeError, ValueError):
                pt_cash = None
            if pt_cash is None or pt_cash != pt_cash or pt_cash < -0.005:
                v.append(f"EXECUTION_NEGATIVE_CASH: derived orders leave post-trade cash "
                         f"{pt_cash!r} < 0 (unfunded BUY)")
            # N3: an OVERRIDE whose derived orders are exactly the baseline's is not a
            # deviation. PASSing it would log an "override" with a 0.0 spread that
            # resets nothing and proves nothing — FAIL it (the baseline executes
            # anyway, so nothing changes for the book).
            def _sig(os_):
                return sorted((str(o.get("ticker", "")).upper(), str(o.get("action", "")).upper(),
                               round(float(o.get("shares") or 0.0), 4)) for o in os_ or [])
            if _sig(derived) == _sig(baseline.get("orders", [])):
                v.append("OVERRIDE_NO_EFFECT: the derived orders equal the baseline's own "
                         f"({len(derived)} order(s)); an override that changes nothing is not "
                         "an override — use CONFIRM_BASELINE")
            derived_buys = {str(o.get("ticker", "")).upper() for o in derived
                            if str(o.get("action", "")).upper() == "BUY"}
            for tk in sorted(declared_buys - core_set - derived_buys):
                v.append(f"SATELLITE_BUY_NOT_EXECUTED: declared satellite BUY {tk} produces "
                         "no derived order (unfunded, below the drift band or below "
                         "config.MIN_ORDER_USD); a validated plan must execute what it declares")
            # RT1-3 / Coder-2 gap: a satellite SELL beyond what the baseline itself
            # sells (a cut made only through the allocation) is LLM-initiated and must
            # be a declared satellite[] SELL, so the min-hold churn guard runs on it.
            base_sell = {}
            for o in baseline.get("orders", []):
                if str(o.get("action", "")).upper() == "SELL":
                    tk = str(o.get("ticker", "")).upper()
                    base_sell[tk] = base_sell.get(tk, 0.0) + float(o.get("shares") or 0.0)
            for o in derived:
                tk = str(o.get("ticker", "")).upper()
                if (str(o.get("action", "")).upper() != "SELL" or tk in core_set
                        or tk in declared_sells):
                    continue
                if float(o.get("shares") or 0.0) > base_sell.get(tk, 0.0) + 1e-6:
                    v.append(f"SATELLITE_SELL_NOT_DECLARED: derived order SELL {tk} "
                             f"{o.get('shares')} sh exceeds the baseline's "
                             f"{base_sell.get(tk, 0.0)} sh; an allocation cut of a held "
                             "satellite must be a declared satellite[] SELL (min-hold guard)")
        # post-derivation isolation invariant: while real-money satellite is off, no
        # order that would execute may BUY anything outside the core sleeve.
        if derived is not None and not C.SATELLITE_REAL_MONEY_ENABLED:
            for o in derived:
                if (str(o.get("action", "")).upper() == "BUY"
                        and str(o.get("ticker", "")).upper() not in core_set):
                    v.append(f"SHADOW_ISOLATION_BREACH: derived order BUY "
                             f"{str(o.get('ticker', '')).upper()} outside the core sleeve "
                             "while config.SATELLITE_REAL_MONEY_ENABLED is off")
        if v:
            derived, execution = None, None

    verdict = "PASS" if not v else "FAIL"

    # Shadow picks are validated only now, AFTER the verdict is fixed. Nothing
    # below may touch `v`, the orders, or the allocation.
    shadow = _shadow_block(proposal, baseline, recs_path)
    if verdict == "FAIL":
        return _baseline_enforced(baseline, v, ledger_tamper, shadow)

    # E3: orders are DERIVED, never authored. Every script-owned key is overwritten,
    # so nothing the LLM wrote under these names survives into the final plan.
    proposal.pop("orders", None)
    proposal["orders"] = derived
    proposal["source"] = ("BASELINE" if decision == "CONFIRM_BASELINE"
                          else "OVERRIDE_VALIDATED")
    proposal["execution"] = execution
    proposal["shadow_picks"] = shadow[0]
    proposal["rejected_override"] = None
    return {"verdict": verdict, "violations": v,
            "shadow": {"accepted": shadow[0], "rejected": shadow[1]},
            "final_plan": proposal,
            "override_active": bool(ovr),
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
        print(json.dumps(_halt([f"HALT: cannot read baseline ({e})"], None),
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
        out = _with_rejected_override(_baseline_enforced(
            baseline, [f"SCHEMA_VIOLATION: uncaught {type(e).__name__}: {e}"], tamper),
            proposal)

    print(json.dumps(out, indent=2, ensure_ascii=False))
    if (out.get("final_plan") or {}).get("source") == "HALT":
        sys.exit(2)          # stale/undated baseline: nothing executes, pipeline halts
    sys.exit(0 if out["verdict"] == "PASS" else 1)


if __name__ == "__main__":
    main()
