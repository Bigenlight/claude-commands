#!/usr/bin/env python3
"""us-stock-advisor v5 — deterministic price ground truth (A4).

Install path: ~/.claude/skills/us-stock-advisor/scripts/fetch_indicators.py
(overwrites the v4 file; this is the ONE v4 component that worked and is kept,
with its four documented bugs fixed.)

Fixed vs v4:
  A4-1 partial bar dropped   — v4 fed today's in-progress bar in as a "close",
       which corrupted High/Low/ATR/support/resistance and made relative_volume
       structurally < 1 (killing the only path to signal_confidence 0.70).
  A4-2 auto_adjust=True      — v4's unadjusted closes step down on ex-div dates
       and biased SMA200/RSI/MACD.
  A4-3 signed signal         — v4's HOLD confidence was direction-blind: a
       confirmed downtrend (GLD, which then fell 12%) scored the SAME 0.60 as
       the benchmark. Now bearish states emit REDUCE/AVOID with a signed score.
  A4-4 data_age >= 0 or abort — v4 emitted data_age_hours = -6.1 in production and
       its staleness gate (stalest = max(0, ...)) then VACUOUSLY PASSED exactly on
       the runs whose data was worst.
Also: tickers deduped (v4 reported "succeeded 13 / requested 14"), ranked output.

NOTE ON AUTHORITY: this script informs. It does NOT decide. Regime, targets,
sizing and orders come from core.py. The `signal` field below may be used only
to (a) rank satellite candidates and (b) halve satellite size when RSI is
extreme. It may NOT veto the core. In the archived window the OVERBOUGHT veto
rejected 43 names that then returned +9.05% (+6.67pp vs QQQ).
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, time as dtime, timezone

MAX_AGE_H = 96


def _imp():
    try:
        import yfinance as yf
        import pandas as pd
        return yf, pd
    except ModuleNotFoundError:
        sys.stderr.write("ERROR: pip install --user yfinance pandas\n")
        sys.exit(2)


def _ema(s, p):
    return s.ewm(span=p, adjust=False).mean()


def _rsi(c, p=14):
    if len(c) < p + 1:
        return None
    d = c.diff().dropna()
    g, l = d.clip(lower=0.0), (-d).clip(lower=0.0)
    ag, al = g.iloc[:p].mean(), l.iloc[:p].mean()
    for x, y in zip(g.iloc[p:], l.iloc[p:]):
        ag = (ag * (p - 1) + x) / p
        al = (al * (p - 1) + y) / p
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + ag / al)


def _atr(h, lo, c, p=14):
    if len(c) < p + 1:
        return None
    pc = c.shift(1)
    tr = (h - lo).combine((h - pc).abs(), max).combine((lo - pc).abs(), max).dropna()
    if len(tr) < p:
        return None
    a = tr.iloc[:p].mean()
    for v in tr.iloc[p:]:
        a = (a * (p - 1) + v) / p
    return float(a)


def _r(x, n=2):
    if x is None:
        return None
    try:
        if x != x:
            return None
    except TypeError:
        pass
    return round(float(x), n)


def _signal(price, sma50, sma200, rsi, hist, rel_vol):
    """A4-3: SIGNED. score in [-4, +4]; confidence is |score| based and the
    direction is in the label, not lost."""
    score, reasons = 0, []
    if sma50 is not None:
        if price > sma50:
            score += 1; reasons.append("price > SMA50")
        else:
            score -= 1; reasons.append("price < SMA50")
    if sma200 is not None:
        if price > sma200:
            score += 1; reasons.append("price > SMA200")
        else:
            score -= 1; reasons.append("price < SMA200")
    if rsi is not None:
        if rsi > 50:
            score += 1; reasons.append(f"RSI {rsi:.1f} > 50")
        else:
            score -= 1; reasons.append(f"RSI {rsi:.1f} < 50")
    if hist is not None:
        if hist > 0:
            score += 1; reasons.append("MACD hist +")
        else:
            score -= 1; reasons.append("MACD hist -")

    if score >= 3:
        sig = "CONSTRUCTIVE"
    elif score <= -3:
        sig = "AVOID"          # a downtrend is NOT a "HOLD @ 0.60" like the index
    elif score <= -1:
        sig = "WEAK"
    else:
        sig = "NEUTRAL"
    conf = min(0.50 + 0.10 * abs(score) + (0.05 if (rel_vol or 0) > 1.2 else 0), 0.95)
    return sig, score, round(conf, 2), reasons


def _fetch(yf, tkr, attempt=0):
    try:
        h = yf.Ticker(tkr).history(period="1y", interval="1d", auto_adjust=True)  # A4-2
        if h is None or h.empty:
            raise RuntimeError("no data")
        return h
    except Exception:
        if attempt == 0:
            time.sleep(1.5)
            return _fetch(yf, tkr, 1)
        raise


def _compute(hist):
    today = datetime.now(timezone.utc).date()
    dropped = None
    if hist.index[-1].date() >= today:          # A4-1
        dropped = str(hist.index[-1].date())
        hist = hist.iloc[:-1]
    c, h, lo, v = hist["Close"], hist["High"], hist["Low"], hist["Volume"]
    last = hist.index[-1].date()
    close_utc = datetime.combine(last, dtime(21, 0), tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - close_utc).total_seconds() / 3600.0
    if age < 0:                                  # A4-4
        raise RuntimeError(f"negative data_age_hours ({age:.1f}) — partial bar leaked")

    price = float(c.iloc[-1])
    sma50 = float(c.rolling(50).mean().iloc[-1]) if len(c) >= 50 else None
    sma200 = float(c.rolling(200).mean().iloc[-1]) if len(c) >= 200 else None
    rsi = _rsi(c)
    if len(c) >= 26:
        ml = _ema(c, 12) - _ema(c, 26)
        sl = _ema(ml, 9)
        hist_v = float((ml - sl).iloc[-1])
    else:
        hist_v = None
    atr = _atr(h, lo, c)
    rel = None
    if len(v) >= 50:
        av = float(v.iloc[-50:].mean())
        if av > 0:
            rel = float(v.iloc[-1]) / av
    sig, score, conf, reasons = _signal(price, sma50, sma200, rsi, hist_v, rel)
    mom = None
    if len(c) >= 252:
        mom = float(c.iloc[-21] / c.iloc[-252] - 1.0) * 100   # 12-1 momentum
    return {
        "current_price": _r(price), "as_of_close_date": str(last),
        "data_age_hours": _r(age, 1), "partial_bar_dropped": dropped,
        "rsi_14": _r(rsi), "macd_histogram": _r(hist_v),
        "sma_50": _r(sma50), "sma_200": _r(sma200), "atr_14": _r(atr),
        "support_60d": _r(float(lo.iloc[-60:].min())),
        "resistance_60d": _r(float(h.iloc[-60:].max())),
        "relative_volume": _r(rel), "momentum_12_1_pct": _r(mom),
        "signal": sig, "signal_score": score, "signal_confidence": conf,
        "signal_reasons": reasons[:4],
        "stale": age > MAX_AGE_H,
    }


def main():
    if len(sys.argv) < 2:
        sys.stderr.write("Usage: fetch_indicators.py TICKER [TICKER ...]\n")
        sys.exit(2)
    yf, _pd = _imp()
    tickers = list(dict.fromkeys(t.upper() for t in sys.argv[1:]))   # dedupe (B7)
    res, errs, ages = {}, [], []
    for t in tickers:
        try:
            res[t] = _compute(_fetch(yf, t))
            ages.append(res[t]["data_age_hours"])
            sys.stderr.write(f"{t}: OK {res[t]['current_price']} "
                             f"{res[t]['signal']}({res[t]['signal_score']:+d})\n")
        except Exception as e:
            errs.append({"ticker": t, "reason": str(e)})
            sys.stderr.write(f"{t}: ERR {e}\n")
    ranked = sorted(res, key=lambda t: (res[t]["signal_score"],
                                        res[t]["momentum_12_1_pct"] or -999), reverse=True)
    out = {
        "as_of_run_iso": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tickers": res, "errors": errs,
        "ranking_by_signal_then_momentum": ranked,
        "summary": {"requested": len(tickers), "succeeded": len(res), "failed": len(errs),
                    "stalest_data_hours": _r(max(ages), 1) if ages else None,
                    "any_negative_age": False},
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))
    sys.exit(3 if not res else (1 if errs else 0))


if __name__ == "__main__":
    main()
