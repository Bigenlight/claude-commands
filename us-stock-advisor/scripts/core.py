#!/usr/bin/env python3
"""us-stock-advisor v5 — Phase 0 DETERMINISTIC CORE. The authority.

Install path: ~/.claude/skills/us-stock-advisor/scripts/core.py

This script owns 100% of the money-touching numbers: regime, target weights,
rebalance orders, whole-share counts (combined QQQ+QQQM core sleeve), satellite
stops, script-owned ATR levels for the shadow track, earnings blackouts.
No LLM token enters any number it emits. Its output, baseline_plan.json, is
ALREADY APPROVED when Phase 2 opens.

Usage
-----
  # live (yfinance)
  python3 core.py --portfolio portfolio.json --out baseline_plan.json

  # offline / backtest / CI (adjusted-close CSV: Date,QQQ,SPY,...)
  python3 core.py --prices-csv prices_daily.csv --portfolio portfolio.json --out /dev/stdout
  python3 core.py --prices-csv prices_daily.csv --backtest 2026-04-21:2026-07-10

portfolio.json
--------------
  {"cash_usd": 504.0, "positions": [{"ticker":"QQQ","shares":1.0,"cost_basis":724.08}]}

Exit codes: 0 ok · 2 bad input · 3 data failure (pipeline MUST halt; a failed
core means there is no approved plan and therefore nothing for Phase 2 to do).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402


# --------------------------------------------------------------------- data
def _load_csv(path):
    import pandas as pd
    df = pd.read_csv(path, parse_dates=["Date"]).set_index("Date").sort_index()
    return df


def _load_yf(tickers, period="6y", fatal=True):
    """fatal=False (satellite-universe levels): raise RuntimeError instead of
    exiting, so a failed universe download can never kill the core plan."""
    try:
        import yfinance as yf
        import pandas as pd
    except ModuleNotFoundError:
        if not fatal:
            raise RuntimeError("yfinance/pandas not installed")
        sys.stderr.write("ERROR: pip install --user yfinance pandas\n")
        sys.exit(3)
    data = yf.download(
        list(dict.fromkeys(tickers)), period=period, interval="1d",
        auto_adjust=C.AUTO_ADJUST, progress=False, group_by="column",
    )
    if data is None or len(data) == 0:
        if not fatal:
            raise RuntimeError("yfinance returned no data")
        sys.stderr.write("ERROR: yfinance returned no data\n")
        sys.exit(3)
    close = data["Close"] if "Close" in data else data
    if hasattr(close, "to_frame"):
        try:
            close = close.to_frame(tickers[0])
        except Exception:
            pass
    high = data["High"] if "High" in data else None
    low = data["Low"] if "Low" in data else None
    return close, high, low


def drop_partial_bar(df):
    """A1/A4: today's in-progress bar is NOT a close. Drop it, always, before
    any indicator touches it. This is what made data_age_hours go negative and
    relative_volume structurally < 1 in v4."""
    if not C.DROP_PARTIAL_BAR or len(df) == 0:
        return df, None
    last = df.index[-1]
    today_utc = datetime.now(timezone.utc).date()
    # US close is 20:00Z (EDT) / 21:00Z (EST). Treat any bar dated today, or a
    # bar dated yesterday before 21:00Z hasn't settled either — be conservative.
    if last.date() >= today_utc:
        return df.iloc[:-1], str(last.date())
    return df, None


def data_age_hours(last_bar_date):
    """A4: NON-NEGATIVE by construction. A negative age is a hard abort in
    validate.py, not a value that vacuously passes a staleness check."""
    from datetime import time as _t, datetime as _dt
    close_utc = _dt.combine(last_bar_date, _t(21, 0), tzinfo=timezone.utc)  # 21:00Z upper bound
    age = (datetime.now(timezone.utc) - close_utc).total_seconds() / 3600.0
    return age


# ------------------------------------------------------------------- regime
def monthly_closes(close_series):
    """COMPLETED months only. The running month's partial bucket must never
    reach the SMA — evaluating the regime on an intramonth print is the 7/08
    whipsaw reinstated mechanically (config.REGIME_EVAL = 'monthly_close')."""
    if C.REGIME_EVAL != "monthly_close":
        sys.stderr.write(f"FATAL: config.REGIME_EVAL={C.REGIME_EVAL!r}; only "
                         "'monthly_close' is a legal regime evaluation basis\n")
        sys.exit(2)
    m = close_series.resample("ME").last().dropna()
    if len(m) and m.index[-1].to_period("M") >= close_series.index[-1].to_period("M"):
        m = m.iloc[:-1]
    return m


def compute_regime(close_series, prev_regime=None, prev_flip_month=None):
    """THE regime. One term: QQQ monthly close vs its 10-month SMA (Faber).

    Deleted in v5: macro-fear headlines, VIX prose, geopolitics, RISK_ON/NEUTRAL/
    RISK_OFF. v4's label was anti-predictive (RISK_OFF -> QQQ up 6/9) because it
    was 5-day-lagged momentum in a macro costume wired to the sizing dial.
    """
    m = monthly_closes(close_series)
    if len(m) < C.REGIME_SMA_MONTHS + 1:
        # not enough history to run the overlay -> default to being invested.
        return {
            "regime": "TREND",
            "reason": f"insufficient monthly history ({len(m)} mo); default = invested",
            "monthly_close": float(m.iloc[-1]) if len(m) else None,
            "sma_10m": None,
            "eval_month": str(m.index[-1].date()) if len(m) else None,
            "flip_suppressed": False,
        }
    sma = m.rolling(C.REGIME_SMA_MONTHS).mean()
    last_close = float(m.iloc[-1])
    last_sma = float(sma.iloc[-1])
    raw = "TREND" if last_close > last_sma else "DEFENSIVE"

    eval_month = str(m.index[-1].date())
    flip_suppressed = False
    regime = raw
    # whipsaw guard (c): config.MIN_MONTHS_BETWEEN_REGIME_FLIPS between flips
    if prev_regime and raw != prev_regime and prev_flip_month:
        import pandas as pd
        gap = (pd.Period(eval_month[:7], "M") - pd.Period(str(prev_flip_month)[:7], "M")).n
        if gap < C.MIN_MONTHS_BETWEEN_REGIME_FLIPS:
            regime = prev_regime
            flip_suppressed = True
    return {
        "regime": regime,
        "raw_regime": raw,
        "reason": f"QQQ monthly close {last_close:.2f} "
                  f"{'>' if last_close > last_sma else '<'} 10mo SMA {last_sma:.2f}",
        "monthly_close": round(last_close, 2),
        "sma_10m": round(last_sma, 2),
        "eval_month": eval_month,
        "flip_suppressed": flip_suppressed,
    }


# ------------------------------------------------------------- indicators
def _atr(high, low, close, period=14):
    prev = close.shift(1)
    tr = (high - low).combine((high - prev).abs(), max).combine((low - prev).abs(), max).dropna()
    if len(tr) < period:
        return None
    a = tr.iloc[:period].mean()
    for v in tr.iloc[period:]:
        a = (a * (period - 1) + v) / period
    return float(a)


def _rsi(close, period=14):
    d = close.diff().dropna()
    if len(d) < period + 1:
        return None
    g = d.clip(lower=0.0)
    l = (-d).clip(lower=0.0)
    ag, al = g.iloc[:period].mean(), l.iloc[:period].mean()
    for x, y in zip(g.iloc[period:], l.iloc[period:]):
        ag = (ag * (period - 1) + x) / period
        al = (al * (period - 1) + y) / period
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + ag / al)


def earnings_days(ticker):
    """A7: the earnings blackout fires off a SCRIPT DATE, not a headline.
    EARNINGS_BINARY was v4's single best veto class (-6.50pp) — keep the effect,
    fix the source."""
    try:
        import yfinance as yf
        cal = yf.Ticker(ticker).get_earnings_dates(limit=8)
        if cal is None or len(cal) == 0:
            return None
        import pandas as pd
        now = pd.Timestamp.now(tz=cal.index.tz)
        fut = [d for d in cal.index if d > now]
        if not fut:
            return None
        return int((min(fut) - now).days)
    except Exception:
        return None


# ------------------------------------------------------------------ orders
def _shares(usd, px):
    """config.FRACTIONAL_SHARES owns share granularity for NON-CORE lines (satellite
    budget trim, derived satellite orders). The broker cannot buy fractional shares,
    so the default is floor(usd/px) whole shares; a 0 result means "no order".
    Core lines are sized by plan_core_orders, not here."""
    if C.FRACTIONAL_SHARES:
        return round(usd / px, 4)
    return float(math.floor(usd / px + 1e-9))


def _whole():
    return not C.FRACTIONAL_SHARES


def _buf():
    return C.WHOLE_SHARE_PRICE_BUFFER_PCT


def atr_levels(price, atr):
    """Script-owned bracket for a satellite / shadow name. The LLM never writes any
    of these numbers. stop = px - SATELLITE_STOP_ATR_MULT*ATR, target = px +
    SHADOW_TARGET_ATR_MULT*ATR, so rr is a CONSTANT bracket ratio by construction
    (a definition, not a forecast). None when there is no ATR."""
    try:
        if not atr or not price:
            return None
        price, atr = float(price), float(atr)
    except (TypeError, ValueError):
        return None
    if atr != atr or price != price or atr <= 0 or price <= 0:
        return None
    stop = price - C.SATELLITE_STOP_ATR_MULT * atr
    target = price + C.SHADOW_TARGET_ATR_MULT * atr
    risk = price - stop
    return {"price": round(price, 2), "atr_14": round(atr, 2), "stop": round(stop, 2),
            "target": round(target, 2),
            "rr": round((target - price) / risk, 2) if risk > 0 else None}


def _core_prices(prices):
    out = {}
    for t in C.CORE_TICKERS:
        try:
            v = float((prices or {}).get(t) or 0.0)
        except (TypeError, ValueError):
            continue
        if v > 0 and v == v:
            out[t] = v
    return out


def min_core_lot_usd(prices):
    """Cheapest whole core share incl. the price buffer (None if no core price)."""
    cp = _core_prices(prices)
    if not cp:
        return None
    if C.FRACTIONAL_SHARES:
        return float(C.MIN_ORDER_USD)
    return min(px * (1 + _buf()) for px in cp.values())


def deploy_threshold_usd(prices):
    """Smallest excess cash that the core planner can actually turn into an order:
    one cheapest whole core share incl. the buffer, and never below
    config.MIN_ORDER_USD (an order line under it is not emitted, so a cheaper share
    is not deployable). None if no core price. Used by cash_events, the residual
    block and the SELL search, so 'deployable' means the same thing everywhere."""
    cp = _core_prices(prices)
    if not cp:
        return None
    if C.FRACTIONAL_SHARES:
        return float(C.MIN_ORDER_USD)
    # RT1-9: the smallest EMITTABLE line per core ticker — enough whole shares that
    # the line reaches MIN_ORDER_USD (one share for any share above it) — plus the
    # buffer. A share cheaper than MIN_ORDER_USD is not deployable one at a time.
    best = None
    for px in cp.values():
        pxr = round(px, 2)
        if pxr <= 0:
            continue
        n = max(1, int(math.ceil(C.MIN_ORDER_USD / pxr - 1e-9)))
        while round(n * pxr, 2) < C.MIN_ORDER_USD:
            n += 1
        cost = n * pxr * (1 + _buf())
        best = cost if best is None else min(best, cost)
    return None if best is None else max(best, float(C.MIN_ORDER_USD))


def _core_order(t, action, shares, px, reason, whole):
    lrp = round(px, 2)
    return {"ticker": t, "action": action, "usd": round(shares * lrp, 2),
            "shares": float(shares), "limit_ref_price": lrp, "reason": reason,
            "stop": None,        # THE CORE CARRIES NO STOP. By design.
            "pre_approved": True, "sleeve": "core", "whole_shares": whole}


def _has_sub_min_line(combo, tks, cp):
    """True iff a non-zero line of this whole-share combo would be under
    config.MIN_ORDER_USD. Such a line is dropped at emission, so a combo containing
    one is not the plan it claims to be (RT1-9: a core share cheaper than
    MIN_ORDER_USD made the search pick a lone sub-minimum line -> no order while
    cash_over_max fired)."""
    return any(n > 0 and round(n * cp[t], 2) < C.MIN_ORDER_USD for n, t in zip(combo, tks))


def drift_warning(execution, orders, weekly=False):
    """N6: the post-trade core drift still exceeds the band and the plan places NO
    core order (e.g. DEFENSIVE with 1 QQQ: half a share cannot be sold). Silence
    here would read as "de-risked" when nothing happened. None when not the case."""
    after = (execution or {}).get("core_drift_pp_after")
    if weekly or after is None or abs(after) <= C.REBALANCE_DRIFT_BAND_PCT * 100 + 1e-9:
        return None
    if any(str(o.get("ticker", "")).upper() in C.CORE_TICKERS for o in orders or []):
        return None
    return (f"core drift after trades {after:+.1f}pp still exceeds the "
            f"{C.REBALANCE_DRIFT_BAND_PCT * 100:.0f}pp band but no whole-share core order can "
            f"be placed (lot size / cash / MIN_ORDER_USD); the book stays at "
            f"{(execution.get('post_trade') or {}).get('equity_pct')}% equity")


def plan_core_orders(delta_usd, total_usd, held_shares, prices, spendable_usd, reason_prefix=""):
    """THE core order planner (D1), used by build_plan and derive_execution alike.

    delta_usd = core_target_value - core_value over the COMBINED core sleeve
    (config.CORE_TICKERS). Pure. Returns 0..len(CORE_TICKERS) order dicts, all of
    the same sign as delta_usd: there is no code path that sells one core ticker to
    buy the other. Which core ticker ends up held is path-dependent; accepted.

      BUY : integer share counts whose cost incl. config.WHOLE_SHARE_PRICE_BUFFER_PCT
            fits `spendable` (the buffer is a CASH guard: fees/FX/overnight gap) and
            whose cost at limit_ref_price does not exceed delta by more than the
            allocation display tolerance (config.CONFIRM_ALLOCATION_TOLERANCE_PP of
            total, so a 1dp-rounded target such as the baseline's own post-trade
            split re-derives the same lot); maximise deployed USD (ties within
            config.MIN_ORDER_USD -> fewer lines, then CORE_TICKERS order). Never
            exceeds spendable cash.
      SELL: whole shares (<= floor(held)) whose proceeds are nearest |delta|, among
            the candidates the NEXT run would not buy back: the oversell (proceeds −
            |delta|) must stay below deploy_threshold_usd (else cash_over_max fires)
            and must not exceed the drift band while a core share fits it (else the
            band re-buys). Either would be a two-run QQQ -> QQQM conversion, so the
            undersell is preferred among surviving candidates (the book can land
            above or below target by up to one core lot).
    """
    total = float(total_usd or 0.0)
    d = float(delta_usd or 0.0)
    if total <= 0 or abs(d) / total <= C.REBALANCE_DRIFT_BAND_PCT:
        return []
    drift_txt = (f"{reason_prefix}drift {d / total * 100:+.1f}pp > "
                 f"{C.REBALANCE_DRIFT_BAND_PCT * 100:.0f}pp band")
    cp = _core_prices(prices)
    if not cp:
        return []

    if C.FRACTIONAL_SHARES:
        # v5.0 behaviour: one fractional line on the primary core ticker.
        t = C.CORE_TICKER if C.CORE_TICKER in cp else next(iter(cp))
        if abs(d) < C.MIN_ORDER_USD:
            return []
        px = cp[t]
        return [{"ticker": t, "action": "BUY" if d > 0 else "SELL",
                 "usd": round(abs(d), 2), "shares": round(abs(d) / px, 4),
                 "limit_ref_price": round(px, 2), "reason": drift_txt, "stop": None,
                 "pre_approved": True, "sleeve": "core", "whole_shares": False}]

    tks = [t for t in C.CORE_TICKERS if t in cp]
    cp = {t: round(cp[t], 2) for t in tks}       # cost at the limit_ref_price the order carries
    held = {t: float((held_shares or {}).get(t, 0.0) or 0.0) for t in tks}
    buf = _buf()

    def _counts(ranges_head, last_opts):
        """product over all-but-last tickers x a few candidates for the last one."""
        import itertools
        for head in itertools.product(*ranges_head):
            for last in last_opts(head):
                yield head + (last,)

    if d > 0:
        cash_cap = max(float(spendable_usd or 0.0), 0.0)
        tgt_cap = d + C.CONFIRM_ALLOCATION_TOLERANCE_PP / 100.0 * total
        unit = {t: cp[t] * (1 + buf) for t in tks}
        cap = {t: int(math.floor(min(cash_cap / unit[t], tgt_cap / cp[t]) + 1e-9)) for t in tks}

        def last_opts(head):
            t = tks[-1]
            left_cash = cash_cap - sum(n * unit[x] for n, x in zip(head, tks[:-1]))
            left_tgt = tgt_cap - sum(n * cp[x] for n, x in zip(head, tks[:-1]))
            if left_cash < -1e-9 or left_tgt < -1e-9:
                return []
            return sorted({0, int(math.floor(min(left_cash / unit[t], left_tgt / cp[t]) + 1e-9))})

        cands = []
        for combo in _counts([range(cap[t] + 1) for t in tks[:-1]], last_opts):
            cost = sum(n * unit[t] for n, t in zip(combo, tks))
            dep = sum(n * cp[t] for n, t in zip(combo, tks))
            if cost > cash_cap + 1e-9 or dep > tgt_cap + 1e-9:
                continue
            if _has_sub_min_line(combo, tks, cp):
                continue          # RT1-9: a line under MIN_ORDER_USD is never emitted
            cands.append((dep, sum(1 for n in combo if n), combo))
        if not cands:
            return []
        best = max(c[0] for c in cands)
        if best <= 0:
            return []
        tied = [c for c in cands if c[0] >= best - C.MIN_ORDER_USD and c[0] > 0]
        # fewer lines, then CORE_TICKERS order (more of the earlier ticker), then more USD
        dep, _, combo = min(tied, key=lambda c: (c[1], tuple(-n for n in c[2]), -c[0]))
        out = []
        for n, t in zip(combo, tks):
            if n <= 0:
                continue
            o = _core_order(t, "BUY", n, cp[t], f"{drift_txt}; whole-share max-deploy", True)
            if o["usd"] >= C.MIN_ORDER_USD:
                out.append(o)
        return out

    need = -d
    cap = {t: int(math.floor(held[t] + 1e-9)) for t in tks}
    max_oversell = deploy_threshold_usd(cp)                      # == deploy threshold

    def last_opts(head):
        t = tks[-1]
        left = need - sum(n * cp[x] for n, x in zip(head, tks[:-1]))
        k = left / cp[t] if cp[t] else 0.0
        opts = {0, cap[t], int(math.floor(k)), int(math.ceil(k))}
        return sorted(n for n in opts if 0 <= n <= cap[t])

    best = None
    for combo in _counts([range(cap[t] + 1) for t in tks[:-1]], last_opts):
        if _has_sub_min_line(combo, tks, cp):
            continue              # RT1-9: a line under MIN_ORDER_USD is never emitted
        usd = sum(n * cp[t] for n, t in zip(combo, tks))
        over = usd - need
        if over >= max_oversell - 1e-9 or (
                over / total > C.REBALANCE_DRIFT_BAND_PCT
                and min(cp.values()) <= over + C.CONFIRM_ALLOCATION_TOLERANCE_PP / 100.0 * total):
            continue          # the next run would BUY core back (event or drift): no churn
        key = (round(abs(usd - need), 6), sum(1 for n in combo if n), round(usd, 6),
               tuple(-n for n in combo))
        if best is None or key < best[0]:
            best = (key, combo)
    if best is None:
        return []
    out = []
    for n, t in zip(best[1], tks):
        if n <= 0:
            continue
        o = _core_order(t, "SELL", n, cp[t], f"{drift_txt}; whole-share nearest", True)
        if o["usd"] >= C.MIN_ORDER_USD:
            out.append(o)
    return out


def post_trade_state(portfolio_block, orders, prices, target_cash_value, core_target_value=None):
    """Apply `orders` to baseline["portfolio"] at limit_ref_price (no buffer) and
    report the book they actually produce, incl. the unavoidable residual (D2).
    Returns the C3 `execution` block minus `warnings`."""
    p = portfolio_block
    total_pre = float(p.get("total_usd") or 0.0)
    cash = float(p.get("cash_usd") or 0.0)
    shares = {str(t).upper(): float(d.get("shares") or 0.0) for t, d in p.get("positions", {}).items()}
    px = {}
    for t, d in p.get("positions", {}).items():
        if d.get("shares"):
            px[str(t).upper()] = float(d["usd"]) / float(d["shares"])
    for t, v in (prices or {}).items():
        try:
            if v and float(v) > 0:
                px[str(t).upper()] = float(v)
        except (TypeError, ValueError):
            pass
    for o in orders or []:
        t = str(o["ticker"]).upper()
        q = float(o.get("shares") or 0.0)
        usd = float(o.get("usd") or 0.0)
        px.setdefault(t, float(o.get("limit_ref_price") or 0.0))
        if o["action"] == "BUY":
            shares[t] = shares.get(t, 0.0) + q
            cash -= usd
        else:
            shares[t] = max(shares.get(t, 0.0) - q, 0.0)
            cash += usd
    pos = {}
    for t, s in shares.items():
        if s > 1e-9:
            pos[t] = {"shares": round(s, 4), "usd": s * px.get(t, 0.0)}
    total = cash + sum(v["usd"] for v in pos.values())
    total = total if total > 0 else (total_pre or 1.0)
    core_usd = sum(v["usd"] for t, v in pos.items() if t in C.CORE_TICKERS)
    sat_usd = sum(v["usd"] for t, v in pos.items() if t not in C.CORE_TICKERS)
    for v in pos.values():
        v["pct"] = round(v["usd"] / total * 100, 1)
        v["usd"] = round(v["usd"], 2)

    lot = min_core_lot_usd(px)
    thr = deploy_threshold_usd(px)
    tcv = max(float(target_cash_value or 0.0), 0.0)
    excess = cash - tcv
    # The price buffer on this plan's own BUYs is reserved cash (fees/FX/overnight
    # gap), not idle cash: the BUY search spent against it, so measuring the
    # leftover without it would call a lot "deployable" that the planner could not
    # have bought. Measured this way, "deployable" is false after any whole-share BUY.
    reserve = (sum(float(o.get("usd") or 0.0) for o in orders or [] if o["action"] == "BUY")
               * (_buf() if _whole() else 0.0))
    usable = excess - reserve
    deployable = bool(thr is not None and usable >= thr)
    unavoidable = bool(excess > 0.005 and not deployable)
    drift_after = None
    if core_target_value is not None and total_pre:
        drift_after = (core_target_value - core_usd) / total_pre
    if unavoidable:
        if thr is None:
            reason = f"no core price: {excess:.2f} cannot be deployed"
        elif reserve > 0.005 and excess >= thr:
            reason = (f"excess cash {excess:.2f} is the {_buf() * 100:.1f}% price buffer "
                      f"({reserve:.2f}) on this plan's BUYs plus {usable:.2f} < "
                      f"deployable lot {thr:.2f}")
        else:
            reason = f"no whole core share fits: {excess:.2f} < deployable lot {thr:.2f}"
    elif deployable:
        if drift_after is not None and abs(drift_after) > C.REBALANCE_DRIFT_BAND_PCT:
            reason = (f"excess cash {usable:.2f} >= deployable lot {thr:.2f} and core drift "
                      f"{drift_after * 100:+.1f}pp exceeds the "
                      f"{C.REBALANCE_DRIFT_BAND_PCT * 100:.0f}pp band, but this plan places "
                      "no core BUY for it (e.g. weekly stop-check mode)")
        elif drift_after is not None:
            reason = (f"excess cash {usable:.2f} >= deployable lot {thr:.2f} but core drift "
                      f"{drift_after * 100:+.1f}pp is within the "
                      f"{C.REBALANCE_DRIFT_BAND_PCT * 100:.0f}pp band")
        else:
            reason = f"excess cash {usable:.2f} >= deployable lot {thr:.2f}"
    else:
        reason = None

    ex = {
        "whole_shares": _whole(),
        "share_price_buffer_pct": round(_buf() * 100, 2),
        "min_core_lot_usd": round(lot, 2) if lot is not None else None,
    }
    if core_target_value is not None and total_pre:
        core_pre = sum(float(d.get("usd") or 0.0) for t, d in p.get("positions", {}).items()
                       if str(t).upper() in C.CORE_TICKERS)
        ex["core_drift_pp_before"] = round((core_target_value - core_pre) / total_pre * 100, 1)
        ex["core_drift_pp_after"] = round((core_target_value - core_usd) / total_pre * 100, 1)
    ex["post_trade"] = {
        "positions": pos,
        "core_pct": round(core_usd / total * 100, 1),
        "satellite_pct": round(sat_usd / total * 100, 1),
        "equity_pct": round((core_usd + sat_usd) / total * 100, 1),
        "cash_usd": round(cash, 2),
        "cash_pct": round(cash / total * 100, 1),
    }
    ex["post_trade_allocation"] = {t: v["pct"] for t, v in pos.items()}
    ex["residual"] = {
        "cash_usd": round(cash, 2), "cash_pct": round(cash / total * 100, 1),
        "target_cash_pct": round(tcv / total * 100, 1),
        "excess_cash_usd": round(excess, 2), "excess_cash_pct": round(excess / total * 100, 1),
        "deployable": deployable, "unavoidable": unavoidable, "reason": reason,
    }
    return ex


def cash_events(portfolio_block, prices, target_cash_value):
    """D2 run triggers on the PRE-trade book. `cash_over_max` fires only when the
    excess over the regime's target cash is both > CASH_MAX_PCT and big enough to
    buy one whole core share; the unavoidable variant is informational only."""
    total = float(portfolio_block.get("total_usd") or 0.0)
    cash = float(portfolio_block.get("cash_usd") or 0.0)
    excess = cash - max(float(target_cash_value or 0.0), 0.0)
    thr = deploy_threshold_usd(prices)       # >= MIN_ORDER_USD: a firing event can order
    over = total > 0 and excess / total > C.CASH_MAX_PCT
    fires = bool(over and thr is not None and excess >= thr)
    return {"cash_over_max": fires, "cash_over_max_unavoidable": bool(over and not fires)}


def normalize_alloc(alloc):
    """Case-fold + de-duplicate allocation keys to canonical UPPER tickers, summing
    weights, coercing each to float. Returns (norm_dict, errors). This is the ONE
    place allocation numerics are parsed, so '{"QQQ":"ninety"}' becomes a listed
    SCHEMA_VIOLATION (not a raw ValueError crash) and '{"QQQ":34,"qqq":33,"Qqq":33}'
    collapses to a single QQQ:100 (not three separate QQQ orders)."""
    norm, errors = {}, []
    if not isinstance(alloc, dict):
        return norm, [f"final_allocation must be an object of {{ticker: weight}}, got {type(alloc).__name__}"]
    for k, x in alloc.items():
        tk = str(k).strip().upper()
        try:
            w = float(x)
        except (TypeError, ValueError):
            errors.append(f"NON_NUMERIC_WEIGHT: {k!r}={x!r} is not a number")
            continue
        if w != w or w in (float("inf"), float("-inf")):   # NaN/inf
            errors.append(f"NON_NUMERIC_WEIGHT: {k!r}={x!r} is not finite")
            continue
        norm[tk] = norm.get(tk, 0.0) + w
    return norm, errors


def collapse_alloc(norm_alloc):
    """{"CORE": sum over config.CORE_TICKERS, **every other ticker}. The QQQ/QQQM
    split is not an input anywhere: the LLM cannot steer it or force a conversion.
    A literal "CORE" key (baseline targets.target_allocation) is core too."""
    out = {"CORE": 0.0}
    for k, w in (norm_alloc or {}).items():
        tk = str(k).strip().upper()
        if tk in C.CORE_TICKERS or tk == "CORE":
            out["CORE"] += float(w)
        else:
            out[tk] = out.get(tk, 0.0) + float(w)
    return out


def _priceable_map(baseline):
    """Every ticker Phase 0 actually priced: baseline["prices"] (v5.1: core tickers,
    held names, satellite-universe levels) first, then the legacy fallback — held
    positions at their mark plus anything carrying a limit_ref_price in the orders."""
    px = {}
    for t, v in (baseline.get("prices") or {}).items():
        try:
            if v is not None and float(v) > 0:
                px[str(t).upper()] = float(v)
        except (TypeError, ValueError):
            pass
    p = baseline["portfolio"]
    for t, d in p["positions"].items():
        if d.get("shares"):
            px.setdefault(str(t).upper(), d["usd"] / d["shares"])
    for o in baseline.get("orders", []):
        px.setdefault(str(o["ticker"]).upper(), o["limit_ref_price"])
    return px


def derive_execution(alloc, baseline):
    """Deterministically derive orders from a (validated) allocation: target weights
    x total, diffed against current positions at limit_ref prices, in WHOLE shares.
    The LLM never writes a share count, and never picks the QQQ/QQQM split: the core
    weight is collapsed and routed through plan_core_orders (drift band applies).

    RAISES on any allocated ticker it cannot price. Silently dropping such an order
    (the old `if price is None: continue`) is the exact cash-leak the red-team broke:
    '{"VOO":100}' would PASS "100% equity" yet execute 100% cash."""
    p = baseline["portfolio"]; total = float(p["total_usd"])
    px = _priceable_map(baseline)
    norm, errs = normalize_alloc(alloc)
    if errs:
        raise ValueError("; ".join(errs))
    col = collapse_alloc(norm)
    held = {str(t).upper(): d for t, d in p["positions"].items()}
    whole = _whole()
    buf = _buf()
    prefix = "derived from validated final_allocation; "

    if abs(col["CORE"]) > 1e-9 and not _core_prices(px):
        raise ValueError(f"UNPRICEABLE_ALLOCATION: CORE has weight {col['CORE']} but no "
                         "core ticker is priced; refusing to drop the order "
                         "(dropping it would leak the target into cash)")

    sells, buys = [], []
    frac_equity = 0.0                       # what an unconstrained fractional derivation realises
    tickers = ({t for t in col if t != "CORE"}
               | {t for t in held if t not in C.CORE_TICKERS})
    for tk in sorted(tickers):
        w = col.get(tk, 0.0)
        tgt_usd = w / 100.0 * total
        cur_usd = float(held.get(tk, {}).get("usd", 0.0))
        cur_sh = float(held.get(tk, {}).get("shares", 0.0) or 0.0)
        d = tgt_usd - cur_usd
        price = px.get(tk)
        if price is None:
            if abs(w) > 1e-9:
                raise ValueError(f"UNPRICEABLE_ALLOCATION: {tk} has weight "
                                 f"{w} but no price; refusing to drop the order "
                                 "(dropping it would leak the target into cash)")
            frac_equity += cur_usd
            continue                      # zero-weight, not held: nothing to do
        frac_equity += tgt_usd if abs(d) >= C.MIN_ORDER_USD else cur_usd
        if abs(d) < C.MIN_ORDER_USD:
            continue
        lrp = round(price, 2)
        if not whole:
            q, usd = round(abs(d) / price, 4), round(abs(d), 2)
        elif d > 0:
            q = float(math.floor(d / (price * (1 + buf)) + 1e-9))
            usd = round(q * lrp, 2)
        elif abs(w) <= 1e-9:
            q = round(cur_sh, 4)              # weight 0 -> the entire held quantity
            usd = round(q * lrp, 2)
        else:
            q = float(min(round(abs(d) / price), math.floor(cur_sh + 1e-9)))
            usd = round(q * lrp, 2)
        if q <= 0 or usd < C.MIN_ORDER_USD:
            continue
        o = {"ticker": tk, "action": "BUY" if d > 0 else "SELL", "usd": usd, "shares": q,
             "limit_ref_price": lrp, "stop": None,
             "reason": "derived from validated final_allocation",
             "pre_approved": False, "sleeve": "satellite", "whole_shares": whole}
        (buys if d > 0 else sells).append(o)

    core_value = sum(float(d.get("usd", 0.0)) for t, d in held.items() if t in C.CORE_TICKERS)
    core_target = col["CORE"] / 100.0 * total
    D = core_target - core_value
    frac_equity += core_target if abs(D) >= C.MIN_ORDER_USD else core_value
    held_sh = {t: float(held.get(t, {}).get("shares", 0.0) or 0.0) for t in C.CORE_TICKERS}
    # Core SELLs (if any) are planned first: their size never depends on cash, and
    # their proceeds are real funding for the non-core BUYs below.
    core_sells = (plan_core_orders(D, total, held_sh, px, 0.0, reason_prefix=prefix)
                  if D < 0 else [])
    # FUNDED BUYS ONLY: a non-core BUY is capped by what the book can pay for (cash +
    # every SELL's proceeds, incl. the price buffer). An unfunded BUY is never
    # emitted, so post-trade cash cannot go negative through this path.
    avail = (float(p.get("cash_usd", 0.0)) + sum(o["usd"] for o in sells)
             + sum(o["usd"] for o in core_sells))
    funded = []
    for o in buys:
        unit = o["limit_ref_price"] * (1 + (buf if whole else 0.0))
        if o["usd"] * (1 + (buf if whole else 0.0)) > avail + 1e-9:
            want = o["usd"]
            q = 0.0
            if unit > 0 and avail > 0:
                q = (float(math.floor(avail / unit + 1e-9)) if whole
                     else round(avail / unit - 5e-5, 4))
            o = dict(o, shares=q, usd=round(q * o["limit_ref_price"], 2),
                     reason=o["reason"] + "; capped by available cash")
            if q <= 0 or o["usd"] < C.MIN_ORDER_USD:
                o["usd"] = 0.0
            # the unfunded part never executes: the "unconstrained" equity must not
            # count it either, or EXECUTION_DIVERGES could not see the dropped BUY
            frac_equity -= want - o["usd"]
            if o["usd"] <= 0:
                continue
        avail -= o["usd"] * (1 + (buf if whole else 0.0))
        funded.append(o)
    buys = funded
    spendable = avail
    core = core_sells if D < 0 else plan_core_orders(D, total, held_sh, px, spendable,
                                                     reason_prefix=prefix)
    for o in core:
        o["pre_approved"] = False
    orders = ([o for o in core if o["action"] == "SELL"] + sells
              + buys + [o for o in core if o["action"] == "BUY"])

    target_cash_value = total * (1.0 - sum(col.values()) / 100.0)
    ex = post_trade_state(p, orders, px, target_cash_value, core_target_value=core_target)
    w = drift_warning(ex, orders)
    ex["warnings"] = [w] if w else []
    return {
        "orders": orders,
        "intended_equity_pct": sum(col.values()),
        "fractional_equity_pct": frac_equity / total * 100.0 if total else 0.0,
        "realized_equity_pct": realized_equity_pct(baseline, orders),
        "execution": ex,
    }


def orders_from_allocation(alloc, baseline):
    """v5 signature kept: the orders of derive_execution()."""
    return derive_execution(alloc, baseline)["orders"]


def realized_equity_pct(baseline, orders):
    """Apply derived orders to the current book and report the equity % they
    actually produce. The gate compares this to the intended equity %; a 100%-on-
    paper / 100%-cash-in-execution plan diverges by ~100pp and must FAIL."""
    total = baseline["portfolio"]["total_usd"]
    pos = {t: float(d["usd"]) for t, d in baseline["portfolio"]["positions"].items()}
    for o in orders:
        t = str(o["ticker"]).upper()
        pos[t] = pos.get(t, 0.0) + (o["usd"] if o["action"] == "BUY" else -o["usd"])
    return sum(pos.values()) / total * 100.0 if total else 0.0


def build_plan(regime_info, prices, portfolio, satellite_state=None, atrs=None, rsis=None,
               earnings=None, levels=None, weekly=False):
    """weekly=True: satellite stop-check only — stop-breach SELLs plus the core BUY
    of their proceeds; no trim, no force-close, no drift rebalance otherwise."""
    regime = regime_info["regime"]
    core_pct, sat_budget = C.TARGETS[regime]
    whole = _whole()

    positions = {}
    for p in portfolio.get("positions", []):
        t = p["ticker"].upper()
        positions[t] = positions.get(t, 0.0) + float(p["shares"])
    cash = float(portfolio.get("cash_usd", 0.0))
    mkt = {t: s * float(prices[t]) for t, s in positions.items() if t in prices}
    unpriceable = [t for t in positions if t not in prices]
    if unpriceable:
        sys.stderr.write(f"FATAL: no price for held position(s) {unpriceable}; "
                         "every percentage and order would be wrong\n")
        sys.exit(3)
    total = cash + sum(mkt.values())
    if total <= 0:
        sys.stderr.write("ERROR: portfolio total value <= 0\n")
        sys.exit(2)

    # the core is ONE sleeve summed over config.CORE_TICKERS; anything else is satellite
    held_sat = [t for t in positions if t not in C.CORE_TICKERS and t in prices]
    held_sat_value = sum(mkt[t] for t in held_sat)

    # ATR-stop breach on a held satellite name. This is the ONLY stop in the
    # system (the core carries none, by design). `--weekly` runs exactly this
    # path. The stop level comes from the PRIOR run's state, never from today's
    # price — a stop recomputed off today's close can never be breached.
    stop_breaches = []
    if regime == "TREND":
        for t in held_sat:
            prior = ((satellite_state or {}).get("stops_atr", {}) or {}).get(t)
            if prior and float(prices[t]) <= float(prior):
                stop_breaches.append(t)

    # a stopped-out name has left the satellite: its proceeds go to the CORE.
    sat_names = [t for t in held_sat if t not in stop_breaches]
    sat_value = sum(mkt[t] for t in sat_names)
    core_value = sum(mkt.get(t, 0.0) for t in C.CORE_TICKERS)

    # Unused satellite budget AUTO-ROUTES TO THE CORE. There is no cash field.
    # This is the fatal-defect fix: "no" can no longer route to 0%-yield cash.
    # TREND: core target = everything the satellite is not using (=> cash 0%).
    # DEFENSIVE: core 50%, satellite 0, remainder to the cash-equivalent.
    if regime == "TREND":
        sat_target_value = min(sat_value, sat_budget * total)
        core_target_value = total - sat_target_value
    else:
        sat_target_value = 0.0
        core_target_value = core_pct * total
    target_cash_value = max(total - core_target_value - sat_target_value, 0.0)

    def _liquidate(t, reason, **extra):
        px = float(prices[t])
        q = round(positions[t], 4)                    # ENTIRE held qty (legacy lots too)
        usd = round(q * round(px, 2), 2) if whole else round(mkt[t], 2)
        o = {"ticker": t, "action": "SELL", "usd": usd, "shares": q,
             "limit_ref_price": round(px, 2), "reason": reason, "stop": None,
             "pre_approved": True, "sleeve": "satellite", "whole_shares": whole}
        o.update(extra)
        return o

    # SELLs first (their proceeds fund the core BUY), then the core BUY.
    orders = []
    for t in stop_breaches:
        px = float(prices[t])
        stop_px = float(((satellite_state or {}).get("stops_atr", {}) or {})[t])
        orders.append(_liquidate(
            t, f"ATR stop breached: {px:.2f} <= stop {stop_px:.2f} "
               f"({C.SATELLITE_STOP_ATR_MULT}x ATR); proceeds -> core",
            stop=stop_px, stop_breach=True))

    # satellite over budget -> trim it into the CORE, never into cash
    if sat_value - sat_target_value > C.MIN_ORDER_USD and regime == "TREND" and not weekly:
        excess = sat_value - sat_target_value
        for t in sat_names:
            share = mkt[t] / sat_value if sat_value else 0.0
            usd = excess * share
            if usd < C.MIN_ORDER_USD:
                continue
            px = float(prices[t])
            q = _shares(usd, px)                     # floor() in whole-share mode
            if q <= 0:
                continue
            o_usd = round(q * round(px, 2), 2) if whole else round(usd, 2)
            if o_usd < C.MIN_ORDER_USD:
                continue
            orders.append({
                "ticker": t, "action": "SELL", "usd": o_usd,
                "shares": q, "limit_ref_price": round(px, 2),
                "reason": f"satellite {sat_value/total*100:.1f}% > "
                          f"{sat_budget*100:.0f}% budget; proceeds -> core",
                "stop": None, "pre_approved": True, "sleeve": "satellite",
                "whole_shares": whole,
            })

    if regime == "DEFENSIVE" and held_sat and not weekly:
        for t in held_sat:
            orders.append(_liquidate(t, "DEFENSIVE regime: satellite force-closed"))

    spendable = cash + sum(o["usd"] for o in orders if o["action"] == "SELL")
    core_orders = plan_core_orders(core_target_value - core_value, total,
                                   {t: positions.get(t, 0.0) for t in C.CORE_TICKERS},
                                   prices, spendable)
    if weekly:
        core_orders = [o for o in core_orders if o["action"] == "BUY"] if stop_breaches else []
    orders += [o for o in core_orders if o["action"] == "SELL"]
    orders += [o for o in core_orders if o["action"] == "BUY"]

    sat_stops = {}
    if regime == "TREND":
        for t in sat_names:
            px, a = float(prices[t]), (atrs or {}).get(t)
            if a:
                sat_stops[t] = round(px - C.SATELLITE_STOP_ATR_MULT * a, 2)

    portfolio_block = {
        "total_usd": round(total, 2),
        "cash_usd": round(cash, 2),
        "cash_pct": round(cash / total * 100, 1),
        "positions": {t: {"shares": positions[t], "usd": round(mkt[t], 2),
                          "pct": round(mkt[t] / total * 100, 1)} for t in mkt},
        "unpriceable": unpriceable,
        "core_value_usd": round(core_value, 2),
        "core_pct": round(core_value / total * 100, 1),
    }
    target_alloc = {"CORE": round(core_target_value / total * 100, 1)}
    if regime == "TREND" and sat_value > 0:
        for t in sat_names:
            w = round(mkt[t] / sat_value * sat_target_value / total * 100, 1)
            if w > 0:
                target_alloc[t] = w

    all_prices = {t: round(float(v), 4) for t, v in prices.items()
                  if v is not None and float(v) > 0}
    for t, lv in (levels or {}).items():
        if lv and lv.get("price"):
            all_prices.setdefault(t, round(float(lv["price"]), 4))

    execution = post_trade_state(portfolio_block, orders, prices, target_cash_value,
                                 core_target_value=core_target_value)
    w = drift_warning(execution, orders, weekly=weekly)
    execution["warnings"] = [w] if w else []

    plan = {
        "schema": "baseline_plan/v5.1",
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "PRE_APPROVED",
        "regime": regime_info,
        "prices": all_prices,
        "portfolio": portfolio_block,
        "targets": {
            "core_ticker": C.CORE_TICKER,
            "core_tickers": list(C.CORE_TICKERS),
            "core_pct": round(core_target_value / total * 100, 1),
            "satellite_budget_pct": round(sat_budget * 100, 1),
            "satellite_used_pct": round(sat_value / total * 100, 1),
            "satellite_target_pct": round(sat_target_value / total * 100, 1),
            "equity_target_pct": round((core_target_value + sat_target_value) / total * 100, 1),
            "cash_target_pct": C.CASH_TARGET_PCT * 100,   # zero. Cash is a residual.
            "cash_pct": 0.0 if regime == "TREND" else round(100 - core_pct * 100, 1),
            "cash_max_pct": C.CASH_MAX_PCT * 100,
            "cash_equiv_destination": (None if regime == "TREND" else C.DEFENSIVE_CASH_EQUIV),
            "equity_floor_pct": round(((core_target_value + sat_target_value) / total) * 100
                                      - C.CASH_MAX_PCT * 100
                                      - C.OVERRIDE_MAX_EQUITY_REDUCTION_PCT * 100, 1),
            "target_allocation": target_alloc,
        },
        "orders": orders,
        "execution": execution,
        "satellite": {
            "names": sat_names,
            "held_names": held_sat,
            "held_pct": round(held_sat_value / total * 100, 1),
            "stop_breaches": stop_breaches,
            "stops_atr": sat_stops,
            "min_hold_days": C.SATELLITE_MIN_HOLD_DAYS,
            "max_roundtrips_per_year": C.SATELLITE_MAX_ROUNDTRIPS_PER_YEAR,
            "rsi": {t: round(v, 1) for t, v in (rsis or {}).items() if v is not None},
            "halve_size_if_rsi_above": C.SATELLITE_RSI_HALVE_ABOVE,
            "max_names": C.SATELLITE_MAX_NAMES,
            "min_rr": C.SATELLITE_MIN_RR,
            "universe": C.SATELLITE_UNIVERSE,
            "days_to_earnings": earnings or {},
            "earnings_blackout_sessions": C.EARNINGS_BLACKOUT_SESSIONS,
            "real_money_enabled": C.SATELLITE_REAL_MONEY_ENABLED,
            "levels": levels or {},
            "levels_error": None,
        },
        "override_channel": {
            "max_equity_reduction_pp": C.OVERRIDE_MAX_EQUITY_REDUCTION_PCT * 100,
            "catalyst_max_age_hours": C.OVERRIDE_CATALYST_MAX_AGE_HOURS,
            "expiry_trading_days": C.OVERRIDE_EXPIRY_TRADING_DAYS,
            "suspended_until": None,   # filled by validate.py from the recs log
        },
        # D2 run triggers on the pre-trade book (main() adds the shock-move fields)
        "events": cash_events(portfolio_block, prices, target_cash_value),
    }
    return plan


def _frame_last_bar(frame):
    """The frame's last bar = the last date on which ANY column has a value (a
    trailing all-NaN row is not a bar)."""
    f = frame.dropna(how="all") if hasattr(frame, "dropna") else frame
    return f.index[-1] if len(f) else None


def fresh_last_price(series, last_bar):
    """Per-ticker freshness (RT1-5). A ticker is priced only if its OWN last valid
    bar IS the frame's last bar; dropna().iloc[-1] alone would silently price a
    name at a days-old close while data_age_hours (frame-level) reads fresh.
    Returns (price or None, last_valid_date or None)."""
    try:
        c = series.dropna()
    except Exception:
        return None, None
    if not len(c):
        return None, None
    lv = c.index[-1]
    try:
        v = float(c.iloc[-1])
    except (TypeError, ValueError):
        return None, lv
    if v != v or v <= 0:
        return None, lv
    if last_bar is not None and lv.date() != last_bar.date():
        return None, lv
    return v, lv


def _fresh_prices(frame, tickers, last_bar):
    """{ticker: fresh last close} + {ticker: 'YYYY-MM-DD' last valid bar} for the
    tickers present but stale (absent columns are simply unpriced)."""
    prices, stale = {}, {}
    cols = getattr(frame, "columns", [])
    for t in tickers:
        if t not in cols:
            continue
        v, lv = fresh_last_price(frame[t], last_bar)
        if v is not None:
            prices[t] = v
        elif lv is not None:
            stale[t] = str(lv.date())
    return prices, stale


def _universe_levels(close, high, low, tickers, source, earn=None, last_bar=None):
    """Script-owned ATR levels for every satellite-universe name (D4). `source`
    is "ohlc" (true range on real highs/lows) or "close_proxy" (CSV/offline).
    Per-name failures are skipped, never fatal."""
    levels = {}
    for t in tickers:
        try:
            if t not in close.columns:
                continue
            c = close[t].dropna()
            if len(c) < 2:
                continue
            ref = last_bar if last_bar is not None else _frame_last_bar(close)
            fpx, _ = fresh_last_price(close[t], ref)
            if fpx is None:
                continue                  # stale / missing last bar: no script levels
            px = fpx
            if source == "ohlc" and high is not None and low is not None \
                    and t in high.columns and t in low.columns:
                a = _atr(high[t].reindex(c.index), low[t].reindex(c.index), c)
            else:
                a = _atr_proxy(c)
            lv = atr_levels(px, a)
            if not lv:
                continue
            r = _rsi(c)
            lv.update({"rsi": round(r, 1) if r is not None else None,
                       "days_to_earnings": (earn or {}).get(t),
                       "atr_source": source})
            levels[t] = lv
        except Exception:
            continue
    return levels


# ---------------------------------------------------------------- backtest
def _core_history(history_csv=None, tickers=None):
    """>= REGIME_SMA_MONTHS + 1 COMPLETED months are required before the overlay
    has an opinion at all, so the backtest cannot be fed the 3-month archive CSV.
    Default source: yfinance 6y. --history-csv overrides (CI/offline)."""
    tickers = list(dict.fromkeys(tickers or list(C.CORE_TICKERS)))
    if history_csv:
        df = _load_csv(history_csv)
        df, _ = drop_partial_bar(df)
        return df[[t for t in tickers if t in df.columns]]
    close, _, _ = _load_yf(tickers, period="6y")
    # today's in-progress bar is NOT a close — it must not enter the walk either,
    # or the backtest silently trades on a price that has not happened yet.
    close, _ = drop_partial_bar(close)
    if hasattr(close, "columns"):
        return close[[t for t in tickers if t in close.columns]].dropna(how="all")
    return close.to_frame(tickers[0])


def _atr_proxy(close, period=14):
    """Backtest-only ATR estimate. The sim has no OHLC, so true range is
    approximated from close-to-close moves (~1.5x understated, hence the factor).
    The LIVE path never uses this — it uses _atr() on real highs/lows."""
    d = close.diff().abs().dropna()
    if len(d) < period:
        return None
    return float(d.iloc[-period:].mean() * 1.5)


def _walk(close_df, start, end, init_usd, sat_ticker=None, sat_pct=0.0, use_atr_stops=True):
    """Bar-by-bar: every session in [start, end] is fed through compute_regime and
    build_plan on the EVOLVING synthetic portfolio, and the resulting orders are
    executed at that bar's close with config.FX_SPREAD_PCT + config.COMMISSION_PCT
    charged per order. No closed form, no assumed regime, no tautology.

    Every config.CORE_TICKERS column present in close_df (and priced at the bar)
    is part of the core sleeve; a history without QQQM walks QQQ-only. In whole-
    share mode a BUY whose cost incl. fees exceeds cash is SKIPPED, never scaled
    into a fractional buy. Seeded fractional lots (bear fixture) are allowed; the
    full-liquidation sells handle them."""
    import pandas as pd
    fee = C.FX_SPREAD_PCT + C.COMMISSION_PCT
    core = close_df[C.CORE_TICKER].dropna()
    sessions = [d for d in core.loc[start:end].index]
    if not sessions:
        sys.stderr.write(f"ERROR: no sessions in {start}..{end}\n")
        sys.exit(2)

    shares, cash = {}, init_usd
    if sat_ticker and sat_pct:
        # bear fixture: seed a real position set so there is a satellite to
        # force-close and a core to de-risk. Plain backtests start in cash and
        # let build_plan buy in, which is the only fair model of a first run.
        px0 = float(core.loc[sessions[0]])
        sp0 = float(close_df[sat_ticker].loc[sessions[0]])
        shares = {C.CORE_TICKER: init_usd * (1.0 - sat_pct) / px0,
                  sat_ticker: init_usd * sat_pct / sp0}
        cash = 0.0

    prev_regime, prev_flip_month = None, None
    n_trend = n_def = 0
    flips, forced_closes, stop_sells, reentries = [], [], [], []
    state = {"stops_atr": {}}
    trades = 0

    for d in sessions:
        hist = core.loc[:d]
        reg = compute_regime(hist, prev_regime, prev_flip_month)
        r = reg["regime"]
        prices = {C.CORE_TICKER: float(core.loc[d])}
        for t in C.CORE_TICKERS:
            if t != C.CORE_TICKER and t in close_df.columns:
                v = close_df[t].get(d)
                if v is not None and v == v and float(v) > 0:
                    prices[t] = float(v)
        if sat_ticker and sat_ticker in close_df.columns:
            try:
                prices[sat_ticker] = float(close_df[sat_ticker].loc[d])
            except Exception:
                pass
        pf = {"cash_usd": cash,
              "positions": [{"ticker": t, "shares": s} for t, s in shares.items()
                            if s > 1e-9 and t in prices]}
        # a name we hold but cannot price would sys.exit(3) in build_plan (E9);
        # in the walk we simply have no such case by construction.
        atrs = {}
        if use_atr_stops and sat_ticker and sat_ticker in close_df.columns:
            a = _atr_proxy(close_df[sat_ticker].loc[:d].dropna())
            if a:
                atrs[sat_ticker] = a
        plan = build_plan(reg, prices, pf, satellite_state=state, atrs=atrs)
        whole = not C.FRACTIONAL_SHARES
        for o in plan["orders"]:
            t, usd, px = o["ticker"], float(o["usd"]), float(o["limit_ref_price"])
            if o["action"] == "BUY":
                spend = usd * (1 + fee)
                if spend > cash + 1e-6:
                    if whole:
                        continue          # never scale a whole-share buy into a fraction
                    spend = max(cash, 0.0)
                    usd = spend / (1 + fee)
                if usd < C.MIN_ORDER_USD:
                    continue
                cash -= spend
                shares[t] = shares.get(t, 0.0) + (float(o["shares"]) if whole else usd / px)
            else:
                q = min(float(o["shares"]) if whole else usd / px, shares.get(t, 0.0))
                if whole and shares.get(t, 0.0) - q < 1e-3:
                    q = shares.get(t, 0.0)    # full liquidation: no 4dp-rounding dust
                if q * px < C.MIN_ORDER_USD:
                    continue
                shares[t] = shares.get(t, 0.0) - q
                cash += q * px * (1 - fee)
            trades += 1
            if o.get("stop_breach"):
                stop_sells.append(str(d.date()))
            elif "force-closed" in o.get("reason", ""):
                forced_closes.append(str(d.date()))
        state = {"stops_atr": plan["satellite"]["stops_atr"]}

        if prev_regime and r != prev_regime:
            flips.append({"date": str(d.date()), "from": prev_regime, "to": r})
            prev_flip_month = reg.get("eval_month")
            if r == "TREND":
                reentries.append(str(d.date()))
        prev_regime = r
        n_trend += (r == "TREND")
        n_def += (r == "DEFENSIVE")

    navN = cash + sum(s * float(close_df[t].loc[sessions[-1]]) for t, s in shares.items() if s > 1e-9)
    fin_cash_pct = cash / navN * 100 if navN else 0.0
    qqq_ret = float(core.loc[sessions[-1]] / core.loc[sessions[0]] - 1.0) * 100
    return {
        "window": f"{str(sessions[0].date())}..{str(sessions[-1].date())}",
        "sessions": len(sessions),
        "trend_sessions": n_trend,
        "defensive_sessions": n_def,
        "regime_flips": flips,
        "satellite_force_closes": forced_closes[:3],
        "satellite_stop_sells": stop_sells[:3],
        "reentries": reentries,
        "trades": trades,
        "qqq_return_pct": round(qqq_ret, 2),
        "mechanical_core_return_pct": round((navN / init_usd - 1) * 100, 2),
        "final_nav_usd": round(navN, 2),
        "final_cash_pct": round(fin_cash_pct, 1),
        "final_shares": {t: round(s, 4) for t, s in shares.items() if s > 1e-9},
        "core_tickers_walked": [t for t in C.CORE_TICKERS if t in close_df.columns],
    }


def backtest(start, end, init_usd=1942.0, history_csv=None):
    r = _walk(_core_history(history_csv), start, end, init_usd)
    ret = r["mechanical_core_return_pct"]
    r.update({
        "v4_realized_return_pct": C.V4_REALIZED_RETURN_PCT,
        "gap_vs_v4_pp": round(ret - C.V4_REALIZED_RETURN_PCT, 2),
        "acceptance_regime": (r["sessions"] >= C.BACKTEST_MIN_SESSIONS
                              and r["trend_sessions"] >= C.BACKTEST_MIN_TREND_SESSIONS),
        "acceptance_return": ret >= C.BACKTEST_MIN_RETURN_PCT,
    })
    return r


def backtest_bear(init_usd=1942.0, history_csv=None):
    """Synthetic bear fixture: real history, then QQQ scaled -35% over 8 months,
    then a recovery leg. Asserts the TREND->DEFENSIVE flip, the satellite force-
    close, and the re-entry all actually fire. The 50%-cash branch has no other
    coverage anywhere in this repo."""
    import numpy as np
    import pandas as pd
    sat = "NVDA"
    real = _core_history(history_csv, tickers=[C.CORE_TICKER, sat])
    real = real.dropna()
    last = real.index[-1]
    n_bear, n_rec = 168, 294          # ~8 months down, ~14 months back up
    idx = pd.bdate_range(last + pd.Timedelta(days=1), periods=n_bear + n_rec)
    p0 = {t: float(real[t].iloc[-1]) for t in real.columns}
    down = np.linspace(0, 1, n_bear + 1)[1:]
    up = np.linspace(0, 1, n_rec + 1)[1:]
    rng = np.random.default_rng(7)          # seeded: the fixture is reproducible
    rows = {}
    for t in real.columns:
        trough = p0[t] * (0.65 if t == C.CORE_TICKER else 0.50)   # satellite falls harder
        peak = p0[t] * (1.30 if t == C.CORE_TICKER else 1.10)
        bear = p0[t] * (trough / p0[t]) ** down
        rec = trough * (peak / trough) ** up
        path = np.concatenate([bear, rec])
        sigma = 0.010 if t == C.CORE_TICKER else 0.020   # a bear tape is not a smooth line
        rows[t] = path * np.exp(rng.normal(0, sigma, size=len(path)))
    synth = pd.DataFrame(rows, index=idx)
    full = pd.concat([real, synth])

    start = str((last - pd.Timedelta(days=20)).date())
    end = str(idx[-1].date())
    # leg A: ATR stops live -> the satellite is stopped out on the way down.
    a = _walk(full, start, end, init_usd, sat_ticker=sat, sat_pct=C.SATELLITE_MAX_PCT,
              use_atr_stops=True)
    # leg B: stops disabled -> the satellite survives to the flip and must then be
    # FORCE-CLOSED by the DEFENSIVE branch. Both exits need coverage; with stops on,
    # the stop fires first and the force-close path would never be reached.
    b = _walk(full, start, end, init_usd, sat_ticker=sat, sat_pct=C.SATELLITE_MAX_PCT,
              use_atr_stops=False)
    r = dict(a)
    r["fixture"] = "bear (-35% over ~8 months, then recovery; seeded noise)"
    r["leg_b_no_stops"] = {k: b[k] for k in ("regime_flips", "satellite_force_closes",
                                             "reentries", "mechanical_core_return_pct")}
    r["assert_flip_to_defensive"] = any(f["to"] == "DEFENSIVE" for f in a["regime_flips"])
    r["assert_satellite_stopped_out"] = bool(a["satellite_stop_sells"])
    r["assert_satellite_force_closed"] = bool(b["satellite_force_closes"])
    r["assert_reentry_to_trend"] = bool(a["reentries"])
    r["acceptance_bear"] = (r["assert_flip_to_defensive"]
                            and r["assert_satellite_stopped_out"]
                            and r["assert_satellite_force_closed"]
                            and r["assert_reentry_to_trend"])
    return r


# -------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--portfolio")
    ap.add_argument("--prices-csv")
    ap.add_argument("--out", default=C.BASELINE_JSON)
    ap.add_argument("--backtest", nargs="?", const=C.BACKTEST_WINDOW,
                    help=f"bar-by-bar walk over START:END (default {C.BACKTEST_WINDOW}); "
                         "history comes from yfinance unless --history-csv is given")
    ap.add_argument("--backtest-fixture", choices=["bear"],
                    help="synthetic fixture: 'bear' asserts the DEFENSIVE flip, the "
                         "satellite force-close, and the re-entry all fire")
    ap.add_argument("--history-csv", help="long history for the backtest (>= 11 completed months)")
    ap.add_argument("--weekly", action="store_true",
                    help="satellite stop-check ONLY: no regime evaluation, no new entries")
    ap.add_argument("--last-run", default=C.LAST_RUN_JSON)
    a = ap.parse_args()

    if a.backtest_fixture == "bear":
        print(json.dumps(backtest_bear(history_csv=a.history_csv), indent=2))
        return

    if a.backtest:
        s, e = a.backtest.split(":")
        print(json.dumps(backtest(s, e, history_csv=a.history_csv), indent=2))
        return

    if not a.portfolio:
        sys.stderr.write("ERROR: --portfolio required\n")
        sys.exit(2)
    portfolio = json.load(open(a.portfolio))
    held = [p["ticker"].upper() for p in portfolio.get("positions", [])]
    tickers = list(dict.fromkeys([C.CORE_TICKER] + list(C.CORE_TICKERS) + held))
    universe = [t for t in dict.fromkeys(C.SATELLITE_UNIVERSE)]
    warnings = []

    atrs, rsis, earn = {}, {}, {}
    levels, levels_error = {}, None
    if a.prices_csv:
        df = _load_csv(a.prices_csv)
        df, dropped = drop_partial_bar(df)
        frame_last = _frame_last_bar(df)
        prices, stale = _fresh_prices(df, tickers, frame_last)
        if C.CORE_TICKER not in prices:
            sys.stderr.write(f"FATAL: no fresh price for the core ticker {C.CORE_TICKER}"
                             f"{' (last bar ' + stale[C.CORE_TICKER] + ')' if C.CORE_TICKER in stale else ''}\n")
            sys.exit(3)
        core_close = df[C.CORE_TICKER].dropna()
        last_bar = frame_last.date()
        closes_df = df
        for t in tickers:
            if t in C.CORE_TICKERS or t not in df.columns:
                continue
            try:
                rsis[t] = _rsi(df[t].dropna())
            except Exception:
                pass
        if not a.weekly:
            # offline: close-to-close ATR proxy from the CSV, no network at all
            levels = _universe_levels(df, None, None, universe, "close_proxy",
                                      last_bar=frame_last)
    else:
        close, high, low = _load_yf(tickers)
        close, dropped = drop_partial_bar(close)
        if high is not None:
            high, _ = drop_partial_bar(high)
            low, _ = drop_partial_bar(low)
        frame_last = _frame_last_bar(close)
        prices, stale = _fresh_prices(close, tickers, frame_last)
        for t in tickers:
            if t not in prices:
                sys.stderr.write(f"WARN: no fresh price for {t}"
                                 f"{' (last bar ' + stale[t] + ')' if t in stale else ''}\n")
        if C.CORE_TICKER not in prices:
            sys.stderr.write(f"FATAL: no fresh price for the core ticker {C.CORE_TICKER}\n")
            sys.exit(3)
        core_close = close[C.CORE_TICKER].dropna()
        last_bar = frame_last.date()
        closes_df = close
        for t in tickers:
            if t in C.CORE_TICKERS:
                continue
            try:
                atrs[t] = _atr(high[t], low[t], close[t])
                rsis[t] = _rsi(close[t].dropna())
            except Exception:
                pass
            d = earnings_days(t)
            if d is not None:
                earn[t] = d
        if not a.weekly:
            # SEPARATE, NON-FATAL download: the shadow/satellite levels must never
            # delay or kill the core plan. On failure levels = {} (every shadow
            # pick is then rejected SHADOW_NO_LEVELS by validate.py).
            try:
                uc, uh, ul = _load_yf(universe, period="1y", fatal=False)
                uc, _ = drop_partial_bar(uc)
                if uh is not None and ul is not None:
                    uh, _ = drop_partial_bar(uh)
                    ul, _ = drop_partial_bar(ul)
                u_earn = {}
                for t in universe:
                    if t in earn:
                        u_earn[t] = earn[t]
                        continue
                    try:
                        d = earnings_days(t)
                    except Exception:
                        d = None
                    if d is not None:
                        u_earn[t] = d
                levels = _universe_levels(uc, uh, ul, universe, "ohlc", u_earn,
                                          last_bar=frame_last)
                if not levels:
                    levels_error = "no satellite-universe levels could be computed"
            except BaseException as e:          # incl. SystemExit from a helper
                if isinstance(e, KeyboardInterrupt):
                    raise
                levels, levels_error = {}, f"{type(e).__name__}: {e}"
                sys.stderr.write(f"WARN: satellite-universe levels unavailable ({levels_error})\n")

    # A HELD name without a fresh price is fatal (every % and order would be wrong;
    # build_plan exits 3 on it too — checked here so the reason names staleness).
    stale_held = [t for t in held if t not in prices]
    if stale_held:
        sys.stderr.write("FATAL: no fresh price for held position(s) "
                         + ", ".join(f"{t} (last bar {stale.get(t, 'none')}, frame "
                                     f"{last_bar})" for t in stale_held) + "\n")
        sys.exit(3)
    for t in C.CORE_TICKERS:
        if t not in prices and t not in held:
            if t in stale:
                warnings.append(f"{t} price stale (last bar {stale[t]} < frame last bar "
                                f"{last_bar}) and not held: core plan runs without it")
            else:
                warnings.append(f"{t} unpriced and not held: core plan runs without it")
            sys.stderr.write(f"WARN: {warnings[-1]}\n")

    age = data_age_hours(last_bar)
    if age < 0:
        sys.stderr.write(f"FATAL: negative data_age_hours ({age:.1f}) — partial bar leaked\n")
        sys.exit(3)
    if age > C.MAX_DATA_AGE_HOURS:
        sys.stderr.write(f"FATAL: data stale ({age:.1f}h > {C.MAX_DATA_AGE_HOURS}h)\n")
        sys.exit(3)

    prev = {}
    if os.path.exists(a.last_run):
        try:
            prev = json.load(open(a.last_run))
        except Exception:
            prev = {}
    sat_state = {"stops_atr": prev.get("satellite_stops_atr", {}) or {}}

    if a.weekly:
        # --weekly: satellite stop-check ONLY. No regime evaluation (the regime is
        # carried from last_run), no new entries, no drift rebalance.
        reg = {"regime": prev.get("regime", "TREND"),
               "reason": "weekly stop-check: regime NOT evaluated (carried from last_run)",
               "monthly_close": None, "sma_10m": None,
               "eval_month": prev.get("regime_flip_month"), "flip_suppressed": False,
               "evaluated": False}
    else:
        reg = compute_regime(core_close, prev.get("regime"), prev.get("regime_flip_month"))

    sat_earn = dict(earn)
    for t, lv in levels.items():
        if lv.get("days_to_earnings") is not None:
            sat_earn.setdefault(t, lv["days_to_earnings"])
    plan = build_plan(reg, prices, portfolio, satellite_state=sat_state,
                      atrs=atrs, rsis=rsis, earnings=sat_earn, levels=levels,
                      weekly=a.weekly)
    plan["satellite"]["levels_error"] = levels_error
    plan["execution"]["warnings"] = warnings + list(plan["execution"].get("warnings") or [])
    for w in plan["execution"]["warnings"][len(warnings):]:
        sys.stderr.write(f"WARN: {w}\n")

    if a.weekly:
        breached = plan["satellite"]["stop_breaches"]
        plan["orders"] = [] if not breached else [
            o for o in plan["orders"]
            if o.get("stop_breach") or (o["ticker"] in C.CORE_TICKERS and o["action"] == "BUY")
        ]
        plan["mode"] = "weekly"

    # event detection: a single-session move on a held name beyond
    # config.SHOCK_MOVE_PCT is a named run trigger (see SKILL Cadence).
    shocks = {}
    try:
        for t in dict.fromkeys([C.CORE_TICKER] + held):
            if t not in closes_df.columns:
                continue
            s = closes_df[t].dropna()
            if len(s) >= 2:
                mv = float(s.iloc[-1] / s.iloc[-2] - 1.0)
                if abs(mv) > C.SHOCK_MOVE_PCT:
                    shocks[t] = round(mv * 100, 2)
    except Exception:
        pass

    # cash_over_max (D2): only DEPLOYABLE excess over the regime's target cash is a
    # run trigger; the unavoidable variant (no whole core share fits) is info only.
    plan["events"] = {"shock_move_pct_threshold": C.SHOCK_MOVE_PCT * 100,
                      "shock_moves": shocks,
                      **plan.get("events", {})}
    plan["data"] = {"last_bar": str(last_bar), "data_age_hours": round(age, 1),
                    "partial_bar_dropped": dropped, "auto_adjust": C.AUTO_ADJUST}

    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True) if a.out != "/dev/stdout" else None
    if a.out == "/dev/stdout":
        print(json.dumps(plan, indent=2, ensure_ascii=False))
    else:
        with open(a.out, "w") as f:
            json.dump(plan, f, indent=2, ensure_ascii=False)
        sys.stderr.write(f"baseline_plan -> {a.out} · regime={reg['regime']} · "
                         f"orders={len(plan['orders'])}\n")
        print(json.dumps(plan, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
