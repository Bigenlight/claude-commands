#!/usr/bin/env python3
"""us-stock-advisor v6.0 — deterministic market data (P0 / watchdog / clerk support).

Install path: ~/.claude/skills/us-stock-advisor/scripts/fetch_indicators.py

Based on the v5.1.2 script, keeping its four fixes:
  A4-1 partial bar dropped    — a bar dated on/after the evaluation date ("today") is an
       in-progress session and never reaches any indicator.
  A4-2 auto_adjust=True       — adjusted closes (no ex-div steps in SMA/RSI/returns).
  A4-4 data_age >= 0 or abort — a negative age means a partial bar leaked: that ticker
       errors out instead of silently passing a staleness gate.
  dedupe                      — tickers deduped, order kept.
Removed vs v5.1.2: `_signal` / confidence / overbought labels, ATR `levels`, the `core`
import. This script INFORMS; it never decides. v6 adds returns 1/3/6m, relative strength
vs QQQ + rank over the sleeve-eligible universe, distance to the 52w high, next earnings
date, the regime flags (input only), `--since`, `--attach-since`, and an offline CSV mode.

CLI
---
  fetch_indicators.py --run-id R [--extra T ...] [--csv PATH] [--as-of YYYY-MM-DD] [--no-earnings]
      -> state/run/R/indicators.json; tickers = ALL_TICKERS + holdings.json positions + --extra
  fetch_indicators.py --tickers T ... [--csv PATH] [--as-of D] [--no-earnings]   -> stdout JSON
  fetch_indicators.py --since T:YYYY-MM-DD [--since ...] [--csv PATH] [--as-of D] -> stdout JSON
  fetch_indicators.py --attach-since run/R/evidence.json [--csv PATH] [--as-of D]
      -> fills items[].return_since_pct in place (atomic rewrite, idempotent)
Exit: 0 ok · 1 partial failures (errors non-empty) · 2 usage · 3 no data at all (nothing written)

--csv PATH: long CSV `date,ticker,open,high,low,close,volume`; no network, no earnings.
--as-of D : evaluation date for the partial-bar rule (default: the US-Eastern session date,
            rolled to the next day once the 16:00 ET close has passed — same rule as
            watchdog._session_as_of; the data_age_hours clock stays real now). With --as-of
            the reference clock for data_age_hours is D 12:00 UTC (deterministic).
            A bar's close instant is 16:00 America/New_York (DST-correct), not a fixed UTC hour.

Importable (pinned): load_frames, compute_all, regime_from_qqq, return_since,
fetch_earnings_dates. Plus: drop_partial_bar, monthly_closes, attach_since.

Notes: earnings_in_sessions uses numpy.busday_count (ignores US holidays; may overcount
by 1-2 sessions). Regime: UP iff QQQ close > SMA200 AND last COMPLETED monthly close >
10-month SMA; insufficient history defaults to UP ("default invested").
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402

RET_WINDOWS = {"1m": 21, "3m": 63, "6m": 126}
SESSIONS_52W = 252
NON_TICKER_ENTITIES = ("MACRO", "NDX")
US_EASTERN = ZoneInfo("America/New_York")
US_CLOSE_ET = dtime(16, 0)

# failures of the most recent load_frames() call: [{"ticker", "reason"}]
LAST_LOAD_ERRORS: list = []


def _pd():
    import pandas as pd
    return pd


# ----------------------------------------------------------------- small math (ported)
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


def _to_date(d):
    if d is None:
        return None
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    return date.fromisoformat(str(d)[:10])


def _now_utc():
    """Real wall clock (tz-aware UTC). Tests inject `now=` instead of relying on this."""
    return datetime.now(timezone.utc)


def _session_eval_date(now):
    """Default evaluation date for the partial-bar rule: the US-Eastern date, rolled to the
    next day once the 16:00 ET close has passed (mirrors watchdog._session_as_of). A Saturday
    08:30 KST run (= Friday evening ET) keeps Friday's completed bar; a run during US hours
    drops the in-progress bar. (UTC 'today' would drop the just-closed bar every evening ET.)"""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now.astimezone(US_EASTERN) + timedelta(hours=8)).date()


def _close_utc(d):
    """UTC instant of the 16:00 America/New_York close on session date d (DST-correct)."""
    return datetime.combine(d, US_CLOSE_ET, tzinfo=US_EASTERN).astimezone(timezone.utc)


def _ref_clock(as_of_date, now=None):
    """(evaluation date, reference datetime for data_age_hours).
    as_of_date None -> (US session date per _session_eval_date, real now); `now` (tz-aware,
    naive = UTC) injects the clock for tests. as_of_date given -> (D, D 12:00 UTC)."""
    if as_of_date is None:
        now = _now_utc() if now is None else now
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return _session_eval_date(now), now
    d = _to_date(as_of_date)
    return d, datetime.combine(d, dtime(12, 0), tzinfo=timezone.utc)


# ----------------------------------------------------------------- loading
def _normalize(df):
    """yfinance-style frame -> tz-naive, date-normalized, sorted, OHLCV float columns."""
    pd = _pd()
    df = df.copy()
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    df.index = idx.normalize()
    df = df[~df.index.duplicated(keep="last")].sort_index()
    cols = {c: c.capitalize() for c in df.columns}
    df = df.rename(columns=cols)
    keep = [c for c in ("Open", "High", "Low", "Close", "Volume") if c in df.columns]
    df = df[keep].dropna(subset=["Close"])
    return df


def _load_csv(tickers, csv_path):
    pd = _pd()
    raw = pd.read_csv(csv_path)
    raw.columns = [c.strip().lower() for c in raw.columns]
    need = {"date", "ticker", "close"}
    if not need.issubset(raw.columns):
        raise ValueError(f"CSV {csv_path} needs columns {sorted(need)}; has {list(raw.columns)}")
    raw["ticker"] = raw["ticker"].astype(str).str.strip().str.upper()
    raw["date"] = pd.to_datetime(raw["date"])
    for c in ("open", "high", "low", "volume"):
        if c not in raw.columns:
            raw[c] = raw["close"] if c != "volume" else 0
    out = {}
    for t in tickers:
        sub = raw[raw["ticker"] == t]
        if sub.empty:
            LAST_LOAD_ERRORS.append({"ticker": t, "reason": "not in CSV"})
            continue
        df = sub.set_index("date")[["open", "high", "low", "close", "volume"]]
        out[t] = _normalize(df)
    return out


def _fetch_yf(yf, tkr, period, attempt=0):
    try:
        h = yf.Ticker(tkr).history(period=period, interval="1d", auto_adjust=True)  # A4-2
        if h is None or h.empty:
            raise RuntimeError("no data")
        return h
    except Exception:
        if attempt == 0:
            time.sleep(1.5)
            return _fetch_yf(yf, tkr, period, 1)
        raise


def load_frames(tickers, csv_path=None, period=C.HISTORY_PERIOD):
    """{T: DataFrame(Open,High,Low,Close,Volume)} for every ticker that loaded. Failures are
    omitted from the result and recorded in fetch_indicators.LAST_LOAD_ERRORS.
    csv_path -> offline (no network). Otherwise yfinance history(period, auto_adjust=True)."""
    del LAST_LOAD_ERRORS[:]
    tickers = list(dict.fromkeys(str(t).strip().upper() for t in tickers if str(t).strip()))
    if csv_path:
        return _load_csv(tickers, csv_path)
    try:
        import yfinance as yf
    except ModuleNotFoundError:
        sys.stderr.write("ERROR: pip install --user yfinance pandas\n")
        sys.exit(2)
    out = {}
    for t in tickers:
        try:
            out[t] = _normalize(_fetch_yf(yf, t, period))
            if out[t].empty:
                raise RuntimeError("no data after normalize")
        except Exception as e:
            out.pop(t, None)
            LAST_LOAD_ERRORS.append({"ticker": t, "reason": str(e)[:200]})
    return out


def fetch_earnings_dates(tickers):
    """{T: 'YYYY-MM-DD' | None} next earnings date (network). ETFs -> None without a call;
    any exception -> None. Port of v5.1.2 core.earnings_days, returning the date."""
    out = {}
    try:
        import yfinance as yf
        import pandas as pd
    except ModuleNotFoundError:
        return {t: None for t in tickers}
    for t in dict.fromkeys(tickers):
        g = C.group_of(t)
        if g in ("core", "broad", "sector", "derisk"):
            out[t] = None
            continue
        try:
            cal = yf.Ticker(t).get_earnings_dates(limit=8)
            if cal is None or len(cal) == 0:
                out[t] = None
                continue
            now = pd.Timestamp.now(tz=cal.index.tz)
            fut = [d for d in cal.index if d > now]
            if not fut:
                out[t] = None
                continue
            nxt = min(fut)
            if nxt.tzinfo is not None:
                nxt = nxt.tz_convert("America/New_York")
            out[t] = str(nxt.date())
        except Exception:
            out[t] = None
    return out


# ----------------------------------------------------------------- pure computations
def drop_partial_bar(frame, as_of_date):
    """A4-1: drop every bar dated on/after as_of_date. Returns (frame, dropped_date|None)."""
    d = _to_date(as_of_date)
    if frame is None or frame.empty:
        return frame, None
    mask = frame.index.date >= d
    if not mask.any():
        return frame, None
    dropped = str(frame.index[mask][0].date())
    return frame[~mask], dropped


def monthly_closes(close_series):
    """COMPLETED months only (port of v5.1.2 core.monthly_closes). The running month's
    bucket is always removed, even when the last bar is the month's last session."""
    m = close_series.resample("ME").last().dropna()
    if len(m) and m.index[-1].to_period("M") >= close_series.index[-1].to_period("M"):
        m = m.iloc[:-1]
    return m


