#!/usr/bin/env python3
"""us-stock-advisor v5.1 — Phase 4 logging + report header (DETERMINISTIC numbers).

Install path: ~/.claude/skills/us-stock-advisor/scripts/report.py

Does these things, all of which the LLM is forbidden from doing itself:
  1. append_run(): writes EXACTLY ONE portfolio_mark line per run-date to
     recommendations.jsonl — including no-ops — plus one line per order/override/
     satellite trade/veto/shadow pick. No run escapes the log. A second run on the
     same UTC date is REFUSED (exit 4, nothing appended) unless --force-relog
     --reason "..." (the reason is stored on the mark); the scorer counts only the
     last run of a date, and the header still lists the superseded order/override
     lines. A FAILed override (final_plan.rejected_override) is logged as an
     override_rejected line — never omitted, never scored as an executed override.
  2. header(): computes the Korean report's mandatory top block (누적 vs QQQ,
     LLM-layer spread vs the mechanical baseline, override P&L, shadow track). The
     LLM writes prose AROUND these numbers and may not restate them from memory.
  3. last_run(): writes state/last_run.json — STRUCTURED JSON ONLY. This is the
     entire continuity payload for the next run. Prior report PROSE is never fed
     back (that archive-as-prompt loop carried abolished v4.0 rules forward for
     7 runs and made the model invent cap violations that no longer existed).
  4. adjust_flow(): corrects a PAST external cash flow by APPENDING a
     cash_flow_adjustment line. The ledger is never rewritten.

Every write builds and validates ALL of its records first, then appends them and
moves the ledger anchor under ONE exclusive lock (score_recs.commit_locked), so a
bad input or a crash cannot leave a partial run or a stale anchor.

Usage:
  python3 report.py --log --baseline baseline_plan.json --final final_plan.json
                    [--deposit USD] [--withdraw USD]
                    [--force-relog --reason "why this run supersedes today's"
                     [--additional-flow]]
          # exit 4 = duplicate date (or a re-log repeating a flow already declared that
          #          day without --additional-flow), 2 = bad input / HALT plan,
          #          5 = torn ledger tail / unreadable ledger (nothing appended)
  python3 report.py --adjust-flow --effective-date YYYY-MM-DD --flow-usd X
                    [--flow-krw K] [--basis ledger_implied|declared] --reason "..." [--force-relog]
  python3 report.py --halt "<reason>" [--stage S]
  python3 report.py --repair-torn-tail --reason "..."   # drops ONLY an unparseable final
                    # line (copy kept in state/), appends a ledger_repair line; exit 2 if
                    # there is nothing to repair
  python3 report.py --header
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date as _date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C            # noqa: E402
import score_recs as S        # noqa: E402

EXIT_DUPLICATE = 4            # same-date run refused; NOT a halt, do not retry blindly
EXIT_LEDGER_TAMPER = 5        # adjustment refused on a broken chain / anchor
EXIT_REFUSED = 2              # bad input / nothing to attribute the adjustment to
EXIT_WRITE_FAILED = 6         # state I/O failed (e.g. read-only state dir). If the ledger
                              # append itself landed, the pending intent written first lets
                              # the next write complete the commit (never read as tamper)


class DuplicateRunError(Exception):
    """A portfolio_mark for today's UTC date already exists and --force-relog was
    not given. Nothing was appended and last_run.json was not touched."""


class RunRefused(Exception):
    """--log input rejected BEFORE anything was appended (exit 2)."""


class DuplicateFlowError(Exception):
    """A forced re-log declared a --deposit/--withdraw equal (within the near-
    duplicate tolerance) to a flow already declared that day, without
    --additional-flow. Nothing appended (exit 4) — D3."""


class AdjustmentRefused(Exception):
    def __init__(self, msg, code=EXIT_REFUSED):
        super().__init__(msg)
        self.code = code


def _today():
    return datetime.now(timezone.utc).date().isoformat()


def _core_px(baseline):
    """CORE_TICKER's (regime/benchmark ticker) price for this run: the v5.1 prices
    map, else a held position, else a core order's reference price."""
    px = (baseline.get("prices") or {}).get(C.CORE_TICKER)
    px = px or next((d["usd"] / d["shares"] for t, d in
                     baseline["portfolio"]["positions"].items()
                     if t == C.CORE_TICKER and d.get("shares")), None)
    px = px or next((o["limit_ref_price"] for o in baseline.get("orders", [])
                     if o["ticker"] == C.CORE_TICKER), None)
    return px


