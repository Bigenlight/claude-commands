#!/usr/bin/env python3
"""Deterministic technical-indicator fetcher for the us-stock-advisor skill.

Replaces the WebSearch-based Phase 1 Agent 4, which produced 5%+ stale price
spreads, wrong ATH attributions, and 1-day-shifted technical levels.

Invocation:
    python3 fetch_indicators.py NVDA GOOGL MSFT META AMZN AAPL TSLA AMD TSM

stdout: pretty-printed JSON (one object) with per-ticker indicators.
stderr: human-readable status lines, one per ticker.

Exit codes:
    0  - all tickers fetched successfully
    1  - partial success (1+ tickers failed, but >=1 OK)
    2  - yfinance import failed (install instructions printed to stderr)
    3  - complete failure (all tickers errored)

Indicators computed locally (no TA-Lib / pandas-ta dependency):
    - RSI(14)         Wilder smoothing
    - MACD(12,26,9)   line, signal, histogram
    - SMA 20/50/200
    - EMA 20
    - ATR(14)         Wilder smoothing
    - support_60d / resistance_60d  (rolling min/max of last 60 lows/highs)
    - relative_volume   latest vol / 50d avg vol
"""

from __future__ import annotations
import json
import sys
import time
from datetime import datetime, timezone


def _import_yfinance():
    try:
        import yfinance as yf  # noqa
        import pandas as pd   # noqa
        return yf, pd
    except ModuleNotFoundError:
        sys.stderr.write(
            "ERROR: yfinance not installed.\n"
            "Install with: python3 -m pip install --user --break-system-packages yfinance pandas\n"
            "(Or in a venv. Required for the us-stock-advisor skill's deterministic price layer.)\n"
        )
        sys.exit(2)