def regime_from_qqq(frame):
    """Regime flags from a (partial-bar-free) QQQ frame. Input only, never a trade."""
    c = frame["Close"] if frame is not None and not frame.empty else None
    if c is None:
        return {"regime": C.REGIME_UP, "qqq_close": None, "sma_200": None, "above_sma200": None,
                "monthly_close": None, "sma_10m": None, "above_sma10m": None,
                "eval_month": None, "reason": "no QQQ data; default invested"}
    close = float(c.iloc[-1])
    sma200 = float(c.rolling(C.REGIME_SMA_DAYS).mean().iloc[-1]) if len(c) >= C.REGIME_SMA_DAYS else None
    m = monthly_closes(c)
    mclose = float(m.iloc[-1]) if len(m) else None
    eval_month = str(m.index[-1].date()) if len(m) else None
    sma10 = (float(m.rolling(C.REGIME_SMA_MONTHS).mean().iloc[-1])
             if len(m) >= C.REGIME_SMA_MONTHS + 1 else None)
    above200 = None if sma200 is None else close > sma200
    above10 = None if sma10 is None else mclose > sma10
    out = {"qqq_close": _r(close), "sma_200": _r(sma200), "above_sma200": above200,
           "monthly_close": _r(mclose), "sma_10m": _r(sma10), "above_sma10m": above10,
           "eval_month": eval_month}
    if sma200 is None or sma10 is None:
        out["regime"] = C.REGIME_UP
        out["reason"] = (f"insufficient history ({len(c)} sessions, {len(m)} completed months); "
                         "default invested")
    else:
        out["regime"] = C.REGIME_UP if (above200 and above10) else C.REGIME_DOWN
        out["reason"] = (f"QQQ {close:.2f} {'>' if above200 else '<='} SMA200 {sma200:.2f}; "
                         f"monthly close {mclose:.2f} {'>' if above10 else '<='} "
                         f"10M SMA {sma10:.2f} ({eval_month})")
    return {k: out[k] for k in ("regime", "qqq_close", "sma_200", "above_sma200", "monthly_close",
                                "sma_10m", "above_sma10m", "eval_month", "reason")}