def _mark_baseline_nav(baseline, prev):
    """Curve 2: the un-overridden mechanical baseline, compounded on the core
    ticker's price between runs at the baseline's EXECUTABLE equity weight
    (execution.post_trade.equity_pct; whole-share cash drag is the baseline's, not
    the LLM layer's). Without a producer here, `mechanical_baseline_cum_pct` is null
    forever, the LLM-layer spread is null forever, and the kill trigger can never
    fire — which is the state the draft shipped in."""
    # the price MUST be the CORE ticker's. orders[0] is frequently the satellite
    # trim SELL, and compounding the mechanical baseline on NVDA's price would make
    # curve 2 — the curve the kill trigger reads — measure the wrong asset.
    px = _core_px(baseline)
    if px is None:
        return prev.get("baseline_cum_return_pct")
    b = prev.get("baseline_nav") or {"start_px": px, "cum": 0.0, "eq": None}
    if b.get("eq") is not None and b.get("last_px"):
        b["cum"] = (1 + b["cum"] / 100) * (1 + b["eq"] / 100 * (px / b["last_px"] - 1)) * 100 - 100
    b["last_px"] = px
    post = (baseline.get("execution") or {}).get("post_trade") or {}
    b["eq"] = post.get("equity_pct", baseline["targets"]["equity_target_pct"])
    prev["baseline_nav"] = b
    prev["baseline_cum_return_pct"] = round(b["cum"], 2)
    return round(b["cum"], 2)


def _core_pct(p):
    if p.get("core_pct") is not None:
        return p["core_pct"]
    tot = float(p.get("total_usd") or 0)
    core = sum(float(d.get("usd") or 0) for t, d in p.get("positions", {}).items()
               if t in C.CORE_TICKERS)
    return round(core / tot * 100, 1) if tot > 0 else None


def _num(x):
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _post_equity(plan):
    """execution.post_trade.equity_pct of a plan (percent), or None."""
    ex = (plan or {}).get("execution") or {}
    return _num((ex.get("post_trade") or {}).get("equity_pct"))


def _implied_flow(recs, today, baseline):
    """The flow the ledger implies between the last mark of an EARLIER date and
    this run's portfolio — the same estimator the read-time reconciliation uses
    (score_recs.implied_flow: cash change not explained by position changes). None
    when a changed position cannot be priced."""
    prev = next((r for r in reversed(recs) if r.get("type") == "portfolio_mark"
                 and str(r.get("date")) < today), None)
    if prev is None:
        return None
    p = baseline["portfolio"]
    return S.implied_flow(prev, {"cash_usd": p.get("cash_usd"),
                                 "positions": p.get("positions") or {}})


def _check_items(final, key):
    items = final.get(key) or []
    if not isinstance(items, list):
        raise RunRefused(f"final_plan.{key} must be a list")
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            raise RunRefused(f"final_plan.{key}[{i}] is not an object")
    return items