def _wilder_rsi(closes, period: int = 14):
    """Wilder's RSI(14). closes: pandas Series. Returns final RSI value (float)."""
    if len(closes) < period + 1:
        return None
    diffs = closes.diff().dropna()
    gains = diffs.clip(lower=0.0)
    losses = (-diffs).clip(lower=0.0)
    # initial averages: simple mean of first `period` values
    avg_gain = gains.iloc[:period].mean()
    avg_loss = gains.iloc[:period].mean()  # placeholder, fixed below
    avg_loss = losses.iloc[:period].mean()
    # Wilder smoothing for remaining
    for g, l in zip(gains.iloc[period:], losses.iloc[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _ema(series, period: int):
    """Standard EMA, alpha = 2/(period+1)."""
    return series.ewm(span=period, adjust=False).mean()


def _wilder_atr(highs, lows, closes, period: int = 14):
    """Wilder's ATR(14)."""
    if len(closes) < period + 1:
        return None
    prev_close = closes.shift(1)
    tr = (highs - lows).combine((highs - prev_close).abs(), max).combine(
        (lows - prev_close).abs(), max
    )
    tr = tr.dropna()
    if len(tr) < period:
        return None
    atr = tr.iloc[:period].mean()
    for v in tr.iloc[period:]:
        atr = (atr * (period - 1) + v) / period
    return float(atr)


def _round(x, n=2):
    if x is None:
        return None
    try:
        if x != x:  # NaN
            return None
    except TypeError:
        pass
    return round(float(x), n)


def _signal(price, sma50, sma200, rsi, macd_hist, rel_vol):
    """Transparent BUY/HOLD/SELL signal + confidence."""
    reasons = []
    bull = 0
    bear = 0
    if sma50 is not None and price > sma50:
        bull += 1
        reasons.append("price > SMA50")
    elif sma50 is not None and price < sma50:
        bear += 1
        reasons.append("price < SMA50")
    if sma200 is not None and price > sma200:
        bull += 1
        reasons.append("price > SMA200")
    elif sma200 is not None and price < sma200:
        bear += 1
        reasons.append("price < SMA200")
    if rsi is not None and rsi > 50:
        bull += 1
        if rsi > 70:
            reasons.append(f"RSI {rsi:.1f} (overbought)")
        else:
            reasons.append(f"RSI {rsi:.1f} > 50")
    elif rsi is not None and rsi < 50:
        bear += 1
        if rsi < 30:
            reasons.append(f"RSI {rsi:.1f} (oversold)")
        else:
            reasons.append(f"RSI {rsi:.1f} < 50")
    if macd_hist is not None and macd_hist > 0:
        bull += 1
        reasons.append("MACD histogram positive")
    elif macd_hist is not None and macd_hist < 0:
        bear += 1
        reasons.append("MACD histogram negative")

    if bull >= 4 and bear == 0:
        sig = "BUY"
        conf = 0.65 + 0.05 * min(bull - 4 + (1 if rel_vol and rel_vol > 1.2 else 0), 5)
    elif bear >= 4 and bull == 0:
        sig = "SELL"
        conf = 0.65 + 0.05 * min(bear - 4 + (1 if rel_vol and rel_vol > 1.2 else 0), 5)
    elif bull > bear:
        sig = "HOLD"
        conf = 0.50 + 0.05 * (bull - bear)
    elif bear > bull:
        sig = "HOLD"
        conf = 0.50 + 0.05 * (bear - bull)
    else:
        sig = "HOLD"
        conf = 0.45
    if conf > 0.90:
        conf = 0.90
    if conf < 0.30:
        conf = 0.30
    return sig, round(conf, 2), reasons[:4]


def _fetch_one(yf, pd, ticker: str, attempt: int = 0):
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="1y", interval="1d", auto_adjust=False)
        if hist is None or hist.empty:
            raise RuntimeError("no data returned from yfinance")
        return hist
    except Exception as e:  # noqa: BLE001
        if attempt == 0:
            time.sleep(1)
            return _fetch_one(yf, pd, ticker, attempt=1)
        raise


def _compute(ticker: str, hist) -> dict:
    closes = hist["Close"]
    highs = hist["High"]
    lows = hist["Low"]
    vols = hist["Volume"]
    last_idx = hist.index[-1]
    as_of_close_date = str(last_idx.date())
    # Compute data_age_hours assuming bar's close at 20:00 UTC (16:00 ET)
    bar_close_utc = datetime.combine(
        last_idx.date(), datetime.min.time(), tzinfo=timezone.utc
    ).replace(hour=20)
    age_hours = (datetime.now(timezone.utc) - bar_close_utc).total_seconds() / 3600.0

    price = float(closes.iloc[-1])
    prev_close = float(closes.iloc[-2]) if len(closes) >= 2 else None
    day_change_pct = (
        ((price - prev_close) / prev_close * 100.0) if prev_close else None
    )

    sma_20 = float(closes.rolling(20).mean().iloc[-1]) if len(closes) >= 20 else None
    sma_50 = float(closes.rolling(50).mean().iloc[-1]) if len(closes) >= 50 else None
    sma_200 = float(closes.rolling(200).mean().iloc[-1]) if len(closes) >= 200 else None
    ema_20 = float(_ema(closes, 20).iloc[-1]) if len(closes) >= 20 else None

    rsi = _wilder_rsi(closes, 14)

    if len(closes) >= 26:
        ema12 = _ema(closes, 12)
        ema26 = _ema(closes, 26)
        macd_line = ema12 - ema26
        macd_signal = _ema(macd_line, 9)
        macd_hist = macd_line - macd_signal
        macd_line_v = float(macd_line.iloc[-1])
        macd_signal_v = float(macd_signal.iloc[-1])
        macd_hist_v = float(macd_hist.iloc[-1])
    else:
        macd_line_v = macd_signal_v = macd_hist_v = None

    atr = _wilder_atr(highs, lows, closes, 14)

    if len(lows) >= 60:
        support = float(lows.iloc[-60:].min())
        resistance = float(highs.iloc[-60:].max())
    else:
        support = float(lows.min())
        resistance = float(highs.max())

    rel_vol = None
    if len(vols) >= 50:
        avg_vol = float(vols.iloc[-50:].mean())
        if avg_vol > 0:
            rel_vol = float(vols.iloc[-1]) / avg_vol

    warnings = []
    if len(closes) < 200:
        warnings.append(f"only {len(closes)} bars available; SMA200 may be null")
    if age_hours > 96:
        warnings.append(f"STALE: data older than 4 days ({age_hours:.1f}h)")

    sig, conf, reasons = _signal(price, sma_50, sma_200, rsi, macd_hist_v, rel_vol)

    return {
        "current_price": _round(price),
        "prev_close": _round(prev_close),
        "day_change_pct": _round(day_change_pct),
        "as_of_close_date": as_of_close_date,
        "data_age_hours": _round(age_hours, 1),
        "rsi_14": _round(rsi),
        "macd_line": _round(macd_line_v),
        "macd_signal_line": _round(macd_signal_v),
        "macd_histogram": _round(macd_hist_v),
        "sma_20": _round(sma_20),
        "sma_50": _round(sma_50),
        "sma_200": _round(sma_200),
        "ema_20": _round(ema_20),
        "atr_14": _round(atr),
        "support_60d": _round(support),
        "resistance_60d": _round(resistance),
        "relative_volume": _round(rel_vol),
        "above_sma_50": (sma_50 is not None and price > sma_50),
        "above_sma_200": (sma_200 is not None and price > sma_200),
        "signal": sig,
        "signal_confidence": conf,
        "signal_reasons": reasons,
        "warnings": warnings,
        "data_complete": len(closes) >= 200,
    }


def main():
    if len(sys.argv) < 2:
        sys.stderr.write("Usage: fetch_indicators.py TICKER [TICKER ...]\n")
        sys.exit(2)

    yf, pd = _import_yfinance()
    tickers = [t.upper() for t in sys.argv[1:]]

    results: dict = {}
    errors: list = []
    stalest = 0.0

    for tkr in tickers:
        try:
            hist = _fetch_one(yf, pd, tkr)
            data = _compute(tkr, hist)
            results[tkr] = data
            if data["data_age_hours"] and data["data_age_hours"] > stalest:
                stalest = data["data_age_hours"]
            sys.stderr.write(
                f"{tkr}: OK price={data['current_price']} "
                f"close_date={data['as_of_close_date']} "
                f"signal={data['signal']}@{data['signal_confidence']}\n"
            )
        except Exception as e:  # noqa: BLE001
            err = str(e)
            errors.append({"ticker": tkr, "reason": err})
            sys.stderr.write(f"{tkr}: ERR: {err}\n")

    out = {
        "as_of_run_iso": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tickers": results,
        "errors": errors,
        "summary": {
            "total_requested": len(tickers),
            "succeeded": len(results),
            "failed": len(errors),
            "stalest_data_hours": _round(stalest, 1),
        },
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))

    if not results:
        sys.exit(3)
    if errors:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