def return_since(frame, since_date):
    """Close on the first session >= since_date -> latest close in `frame`."""
    d = _to_date(since_date)
    out = {"since": str(d), "since_session_date": None, "close_at_since": None,
           "close_now": None, "return_since_pct": None}
    if frame is None or frame.empty:
        return out
    c = frame["Close"]
    out["close_now"] = _r(c.iloc[-1])
    after = c[c.index.date >= d]
    if after.empty:
        return out
    base = float(after.iloc[0])
    out["since_session_date"] = str(after.index[0].date())
    out["close_at_since"] = _r(base)
    if base > 0:
        out["return_since_pct"] = _r((float(c.iloc[-1]) / base - 1.0) * 100.0)
    return out


def _ret(c, k):
    if len(c) <= k:
        return None
    base = float(c.iloc[-1 - k])
    return None if base <= 0 else (float(c.iloc[-1]) / base - 1.0) * 100.0


def _compute_one(frame, now_ref, partial_dropped):
    pd = _pd()
    c, h, lo = frame["Close"], frame["High"], frame["Low"]
    last = frame.index[-1].date()
    close_utc = _close_utc(last)                                  # 16:00 ET, DST-correct
    age = (now_ref - close_utc).total_seconds() / 3600.0
    if age < 0:                                                   # A4-4
        raise RuntimeError(f"negative data_age_hours ({age:.1f}) — partial bar leaked")
    price = float(c.iloc[-1])
    hi52 = float(h.iloc[-SESSIONS_52W:].max())
    hi52 = max(hi52, price)
    rets = {k: _ret(c, n) for k, n in RET_WINDOWS.items()}
    return {
        "price": _r(price), "as_of_close_date": str(last), "data_age_hours": _r(age, 1),
        "partial_bar_dropped": partial_dropped,
        "sma_50": _r(c.rolling(50).mean().iloc[-1]) if len(c) >= 50 else None,
        "sma_200": _r(c.rolling(200).mean().iloc[-1]) if len(c) >= 200 else None,
        "rsi_14": _r(_rsi(c)), "atr_14": _r(_atr(h, lo, c)),
        "chg_1d_pct": _r(_ret(c, 1)), "chg_5d_pct": _r(_ret(c, 5)),
        "_rets": rets,
        "ret_1m_pct": _r(rets["1m"]), "ret_3m_pct": _r(rets["3m"]), "ret_6m_pct": _r(rets["6m"]),
        "rs_1m_pp": None, "rs_3m_pp": None, "rs_6m_pp": None, "rs_score": None, "rs_rank": None,
        "high_52w": _r(hi52), "pct_from_52w_high": _r((price / hi52 - 1.0) * 100.0) if hi52 > 0 else None,
        "next_earnings_date": None, "earnings_in_sessions": None,
        "stale": age > C.MAX_DATA_AGE_HOURS,
    }


