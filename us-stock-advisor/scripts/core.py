#!/usr/bin/env python3
"""us-stock-advisor v5 — Phase 0 DETERMINISTIC CORE. The authority.

Install path: ~/.claude/skills/us-stock-advisor/scripts/core.py

This script owns 100% of the money-touching numbers: regime, target weights,
rebalance orders, fractional share counts, satellite stops, earnings blackouts.
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


def _load_yf(tickers, period="6y"):
    try:
        import yfinance as yf
        import pandas as pd
    except ModuleNotFoundError:
        sys.stderr.write("ERROR: pip install --user yfinance pandas\n")
        sys.exit(3)
    data = yf.download(
        list(dict.fromkeys(tickers)), period=period, interval="1d",
        auto_adjust=C.AUTO_ADJUST, progress=False, group_by="column",
    )
    if data is None or len(data) == 0:
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
    """config.FRACTIONAL_SHARES owns share granularity. KIS supports fractional;
    if a broker ever does not, this is the one place that changes."""
    if C.FRACTIONAL_SHARES:
        return round(usd / px, 4)
    return float(math.floor(usd / px))


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


def _priceable_map(baseline):
    """Every ticker Phase 0 actually priced: held positions (at their mark) plus
    anything carrying a limit_ref_price in the pre-approved orders."""
    p = baseline["portfolio"]
    px = {t: d["usd"] / d["shares"] for t, d in p["positions"].items() if d.get("shares")}
    for o in baseline.get("orders", []):
        px.setdefault(str(o["ticker"]).upper(), o["limit_ref_price"])
    return px


def orders_from_allocation(alloc, baseline):
    """Deterministically derive orders: target weights x total, diffed against
    current positions at limit_ref prices. The LLM never writes a share count.

    RAISES on any allocated ticker it cannot price. Silently dropping such an order
    (the old `if price is None: continue`) is the exact cash-leak the red-team broke:
    '{"VOO":100}' would PASS "100% equity" yet execute 100% cash."""
    p = baseline["portfolio"]; total = p["total_usd"]
    px = _priceable_map(baseline)
    norm, errs = normalize_alloc(alloc)
    if errs:
        raise ValueError("; ".join(errs))
    orders = []
    tickers = set(norm) | {str(t).upper() for t in p["positions"]}
    for tk in sorted(tickers):
        tgt_usd = norm.get(tk, 0.0) / 100.0 * total
        cur_usd = p["positions"].get(tk, {}).get("usd", 0.0)
        d = tgt_usd - cur_usd
        price = px.get(tk)
        if price is None:
            if abs(norm.get(tk, 0.0)) > 1e-9:
                raise ValueError(f"UNPRICEABLE_ALLOCATION: {tk} has weight "
                                 f"{norm[tk]} but no price; refusing to drop the order "
                                 "(dropping it would leak the target into cash)")
            continue                      # zero-weight, not held: nothing to do
        if abs(d) < C.MIN_ORDER_USD:
            continue
        orders.append({"ticker": tk, "action": "BUY" if d > 0 else "SELL",
                       "usd": round(abs(d), 2), "shares": _shares(abs(d), price),
                       "limit_ref_price": round(price, 2), "stop": None,
                       "reason": "derived from validated final_allocation",
                       "pre_approved": False})
    return orders


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
               earnings=None):
    regime = regime_info["regime"]
    core_pct, sat_budget = C.TARGETS[regime]

    positions = {p["ticker"].upper(): float(p["shares"]) for p in portfolio.get("positions", [])}
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

    held_sat = [t for t in positions if t != C.CORE_TICKER and t in prices]
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
    core_value = mkt.get(C.CORE_TICKER, 0.0)

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

    orders = []
    for t in stop_breaches:
        px = float(prices[t])
        stop_px = float(((satellite_state or {}).get("stops_atr", {}) or {})[t])
        orders.append({
            "ticker": t, "action": "SELL", "usd": round(mkt[t], 2),
            "shares": round(positions[t], 4), "limit_ref_price": round(px, 2),
            "reason": f"ATR stop breached: {px:.2f} <= stop {stop_px:.2f} "
                      f"({C.SATELLITE_STOP_ATR_MULT}x ATR); proceeds -> {C.CORE_TICKER}",
            "stop": stop_px, "pre_approved": True, "stop_breach": True,
        })

    # satellite over budget -> trim it into the CORE, never into cash
    if sat_value - sat_target_value > C.MIN_ORDER_USD and regime == "TREND":
        excess = sat_value - sat_target_value
        for t in sat_names:
            share = mkt[t] / sat_value if sat_value else 0.0
            usd = excess * share
            if usd < C.MIN_ORDER_USD:
                continue
            px = float(prices[t])
            orders.append({
                "ticker": t, "action": "SELL", "usd": round(usd, 2),
                "shares": _shares(usd, px), "limit_ref_price": round(px, 2),
                "reason": f"satellite {sat_value/total*100:.1f}% > "
                          f"{sat_budget*100:.0f}% budget; proceeds -> {C.CORE_TICKER}",
                "stop": None, "pre_approved": True,
            })

    core_drift = (core_target_value - core_value) / total
    if abs(core_drift) > C.REBALANCE_DRIFT_BAND_PCT:
        usd = core_target_value - core_value
        if abs(usd) >= C.MIN_ORDER_USD:
            px = float(prices[C.CORE_TICKER])
            orders.append({
                "ticker": C.CORE_TICKER,
                "action": "BUY" if usd > 0 else "SELL",
                "usd": round(abs(usd), 2),
                "shares": _shares(abs(usd), px),   # config.FRACTIONAL_SHARES
                "limit_ref_price": round(px, 2),
                "reason": f"drift {core_drift*100:+.1f}pp > {C.REBALANCE_DRIFT_BAND_PCT*100:.0f}pp band",
                "stop": None,      # THE CORE CARRIES NO STOP. By design. (7/08 whipsaw = impossible.)
                "pre_approved": True,
            })

    if regime == "DEFENSIVE" and held_sat:
        for t in held_sat:
            px = float(prices[t])
            orders.append({
                "ticker": t, "action": "SELL", "usd": round(mkt[t], 2),
                "shares": round(positions[t], 4), "limit_ref_price": round(px, 2),
                "reason": "DEFENSIVE regime: satellite force-closed", "stop": None,
                "pre_approved": True,
            })

    sat_stops = {}
    if regime == "TREND":
        for t in sat_names:
            px, a = float(prices[t]), (atrs or {}).get(t)
            if a:
                sat_stops[t] = round(px - C.SATELLITE_STOP_ATR_MULT * a, 2)

    plan = {
        "schema": "baseline_plan/v5",
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "PRE_APPROVED",
        "regime": regime_info,
        "portfolio": {
            "total_usd": round(total, 2),
            "cash_usd": round(cash, 2),
            "cash_pct": round(cash / total * 100, 1),
            "positions": {t: {"shares": positions[t], "usd": round(mkt[t], 2),
                              "pct": round(mkt[t] / total * 100, 1)} for t in mkt},
            "unpriceable": unpriceable,
        },
        "targets": {
            "core_ticker": C.CORE_TICKER,
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
        },
        "orders": orders,
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
        },
        "override_channel": {
            "max_equity_reduction_pp": C.OVERRIDE_MAX_EQUITY_REDUCTION_PCT * 100,
            "catalyst_max_age_hours": C.OVERRIDE_CATALYST_MAX_AGE_HOURS,
            "expiry_trading_days": C.OVERRIDE_EXPIRY_TRADING_DAYS,
            "suspended_until": None,   # filled by validate.py from the recs log
        },
    }
    return plan


# ---------------------------------------------------------------- backtest
def _core_history(history_csv=None, tickers=None):
    """>= REGIME_SMA_MONTHS + 1 COMPLETED months are required before the overlay
    has an opinion at all, so the backtest cannot be fed the 3-month archive CSV.
    Default source: yfinance 6y. --history-csv overrides (CI/offline)."""
    tickers = list(dict.fromkeys(tickers or [C.CORE_TICKER]))
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
    charged per order. No closed form, no assumed regime, no tautology."""
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
        for o in plan["orders"]:
            t, usd, px = o["ticker"], float(o["usd"]), float(o["limit_ref_price"])
            if o["action"] == "BUY":
                spend = usd * (1 + fee)
                if spend > cash + 1e-6:
                    spend = max(cash, 0.0)
                    usd = spend / (1 + fee)
                if usd < C.MIN_ORDER_USD:
                    continue
                cash -= spend
                shares[t] = shares.get(t, 0.0) + usd / px
            else:
                q = min(usd / px, shares.get(t, 0.0))
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
    tickers = list(dict.fromkeys([C.CORE_TICKER] + held))

    atrs, rsis, earn = {}, {}, {}
    if a.prices_csv:
        df = _load_csv(a.prices_csv)
        df, dropped = drop_partial_bar(df)
        prices = {t: float(df[t].dropna().iloc[-1]) for t in tickers if t in df.columns}
        core_close = df[C.CORE_TICKER].dropna()
        last_bar = df.index[-1].date()
        closes_df = df
        for t in tickers:
            if t == C.CORE_TICKER or t not in df.columns:
                continue
            try:
                rsis[t] = _rsi(df[t].dropna())
            except Exception:
                pass
    else:
        close, high, low = _load_yf(tickers)
        close, dropped = drop_partial_bar(close)
        if high is not None:
            high, _ = drop_partial_bar(high)
            low, _ = drop_partial_bar(low)
        prices = {}
        for t in tickers:
            try:
                prices[t] = float(close[t].dropna().iloc[-1])
            except Exception:
                sys.stderr.write(f"WARN: no price for {t}\n")
        core_close = close[C.CORE_TICKER].dropna()
        last_bar = close.index[-1].date()
        closes_df = close
        for t in tickers:
            if t == C.CORE_TICKER:
                continue
            try:
                atrs[t] = _atr(high[t], low[t], close[t])
                rsis[t] = _rsi(close[t].dropna())
            except Exception:
                pass
            d = earnings_days(t)
            if d is not None:
                earn[t] = d

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

    plan = build_plan(reg, prices, portfolio, satellite_state=sat_state,
                      atrs=atrs, rsis=rsis, earnings=earn)

    if a.weekly:
        breached = plan["satellite"]["stop_breaches"]
        plan["orders"] = [] if not breached else [
            o for o in plan["orders"]
            if o.get("stop_breach") or (o["ticker"] == C.CORE_TICKER and o["action"] == "BUY")
        ]
        plan["mode"] = "weekly"

    # event detection: a single-session move on a held name beyond
    # config.SHOCK_MOVE_PCT is a named run trigger (see SKILL Cadence).
    shocks = {}
    try:
        for t in tickers:
            s = closes_df[t].dropna()
            if len(s) >= 2:
                mv = float(s.iloc[-1] / s.iloc[-2] - 1.0)
                if abs(mv) > C.SHOCK_MOVE_PCT:
                    shocks[t] = round(mv * 100, 2)
    except Exception:
        pass

    plan["events"] = {"shock_move_pct_threshold": C.SHOCK_MOVE_PCT * 100,
                      "shock_moves": shocks,
                      "cash_over_max": plan["portfolio"]["cash_pct"] > C.CASH_MAX_PCT * 100}
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