def append_run(baseline, final, deposit_usd=0.0, prices_csv=None, withdraw_usd=0.0,
               force=False, reason=None, additional_flow=False):
    """Log ONE run. Every record is built and validated first; then, under a single
    ledger lock, the duplicate-date check, the appends (one write) and the atomic
    anchor move happen together (F5/F6). Raises RunRefused / DuplicateRunError with
    nothing appended and last_run.json untouched."""
    outer = {}
    if isinstance(final, dict) and "final_plan" in final:
        outer, final = final, final["final_plan"]
    if not isinstance(final, dict):
        raise RunRefused("final plan must be a JSON object")
    final = dict(final)
    if (str(final.get("source") or "").upper() == "HALT" or final.get("halt") is True
            or outer.get("halt") is True):
        # N5: validate.py exit 2 (stale / unreadable baseline). Nothing executes, so
        # there is no run to mark — it is a halt (report.py --halt), never a run.
        raise RunRefused("final plan source is HALT (validate.py exit 2): nothing executes; "
                         "log it with report.py --halt \"<reason>\" --stage phase3, not --log")
    if final.get("orders") is None:
        # CONFIRM_BASELINE with no orders key == the baseline orders. A validated
        # EMPTY list is a decision and stays empty.
        final["orders"] = baseline.get("orders") or []
    relog_reason = str(reason or "").strip()
    if force and not relog_reason:
        raise RunRefused("--force-relog requires --reason \"why this run supersedes today's "
                         "earlier one\" (stored on the mark)")
    try:
        regime = baseline["regime"]["regime"]
        p = baseline["portfolio"]
        float(p["total_usd"]), float(p["cash_usd"])
        if not isinstance(p.get("positions", {}), dict):
            raise TypeError("positions must be an object")
        if _post_equity(baseline) is None:
            float(baseline["targets"]["equity_target_pct"])      # _mark_baseline_nav needs one
    except (KeyError, TypeError, ValueError) as e:
        raise RunRefused(f"baseline plan is malformed ({type(e).__name__}: {e})")
    orders = _check_items(final, "orders")
    for i, o in enumerate(orders):
        if not isinstance(o.get("ticker"), str) or not o["ticker"].strip():
            raise RunRefused(f"final_plan.orders[{i}] has no ticker")
        if o.get("action") not in ("BUY", "SELL"):
            raise RunRefused(f"final_plan.orders[{i}] action must be BUY|SELL, got {o.get('action')!r}")
    vetoes = _check_items(final, "vetoes")
    picks = _check_items(final, "shadow_picks")
    ovr = final.get("override")
    if ovr is not None and not isinstance(ovr, dict):
        raise RunRefused("final_plan.override must be an object or null")
    rej = final.get("rejected_override", outer.get("rejected_override"))
    if rej is not None and not isinstance(rej, dict):
        raise RunRefused("final_plan.rejected_override must be an object or null")

    today = _today()
    ex = final.get("execution") or baseline.get("execution") or {}
    residual = ex.get("residual")
    flow = round(float(deposit_usd or 0) - float(withdraw_usd or 0), 2)
    anchor = S._anchor_path(C.RECS_JSONL)
    os.makedirs(C.STATE_DIR, exist_ok=True)

    with S.ledger_lock(C.RECS_JSONL) as f:
        recs = S.load_recs(C.RECS_JSONL)
        same_date = [r for r in recs if r.get("type") == "portfolio_mark" and r.get("date") == today]
        if same_date and not force:
            raise DuplicateRunError(
                f"a portfolio_mark for {today} (UTC) already exists "
                f"({len(same_date)} on file); nothing appended, last_run.json untouched. "
                f"Use --force-relog --reason \"...\" only if this run must supersede the earlier one.")
        # prior structured state is read under the lock: the baseline-NAV marker
        # mutates it and it is re-persisted by the same commit.
        prev = {}
        if os.path.exists(anchor):
            try:
                with open(anchor) as fh:
                    prev = json.load(fh)
            except Exception:
                prev = {}
            if not isinstance(prev, dict):
                prev = {}

        # F7: a same-date re-run's --deposit/--withdraw is ADDITIONAL to the day's
        # earlier declarations; the mark states the resulting day total outright.
        # D3: repeating a flow already declared today is refused unless the caller
        # says it is a second, separate transfer (--additional-flow).
        if same_date and abs(flow) > 1e-9 and not additional_flow:
            tol = _flow_dup_tol(flow)
            prior = [S._mark_flow(m) for m in same_date] + [S.day_flow_total(same_date)]
            hit = next((x for x in prior if abs(x) > 1e-9 and abs(x - flow) <= tol + 1e-9), None)
            if hit is not None:
                raise DuplicateFlowError(
                    f"this re-log declares {flow:+.2f} USD but {hit:+.2f} was already declared "
                    f"for {today} (tolerance ±{tol:.2f}); a re-log keeps the day's earlier "
                    f"declarations, so omit --deposit/--withdraw — or pass --additional-flow if "
                    f"this is a second, separate transfer. Nothing appended.")
        day_total = round(S.day_flow_total(same_date) + flow, 2)
        implied = _implied_flow(recs, today, baseline)
        unexplained = None
        if implied is not None and abs(implied - day_total) > C.UNEXPLAINED_FLOW_WARN_PCT * float(p["total_usd"]):
            unexplained = round(implied - day_total, 2)
            sys.stderr.write(f"WARNING: unexplained external flow {unexplained:+.2f} USD "
                             f"(implied {implied:+.2f}, declared {day_total:+.2f}); "
                             f"use --deposit/--withdraw or --adjust-flow\n")

        lines = [{
            "type": "portfolio_mark", "date": today, "regime": regime,
            "total_usd": p["total_usd"], "cash_usd": p["cash_usd"], "cash_pct": p.get("cash_pct"),
            "positions": p.get("positions", {}), "deposit_usd": deposit_usd,
            "baseline_cum_return_pct": _mark_baseline_nav(baseline, prev),
            "ticker": None, "action": "MARK",
            "schema": "mark/v5.1",
            "external_flow_usd": flow, "withdraw_usd": withdraw_usd,
            "day_flow_total_usd": day_total,
            "implied_flow_usd": implied,
            "last_bar": (baseline.get("data") or {}).get("last_bar"),
            "benchmark_px": _core_px(baseline),
            "baseline_eq_pct": _post_equity(baseline),
            "core_pct": _core_pct(p),
            "execution_residual": residual,
            "unexplained_flow_usd": unexplained,
            "supersedes_same_date": bool(same_date),
            "relog_reason": relog_reason or None,
        }]
        if not orders:
            # A no-op is a decision and it gets logged like one. "Nothing happened"
            # was 84% of v4's runs and none of it was ever scored.
            lines.append({
                "type": "noop", "date": today, "regime": regime,
                "ticker": C.CORE_TICKER, "action": "NO_OP",
                "reason": (residual or {}).get("reason") or "within drift band; baseline confirmed",
            })
        for o in orders:
            lines.append({
                "type": "order", "date": today, "regime": regime,
                "ticker": o["ticker"], "action": o["action"], "usd": o.get("usd"),
                "shares": o.get("shares"), "price": o.get("limit_ref_price"),
                "stop": o.get("stop"), "source": final.get("source", "BASELINE"),
                "sleeve": o.get("sleeve") or ("core" if o["ticker"] in C.CORE_TICKERS else "satellite"),
                "whole_shares": o.get("whole_shares"),
            })
        if ovr:
            # F4: both post-trade equity weights are persisted so the scorer can grade
            # spread_pp = (realised − baseline)/100 × QQQ fwd return over the horizon.
            lines.append({
                "type": "override", "date": today, "regime": regime,
                "ticker": ovr.get("ticker", C.CORE_TICKER), "action": "OVERRIDE",
                "direction": ovr.get("direction"),
                "catalyst_url": ovr.get("catalyst_url"),
                "catalyst_timestamp": ovr.get("catalyst_timestamp"),
                "expected_cost_if_wrong_pct": ovr.get("expected_cost_if_wrong_pct"),
                "qqq_forward_20d_if_i_am_wrong": ovr.get("qqq_forward_20d_if_i_am_wrong"),
                "expires_after_trading_days": C.OVERRIDE_EXPIRY_TRADING_DAYS,
                "baseline_equity_pct": _post_equity(baseline),
                "realised_equity_pct": _post_equity(final),
                "spread_vs_baseline_pp": None,   # APPENDED later as an override_score line
            })
        if rej:
            # a FAILed override is logged, never omitted — and never counted as an
            # executed override (no spread, no suspension counter).
            ro = rej.get("override") if isinstance(rej.get("override"), dict) else {}
            lines.append({
                "type": "override_rejected", "date": today, "regime": regime,
                "ticker": ro.get("ticker", C.CORE_TICKER), "action": "OVERRIDE_REJECTED",
                "direction": ro.get("direction"),
                "override": rej.get("override"),
                "proposed_allocation": rej.get("proposed_allocation"),
                "violations": rej.get("violations") or [],
            })
        for v in vetoes:
            lines.append({
                "type": "veto", "date": today, "regime": regime,
                "ticker": v.get("ticker"), "action": "VETO",
                "event": v.get("event"), "url": v.get("url"), "event_date": v.get("event_date"),
            })
        # paper satellite track: validate.py already accepted + enriched these with
        # script levels (C7). Logged so they are graded; never an order, never money.
        for sp in picks:
            rec = {"type": "shadow_pick", "date": today, "regime": regime}
            rec.update({k: v for k, v in sp.items() if k not in
                        ("type", "date", "regime", "action", "real_money", "logged_utc", "prev_hash",
                         "ledger_tamper", "ledger_tamper_reason")})
            rec["action"] = "SHADOW_LONG"
            rec["real_money"] = False
            lines.append(rec)

        # structured continuity only
        flip_month = (baseline["regime"].get("eval_month")
                      if prev.get("regime") not in (None, regime)
                      else prev.get("regime_flip_month"))
        payload = {
            "date": today,
            "regime": regime,
            "regime_flip_month": flip_month,
            "positions": p.get("positions", {}),
            "cash_usd": p["cash_usd"],
            "targets": baseline.get("targets"),
            "active_override": ovr or None,
            "satellite_names": (baseline.get("satellite") or {}).get("names", []),
            # the ATR stops MUST persist: core.py --weekly compares today's price to
            # the PRIOR run's stop. An unpersisted stop can never be breached, which
            # would make the only stop in the system decorative.
            "satellite_stops_atr": (baseline.get("satellite") or {}).get("stops_atr", {}),
            "baseline_nav": prev.get("baseline_nav"),
            "baseline_cum_return_pct": prev.get("baseline_cum_return_pct"),
            "core_tickers": list(C.CORE_TICKERS),
            "prices": baseline.get("prices", {}),
            "execution_residual": residual,
        }
        # EXTERNAL ledger anchor (tip hash + line count) + sticky tamper fields are
        # added by commit_locked AFTER the appends, inside the same lock.
        st = S.commit_locked(f, C.RECS_JSONL, anchor, lines, payload)
    if not st["intact"]:
        sys.stderr.write(f"WARNING: ledger tamper evidence ({st['reason'] or 'sticky window'}); "
                         f"override privileges suspended until {st['sticky_until']}\n")
    return len(lines)