def compute_all(frames, as_of_date=None, earnings=None, requested=None, *, now=None):
    """Pure: frames {T: DataFrame} -> indicators/v6 dict (see the v6 team plan schema).
    as_of_date: evaluation date (partial-bar rule); None -> US-Eastern session date (rolled
    forward after the 16:00 ET close), with data_age_hours measured from real now.
    now: optional injected clock (tests) used only when as_of_date is None.
    earnings: {T: date|None}. requested: tickers expected (missing ones are reported in `errors`)."""
    import numpy as np
    as_of, now_ref = _ref_clock(as_of_date, now=now)
    earnings = earnings or {}
    frames = {str(t).upper(): f for t, f in (frames or {}).items()}
    req = list(dict.fromkeys([str(t).upper() for t in (requested or [])] + list(frames)))
    tick, errs, raw_rets = {}, [], {}
    clean = {}
    for t in req:
        f = frames.get(t)
        if f is None or getattr(f, "empty", True):
            errs.append({"ticker": t, "reason": "no data"})
            continue
        try:
            f2, dropped = drop_partial_bar(f, as_of)
            if f2.empty:
                raise RuntimeError("no completed session before as_of")
            rec = _compute_one(f2, now_ref, dropped)
            raw_rets[t] = rec.pop("_rets")
            tick[t] = rec
            clean[t] = f2
        except Exception as e:
            errs.append({"ticker": t, "reason": str(e)[:200]})

    bench = C.BENCHMARK
    regime = regime_from_qqq(clean.get(bench))
    if bench in raw_rets:
        b = raw_rets[bench]
        for t, rr in raw_rets.items():
            rs = {k: (None if rr[k] is None or b[k] is None else rr[k] - b[k]) for k in RET_WINDOWS}
            for k in RET_WINDOWS:
                tick[t][f"rs_{k}_pp"] = _r(rs[k])
            avail = [v for v in (rs["3m"], rs["6m"]) if v is not None]
            score = sum(avail) / len(avail) if avail else None
            tick[t]["rs_score"] = _r(score, 3)
            tick[t]["_score"] = score
    ranked = sorted((t for t in tick if t in C.SLEEVE_ELIGIBLE and tick[t].get("_score") is not None),
                    key=lambda t: (-tick[t]["_score"], t))
    for i, t in enumerate(ranked, 1):
        tick[t]["rs_rank"] = i
    for t in tick:
        tick[t].pop("_score", None)
        nd = earnings.get(t)
        if nd:
            try:
                n = int(np.busday_count(tick[t]["as_of_close_date"], str(nd)[:10]))
                if n >= 0:
                    tick[t]["next_earnings_date"] = str(nd)[:10]
                    tick[t]["earnings_in_sessions"] = n
            except Exception:
                pass

    ages = [v["data_age_hours"] for v in tick.values() if v.get("data_age_hours") is not None]
    if bench in tick:
        close_date = tick[bench]["as_of_close_date"]
    else:
        close_date = max((v["as_of_close_date"] for v in tick.values()), default=None)
    if bench not in tick and not any(e["ticker"] == bench for e in errs):
        errs.append({"ticker": bench, "reason": "benchmark missing: RS and regime unavailable"})
    return {
        "schema": "indicators/v6",
        "as_of_run_iso": now_ref.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "as_of_close_date": close_date, "benchmark": bench, "regime": regime,
        "tickers": tick, "rs_ranking": ranked, "errors": errs,
        "summary": {"requested": len(req), "succeeded": len(tick), "failed": len(req) - len(tick),
                    "stalest_data_hours": _r(max(ages), 1) if ages else None},
    }


