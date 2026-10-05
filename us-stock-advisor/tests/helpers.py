"""Shared builders for the v5.1 tests. Imported AFTER conftest.py redirected state."""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone

import pandas as pd

import config as C
import core as CORE
import score_recs as S

SK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(SK, "scripts")
FIX = os.path.join(SK, "tests", "fixtures")
HISTORY_CSV = os.path.join(FIX, "history_6y.csv")        # cached yfinance closes, <= 2026-09-29
LIVE_LEDGER_COPY = os.path.join(FIX, "live_ledger_33.jsonl")
LIVE_LAST_RUN_COPY = os.path.join(FIX, "live_last_run.json")

LIVE_PX = {"QQQ": 737.93, "QQQM": 303.83}


def utc_today():
    return datetime.now(timezone.utc).date()


def iso_hours_ago(h):
    return (datetime.now(timezone.utc) - timedelta(hours=h)).isoformat(timespec="seconds")


def run(script, *args, state, env_extra=None, path_prefix=None, timeout=600):
    """Run a script CLI with state redirected. Never inherits a live state dir."""
    env = dict(os.environ)
    env["US_ADVISOR_STATE"] = str(state)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if path_prefix:
        env["PATH"] = str(path_prefix) + os.pathsep + env.get("PATH", "")
    if env_extra:
        env.update(env_extra)
    exe = [sys.executable, os.path.join(SCRIPTS, script)] if script.endswith(".py") \
        else ["bash", os.path.join(SCRIPTS, script)]
    return subprocess.run(exe + [str(a) for a in args], env=env, capture_output=True,
                          text=True, timeout=timeout)


def make_prices_csv(path, last=None, trend=+1.0, days=420, end=None, extra=None):
    """Synthetic daily closes ending exactly at `last` on `end` (default: yesterday
    UTC, so the bar is complete and data_age_hours is positive and fresh).
    trend>0 -> rising monthly closes (TREND); trend<0 -> falling (DEFENSIVE)."""
    last = dict(last or LIVE_PX)
    last.update(extra or {})
    end = end or (utc_today() - timedelta(days=1))
    idx = pd.date_range(end=pd.Timestamp(end), periods=days, freq="D")
    cols = {}
    for j, (t, px) in enumerate(last.items()):
        vals = []
        for i in range(days):
            k = i - (days - 1)                       # 0 on the last bar
            vals.append(px * math.exp(trend * 0.0008 * k) * (1 + 0.01 * math.sin(k + j)
                                                             - 0.01 * math.sin(j)))
        cols[t] = vals
    df = pd.DataFrame(cols, index=idx)
    df.index.name = "Date"
    for t, px in last.items():
        df.loc[df.index[-1], t] = px                 # exact
    df.to_csv(path)
    return str(path)


def portfolio(cash, **shares):
    return {"cash_usd": cash,
            "positions": [{"ticker": t, "shares": float(s)} for t, s in shares.items()]}


def shadow_levels(prices, atr_frac=0.03, dte=None, rsi=55.0):
    out = {}
    for t, px in prices.items():
        lv = CORE.atr_levels(px, px * atr_frac)
        lv.update({"rsi": rsi, "days_to_earnings": (dte or {}).get(t), "atr_source": "ohlc"})
        out[t] = lv
    return out


def make_baseline(regime="TREND", prices=None, pf=None, levels=None, earnings=None,
                  satellite_state=None, atrs=None, weekly=False):
    prices = dict(prices or LIVE_PX)
    pf = pf or portfolio(392.25, QQQ=2)
    all_px = dict(prices)
    plan = CORE.build_plan({"regime": regime, "reason": "test", "eval_month": "2026-08-31"},
                           all_px, pf, satellite_state=satellite_state, atrs=atrs,
                           earnings=earnings, levels=levels, weekly=weekly)
    plan["data"] = {"last_bar": str(utc_today() - timedelta(days=1)),
                    "data_age_hours": 10.0, "partial_bar_dropped": None, "auto_adjust": True}
    if weekly:
        plan["mode"] = "weekly"
    return plan


def write_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    return str(path)


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def mark(d, total, cash, positions=None, **extra):
    r = {"type": "portfolio_mark", "date": str(d), "regime": "TREND", "total_usd": total,
         "cash_usd": cash, "cash_pct": round(cash / total * 100, 1) if total else 0.0,
         "positions": positions or {}, "deposit_usd": 0.0, "ticker": None, "action": "MARK"}
    r.update(extra)
    return r


def build_ledger(path, recs, anchor=True):
    """Chain `recs` into a fresh ledger with append_rec; optionally write an anchor
    (last_run.json next to it) at the resulting tip."""
    for r in recs:
        S.append_rec(str(path), dict(r))
    if anchor:
        tip, ln = S.tip_and_len(str(path))
        write_json(os.path.join(os.path.dirname(str(path)), "last_run.json"),
                   {"regime": "TREND", "ledger_tip_hash": tip, "ledger_len": ln})
    return str(path)