def _flow_dup_tol(flow):
    return max(C.ADJ_DUP_ABS_USD, C.ADJ_DUP_REL * abs(flow))


def adjust_flow(effective_date, flow_usd, flow_krw=None, basis="ledger_implied", reason="", force=False):
    """Correct a PAST external flow by APPENDING one cash_flow_adjustment line. It
    can never be removed, only offset by another adjustment. Every one is listed in
    the header with its reconciliation verdict (입출금_내역): a declared flow the
    ledger does not corroborate is `verified: false`, excluded from the
    conservative return the kill trigger reads."""
    try:
        eff = _date.fromisoformat(str(effective_date)).isoformat()
    except ValueError:
        raise AdjustmentRefused(f"bad --effective-date {effective_date!r} (YYYY-MM-DD)")
    try:
        flow = round(float(flow_usd), 2)
    except (TypeError, ValueError):
        raise AdjustmentRefused(f"bad --flow-usd {flow_usd!r}")
    if flow == 0.0 or flow != flow:
        raise AdjustmentRefused("--flow-usd must be a non-zero number")
    if basis not in ("ledger_implied", "declared"):
        raise AdjustmentRefused(f"--basis must be ledger_implied|declared, got {basis!r}")
    if not str(reason or "").strip():
        raise AdjustmentRefused("--reason is required")
    anchor = S._anchor_path(C.RECS_JSONL)
    with S.ledger_lock(C.RECS_JSONL) as f:
        st = S.ledger_status(C.RECS_JSONL, anchor)
        if not st["intact"]:
            raise AdjustmentRefused(
                f"ledger tamper evidence ({st['reason'] or 'sticky window until ' + str(st['sticky_until'])}); "
                f"refusing to append an adjustment", EXIT_LEDGER_TAMPER)
        recs = S.load_recs(C.RECS_JSONL)
        mark_dates = sorted({str(r.get("date")) for r in recs
                             if r.get("type") == "portfolio_mark" and r.get("date")})
        recv = next((d for d in mark_dates if d >= eff), None)
        if recv is None:
            raise AdjustmentRefused(f"no portfolio_mark dated on/after {eff}; nothing receives the flow")
        if recv == mark_dates[0]:
            raise AdjustmentRefused(
                f"--effective-date {eff} is on/before the first portfolio_mark ({mark_dates[0]}): "
                f"a flow before the first valuation has no effect on the time-weighted return; "
                f"nothing to correct")
        tol = _flow_dup_tol(flow)
        dup = [r for r in recs if r.get("type") == "cash_flow_adjustment"
               and r.get("effective_date") == eff
               and abs(round(float(r.get("flow_usd") or 0), 2) - flow) <= tol + 1e-9]
        if not dup and recv == eff:
            day = S.day_flow_total([r for r in recs if r.get("type") == "portfolio_mark"
                                    and str(r.get("date")) == recv])
            if abs(day - flow) <= tol + 1e-9:
                dup = [{"declared_on_mark": recv, "flow_usd": day}]
        if dup and not force:
            raise AdjustmentRefused(
                f"an identical or near-identical flow ({eff}, {flow:+.2f}, tolerance ±{tol:.2f}) is "
                f"already on file; use --force-relog only if a second one is genuinely intended",
                EXIT_DUPLICATE)
        rec = {"type": "cash_flow_adjustment", "date": _today(), "effective_date": eff,
               "flow_usd": flow, "flow_krw": flow_krw, "basis": basis, "reason": reason,
               "ticker": None, "action": "ADJUST"}
        S.commit_locked(f, C.RECS_JSONL, anchor, [rec])
    entries, _ = S.reconcile_flows(S.load_recs(C.RECS_JSONL))
    mine = next((e for e in reversed(entries) if e["kind"] == "adjustment"
                 and e.get("logged_utc") == rec.get("logged_utc")), {})
    sys.stderr.write(f"cash_flow_adjustment appended: {eff} {flow:+.2f} USD -> {C.RECS_JSONL}\n"
                     f"reconciliation: verified={mine.get('verified')} "
                     f"implied={mine.get('implied_usd')} deviation={mine.get('deviation_usd')} "
                     f"{mine.get('note') or ''}\n")
    return mine