# ----------------------------------------------------------------- evidence attach
def _published_session_date(iso):
    """published_iso -> US/Eastern calendar date (naive ISO -> its own date)."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            return date.fromisoformat(str(iso)[:10])
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.date()
    from zoneinfo import ZoneInfo
    return dt.astimezone(ZoneInfo("America/New_York")).date()


def _item_ticker(it):
    ent = str(it.get("entity") or "").strip().upper()
    if ent in NON_TICKER_ENTITIES:
        return None
    ts = it.get("tickers") or []
    if not ts:
        return None
    t = str(ts[0]).strip().upper()
    return None if t in NON_TICKER_ENTITIES else t


def attach_since(path, csv_path=None, as_of_date=None):
    """Fill items[].return_since_pct in evidence.json in place (atomic, idempotent).
    Returns {"filled": n, "null": n, "errors": [...]}."""
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    items = doc.get("items") or []
    need = sorted({t for t in (_item_ticker(it) for it in items) if t})
    frames = load_frames(need, csv_path=csv_path) if need else {}
    errs = list(LAST_LOAD_ERRORS)
    as_of, _ = _ref_clock(as_of_date)
    clean = {t: drop_partial_bar(f, as_of)[0] for t, f in frames.items()}
    filled = nulls = 0
    for it in items:
        t = _item_ticker(it)
        d = _published_session_date(it.get("published_iso"))
        val = None
        if t and d and t in clean:
            val = return_since(clean[t], d)["return_since_pct"]
        it["return_since_pct"] = val
        if val is None:
            nulls += 1
        else:
            filled += 1
    _atomic_write_json(path, doc)
    return {"filled": filled, "null": nulls, "errors": errs}


def _atomic_write_json(path, obj):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, f".{os.path.basename(path)}.tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ----------------------------------------------------------------- CLI
def _earnings_for(tickers, args):
    if args.csv or args.no_earnings:
        return {}
    names = [t for t in tickers if C.group_of(t) not in ("core", "broad", "sector", "derisk")]
    return fetch_earnings_dates(names)


def _run_indicators(tickers, args):
    frames = load_frames(tickers, csv_path=args.csv)
    load_errs = list(LAST_LOAD_ERRORS)
    if not frames:
        return None, load_errs
    ind = compute_all(frames, as_of_date=args.as_of, earnings=_earnings_for(list(frames), args),
                      requested=tickers)
    # replace generic "no data" with the loader's reason
    reasons = {e["ticker"]: e["reason"] for e in load_errs}
    for e in ind["errors"]:
        if e["ticker"] in reasons and e["reason"] == "no data":
            e["reason"] = reasons[e["ticker"]]
    return ind, load_errs


def _parse_args(argv):
    p = argparse.ArgumentParser(prog="fetch_indicators.py", description=__doc__.split("\n")[0])
    m = p.add_mutually_exclusive_group(required=True)
    m.add_argument("--run-id", help="write state/run/<RUN_ID>/indicators.json")
    m.add_argument("--tickers", nargs="+", help="print indicators for these tickers")
    m.add_argument("--since", action="append", metavar="T:YYYY-MM-DD", help="return since a date")
    m.add_argument("--attach-since", metavar="EVIDENCE_JSON", help="fill return_since_pct in place")
    p.add_argument("--extra", nargs="+", default=[], help="(--run-id) extra tickers")
    p.add_argument("--csv", help="offline long CSV date,ticker,open,high,low,close,volume")
    p.add_argument("--as-of", help="evaluation date YYYY-MM-DD (default: US-Eastern session "
                   "date, rolled forward after the 16:00 ET close)")
    p.add_argument("--no-earnings", action="store_true", help="skip the earnings-date lookup")
    a = p.parse_args(argv)
    if a.as_of:
        try:
            date.fromisoformat(a.as_of)
        except ValueError:
            p.error(f"--as-of must be YYYY-MM-DD, got {a.as_of!r}")
    if a.extra and not a.run_id:
        p.error("--extra is only valid with --run-id")
    return a


def main(argv=None):
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    if args.attach_since:
        if not os.path.isfile(args.attach_since):
            sys.stderr.write(f"ERROR: {args.attach_since} not found\n")
            return 2
        try:
            res = attach_since(args.attach_since, csv_path=args.csv, as_of_date=args.as_of)
        except (ValueError, json.JSONDecodeError) as e:
            sys.stderr.write(f"ERROR: {e}\n")
            return 2
        print(json.dumps(res, ensure_ascii=False))
        return 1 if res["errors"] else 0

    if args.since:
        specs = []
        for s in args.since:
            if ":" not in s:
                sys.stderr.write(f"ERROR: --since expects T:YYYY-MM-DD, got {s!r}\n")
                return 2
            t, d = s.split(":", 1)
            try:
                date.fromisoformat(d)
            except ValueError:
                sys.stderr.write(f"ERROR: bad date in --since {s!r}\n")
                return 2
            specs.append((t.strip().upper(), d))
        frames = load_frames([t for t, _ in specs], csv_path=args.csv)
        errs = list(LAST_LOAD_ERRORS)
        if not frames:
            sys.stderr.write("ERROR: no data for any ticker\n")
            return 3
        as_of, _ = _ref_clock(args.as_of)
        rows = []
        for t, d in specs:
            f = frames.get(t)
            if f is None:
                rows.append({"ticker": t, "since": d, "close_at_since": None, "close_now": None,
                             "return_since_pct": None})
                continue
            r = return_since(drop_partial_bar(f, as_of)[0], d)
            rows.append({"ticker": t, "since": d, "since_session_date": r["since_session_date"],
                         "close_at_since": r["close_at_since"], "close_now": r["close_now"],
                         "return_since_pct": r["return_since_pct"]})
        print(json.dumps({"since": rows, "errors": errs}, indent=2, ensure_ascii=False))
        return 1 if errs else 0

    if args.tickers:
        tickers = list(dict.fromkeys(t.strip().upper() for t in args.tickers if t.strip()))
        if C.BENCHMARK not in tickers:
            tickers.append(C.BENCHMARK)          # RS needs the benchmark
        ind, _ = _run_indicators(tickers, args)
        if ind is None or not ind["tickers"]:
            sys.stderr.write("ERROR: no data for any ticker\n")
            return 3
        print(json.dumps(ind, indent=2, ensure_ascii=False))
        return 1 if ind["errors"] else 0

    # --run-id
    rd = C.run_dir(args.run_id)
    held = []
    hp = os.path.join(rd, "holdings.json")
    if os.path.isfile(hp):
        try:
            with open(hp, encoding="utf-8") as f:
                held = list((json.load(f).get("positions") or {}).keys())
        except (OSError, ValueError) as e:
            sys.stderr.write(f"WARN: unreadable {hp}: {e}\n")
    tickers = list(dict.fromkeys(list(C.ALL_TICKERS) + [t.upper() for t in held]
                                 + [t.strip().upper() for t in args.extra]))
    ind, _ = _run_indicators(tickers, args)
    if ind is None or not ind["tickers"]:
        sys.stderr.write("ERROR: no data at all; indicators.json NOT written\n")
        return 3
    ind["run_id"] = args.run_id
    _atomic_write_json(os.path.join(rd, "indicators.json"), ind)
    s = ind["summary"]
    sys.stderr.write(f"indicators: {s['succeeded']}/{s['requested']} ok, close {ind['as_of_close_date']}, "
                     f"regime {ind['regime']['regime']}, errors {len(ind['errors'])}\n")
    return 1 if ind["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