def halt(reason, stage="phase0"):
    """E11: a halt is never silent. A yfinance flake on the month's ONLY run must
    not become an unlogged zero-entry month — 'nothing happened' being invisible
    was 84% of v4's runs. Anchored, so the next run does not read it as tamper."""
    S.append_anchored(C.RECS_JSONL, {
        "type": "pipeline_halt", "date": _today(),
        "stage": stage, "reason": reason, "ticker": None, "action": "HALT",
    }, C.LAST_RUN_JSON)
    sys.stderr.write(f"pipeline_halt logged: {stage}: {reason}\n")
    return 1


def repair_torn_tail(reason):
    """report.py --repair-torn-tail: see score_recs.repair_torn_tail. The ONE
    documented exception to append-only, and only for an unparseable final line."""
    rec, st = S.repair_torn_tail(C.RECS_JSONL, C.LAST_RUN_JSON, reason)
    after = S.ledger_status(C.RECS_JSONL, C.LAST_RUN_JSON)
    sys.stderr.write(f"ledger_repair appended: removed {rec['removed_bytes']} bytes "
                     f"(sha256 {rec['removed_sha256'][:12]}..., copy state/{rec['removed_copy']}): "
                     f"{rec['why']}\n"
                     f"ledger now: intact={after['intact']} reason={after['reason']}\n")
    return rec, after


def _flow_row(e):
    return {k: e.get(k) for k in ("kind", "effective_date", "receiving_mark", "amount_usd",
                                  "reason", "verified", "implied_usd", "deviation_usd", "note")}


def header(prices_csv=None):
    cum = S.cumulative(C.RECS_JSONL, prices_csv) or {}
    tr = S.track_record(C.RECS_JSONL, C.SCORECARD_CSV, prices_csv) if os.path.exists(C.RECS_JSONL) else {}
    sh = tr.get("shadow") or {}
    res = cum.get("last_residual")
    unavoidable = bool(res and res.get("unavoidable"))
    lst = S.ledger_status(C.RECS_JSONL, C.LAST_RUN_JSON)
    price_ok = cum.get("price_data_ok")
    if tr and price_ok is not None:
        price_ok = bool(price_ok and tr.get("price_data_ok", True))
    kill, board = cum.get("kill_trigger_armed"), cum.get("board_reconvene_armed")
    if price_ok is False:
        kill, board = "unknown", "unknown"         # D2: any price failure -> unknown
    return {
        "누적_수익률_pct": cum.get("actual_cum_pct"),
        "누적_수익률_검증입출금만_pct": cum.get("actual_cum_pct_verified_flows"),
        "누적_수익률_보수적_pct": cum.get("actual_cum_pct_conservative"),
        "QQQ_누적_pct": cum.get("qqq_cum_pct"),
        "vs_QQQ_pp": cum.get("vs_qqq_pp"),
        "기계적_베이스라인_누적_pct": cum.get("mechanical_baseline_cum_pct"),
        "기계적_베이스라인_누적_TR상한_pct": cum.get("mechanical_baseline_cum_pct_tr_bound"),
        "배당_기준차_pp": cum.get("dividend_basis_gap_pp"),
        "LLM_레이어_기여_pp": cum.get("llm_layer_spread_pp"),
        "LLM_레이어_기여_보수적_pp": cum.get("llm_layer_spread_pp_conservative"),
        "적중률_vs_QQQ": tr.get("hit_rate_vs_qqq"),
        "다트판_기준선": C.DARTBOARD_BASE_RATE,
        "가격_데이터_정상": price_ok,
        "킬_트리거_발동": kill,
        "오버라이드_권한_정지": cum.get("override_privileges_at_risk"),
        "오버라이드_적중률": cum.get("override_hit_rate"),
        "오버라이드_미채점_건수": cum.get("overrides_ungraded"),
        "오버라이드_거부_건수": (cum["overrides_rejected"] if "overrides_rejected" in cum else
                            sum(1 for r in S.load_recs(C.RECS_JSONL)
                                if r.get("type") == "override_rejected")),
        "보드_재소집": board,
        "새틀라이트_확대_가능": tr.get("expand_satellite_ok"),
        "원장_변조_감지": not lst["intact"],
        "원장_변조_최초감지": lst["tamper_since"],
        "원장_변조_정지_해제": lst["sticky_until"],
        "원장_변조_사유": lst["reason"],
        "원장_꼬리_손상": lst.get("torn_tail"),
        "원장_미완료_커밋_복구대기": lst.get("pending_commit"),
        "원장_복구_이력": S.repairs(S.load_recs(C.RECS_JSONL)),
        "양도세_공제한도_KRW": C.CGT_ALLOWANCE_KRW,
        "기준일": cum.get("since"),
        "수익률_방식": "TWR(외부 입출금 제외)",
        "외부_입출금_순액_USD": cum.get("net_external_flow_usd"),
        "입출금_보정_건수": cum.get("flow_adjustments"),
        "입출금_내역": [_flow_row(e) for e in cum.get("flows", [])],
        "미검증_입출금_건수": cum.get("unverified_flows"),
        "누적_손익_USD": cum.get("pnl_usd"),
        "중복_기록_제외_건수": cum.get("superseded_runs"),
        "중복_기록_제외_라인": cum.get("superseded_lines", []),
        "미확인_입출금_의심": cum.get("unexplained_flow_marks", []),
        "불가피_잔여현금_USD": (res.get("excess_cash_usd") if unavoidable else 0.0) if res else None,
        "불가피_잔여현금_pct": (res.get("excess_cash_pct") if unavoidable else 0.0) if res else None,
        "섀도우_적중률_vs_QQQ": sh.get("hit_rate"),
        "섀도우_표본수": f"{sh.get('n_graded', 0)}/{sh.get('n_total', 0)}",
        "섀도우_미성숙_건수": sh.get("n_unmatured"),
        "섀도우_평균초과수익_20d_pp": sh.get("mean_excess_20d_pp"),
        "실전_승격_가능": sh.get("graduation_ok", False),
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
    ap.add_argument("--withdraw", type=float, default=0.0)
    ap.add_argument("--additional-flow", action="store_true",
                    help="--log --force-relog: this --deposit/--withdraw is a SECOND transfer, "
                         "not a repeat of one already declared today (D3)")
    ap.add_argument("--repair-torn-tail", action="store_true",
                    help="drop ONLY an unparseable/unterminated final ledger line (copy kept "
                         "in state/, ledger_repair line appended); needs --reason")
    ap.add_argument("--force-relog", action="store_true",
                    help="--log: supersede today's earlier run (needs --reason); "
                         "--adjust-flow: allow an identical/near-identical repeat")
    ap.add_argument("--adjust-flow", action="store_true",
                    help="append a cash_flow_adjustment line (past flows are corrected by appending)")
    ap.add_argument("--effective-date")
    ap.add_argument("--flow-usd", type=float)
    ap.add_argument("--flow-krw", type=float)
    ap.add_argument("--basis", default="ledger_implied", choices=["ledger_implied", "declared"])
    ap.add_argument("--reason", default="",
                    help="--adjust-flow: required; --log --force-relog: required, stored on the mark")
    ap.add_argument("--prices-csv")
    a = ap.parse_args()
    try:
        _main(a)
    except OSError as e:
        sys.stderr.write(f"WRITE FAILED ({type(e).__name__}: {e}). If the ledger line(s) landed "
                         "before the failure, the pending intent (state/ledger_pending.json) lets "
                         "the next write complete the commit; otherwise nothing was appended.\n")
        sys.exit(EXIT_WRITE_FAILED)


def _main(a):
    if a.repair_torn_tail:
        try:
            repair_torn_tail(a.reason)
        except S.RepairRefused as e:
            sys.stderr.write(f"REFUSED (nothing touched): {e}\n")
            sys.exit(EXIT_REFUSED)
        return
    if a.halt:
        try:
            halt(a.halt, a.stage)
        except (S.LedgerTornTail, S.LedgerUnreadable) as e:
            sys.stderr.write(f"HALT NOT LOGGED: {e}\n")
            sys.exit(EXIT_LEDGER_TAMPER)
        return
    if a.adjust_flow:
        if not a.effective_date or a.flow_usd is None:
            sys.stderr.write("ERROR: --adjust-flow needs --effective-date and --flow-usd\n")
            sys.exit(EXIT_REFUSED)
        try:
            adjust_flow(a.effective_date, a.flow_usd, a.flow_krw, a.basis, a.reason, a.force_relog)
        except AdjustmentRefused as e:
            sys.stderr.write(f"REFUSED: {e}\n")
            sys.exit(e.code)
        except (S.LedgerTornTail, S.LedgerUnreadable) as e:
            sys.stderr.write(f"REFUSED (nothing appended): {e}\n")
            sys.exit(EXIT_LEDGER_TAMPER)
    if a.log:
        if not (a.baseline and a.final):
            sys.stderr.write("ERROR: --log needs --baseline and --final\n")
            sys.exit(EXIT_REFUSED)
        if a.deposit < 0 or a.withdraw < 0:
            sys.stderr.write("ERROR: --deposit/--withdraw are non-negative amounts\n")
            sys.exit(EXIT_REFUSED)
        try:
            with open(a.baseline, encoding="utf-8") as fb, open(a.final, encoding="utf-8") as ff:
                baseline, final = json.load(fb), json.load(ff)
        except (OSError, ValueError) as e:
            sys.stderr.write(f"REFUSED (nothing appended): cannot read --baseline/--final ({e})\n")
            sys.exit(EXIT_REFUSED)
        try:
            n = append_run(baseline, final, a.deposit, a.prices_csv, a.withdraw,
                           a.force_relog, a.reason, a.additional_flow)
        except DuplicateRunError as e:
            sys.stderr.write(f"DUPLICATE RUN: {e}\n")
            sys.exit(EXIT_DUPLICATE)
        except DuplicateFlowError as e:
            sys.stderr.write(f"DUPLICATE FLOW: {e}\n")
            sys.exit(EXIT_DUPLICATE)
        except (S.LedgerTornTail, S.LedgerUnreadable) as e:
            sys.stderr.write(f"REFUSED (nothing appended): {e}\n")
            sys.exit(EXIT_LEDGER_TAMPER)
        except (RunRefused, ValueError) as e:
            sys.stderr.write(f"REFUSED (nothing appended): {e}\n")
            sys.exit(EXIT_REFUSED)
        sys.stderr.write(f"appended {n} lines -> {C.RECS_JSONL}\n")
    if a.header:
        print(json.dumps(header(a.prices_csv), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
