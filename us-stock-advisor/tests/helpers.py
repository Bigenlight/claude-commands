"""Shared offline test helpers for us-stock-advisor v6 (used by Teams 1, 2 and 3).

Everything here is deterministic and offline. Script modules (config, book,
fetch_indicators) are imported LAZILY inside functions, so `import helpers` is safe
at any time after conftest has redirected US_ADVISOR_STATE.

Public API
----------
FIXED_END_DATE = "2026-10-07"   last synthetic session (as_of_close_date)
FIXED_AS_OF    = "2026-10-08"   evaluation date passed to compute_all
TODAY_BOOK     = "QQQ 2, QQQM 1, cash 68.59"   (total 1,895.99 at today_prices())
today_prices() -> dict[str, float]
synthetic_frames(tickers, days=520, seed=7, start_prices=None, *, end_date=FIXED_END_DATE,
                 end_prices=None, drift=None, vol=None) -> dict[str, DataFrame]
write_csv(frames, path) -> str
first_run_digest(run_id) -> dict          empty-ledger digest/v6 defaults
make_run(tmp_state, run_id, holdings_text=TODAY_BOOK, prices=None, regime="UP", *,
         as_of=FIXED_AS_OF, earnings=None, digest=None, extra_tickers=(),
         rs_order=DEFAULT_RS_ORDER) -> str (run dir)
    default menu on today's book: ["NVDA","AMZN","PLTR","XLK","SOXQ","IGV"], regime as asked
read_json(path) / write_json(path, obj)
"""
from __future__ import annotations

import json
import os
import zlib

import numpy as np
import pandas as pd

FIXED_END_DATE = "2026-10-07"
FIXED_AS_OF = "2026-10-08"
TODAY_BOOK = "QQQ 2, QQQM 1, cash 68.59"

_TODAY_PRICES = {
    "QQQ": 757.73, "QQQM": 311.94, "NVDA": 237.47, "GOOGL": 350.50, "MSFT": 529.76,
    "META": 721.31, "AMZN": 259.92, "AAPL": 336.67, "AMD": 645.86, "TSM": 472.20,
    "AVGO": 376.51, "MU": 1088.00, "PLTR": 194.12, "SOXX": 582.82, "SMH": 625.03,
    "SOXQ": 103.0, "XLK": 201.0, "IGV": 110.0, "SPYM": 91.0, "SGOV": 100.0,
}

# make_run's default relative-strength order (strongest first). These names get strong,
# low-noise drifts so the RS ranking and the candidate menu are deterministic and match
# the plan's example menu: ["NVDA","AMZN","PLTR","XLK","SOXQ","IGV"].
DEFAULT_RS_ORDER = ("NVDA", "AMZN", "PLTR", "XLK", "SOXQ", "IGV")

# Regime-shaping drift/vol for QQQ in make_run (strong, low-noise trends so the
# regime label is unambiguous on both the 200-day and the 10-month test).
_REGIME_SHAPE = {"UP": (0.0015, 0.006), "DOWN": (-0.0015, 0.006)}


def today_prices() -> dict:
    """Today's book facts (v6 plan): 10-07 closes; SOXQ/XLK/IGV/SPYM/SGOV approximate."""
    return dict(_TODAY_PRICES)


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    return path


def _seed_for(ticker: str, seed: int) -> int:
    # zlib.crc32 is stable across processes (str hash() is salted per process).
    return (zlib.crc32(ticker.encode()) + int(seed) * 1_000_003) % (2 ** 32)


def synthetic_frames(tickers, days=520, seed=7, start_prices=None, *, end_date=FIXED_END_DATE,
                     end_prices=None, drift=None, vol=None):
    """Deterministic random-walk OHLCV frames (yfinance column names Open/High/Low/Close/
    Volume) on a business-day DatetimeIndex ending on `end_date`.

    start_prices: {T: first close} (default 100). end_prices: {T: last close}; when given
    the whole frame is rescaled so the last Close equals it exactly. drift/vol: {T: daily
    log-drift / daily log-vol} overrides (defaults 0.0004 / 0.015)."""
    start_prices = start_prices or {}
    end_prices = end_prices or {}
    drift = drift or {}
    vol = vol or {}
    idx = pd.bdate_range(end=pd.Timestamp(end_date), periods=int(days))
    out = {}
    for t in dict.fromkeys(tickers):
        rng = np.random.default_rng(_seed_for(t, seed))
        mu, sd = float(drift.get(t, 0.0004)), float(vol.get(t, 0.015))
        r = rng.normal(mu, sd, len(idx))
        close = float(start_prices.get(t, 100.0)) * np.exp(np.cumsum(r))
        prev = np.concatenate([[close[0]], close[:-1]])
        opn = prev * (1 + rng.normal(0, sd / 3, len(idx)))
        hi = np.maximum(opn, close) * (1 + np.abs(rng.normal(0, sd / 2, len(idx))))
        lo = np.minimum(opn, close) * (1 - np.abs(rng.normal(0, sd / 2, len(idx))))
        volu = rng.integers(1_000_000, 5_000_000, len(idx))
        if t in end_prices and end_prices[t]:
            k = float(end_prices[t]) / close[-1]
            close, opn, hi, lo = close * k, opn * k, hi * k, lo * k
            close[-1] = float(end_prices[t])          # exact, no float drift
            hi[-1] = max(hi[-1], close[-1])
            lo[-1] = min(lo[-1], close[-1])
        out[t] = pd.DataFrame({"Open": opn, "High": hi, "Low": lo, "Close": close,
                               "Volume": volu}, index=idx)
    return out


def write_csv(frames, path):
    """Long CSV `date,ticker,open,high,low,close,volume` (the fetch_indicators --csv format)."""
    rows = []
    for t, df in frames.items():
        for ts, r in df.iterrows():
            rows.append((ts.strftime("%Y-%m-%d"), t, r["Open"], r["High"], r["Low"],
                         r["Close"], int(r["Volume"])))
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("date,ticker,open,high,low,close,volume\n")
        for d, t, o, h, lo, c, v in rows:
            f.write(f"{d},{t},{o:.6f},{h:.6f},{lo:.6f},{c:.6f},{v}\n")
    return str(path)


def first_run_digest(run_id):
    """digest/v6 as ledger.py --digest writes it for an absent/empty ledger."""
    import config as C
    return {
        "schema": "digest/v6", "run_id": run_id, "first_run": True, "epoch_start": None,
        "runs_since_epoch": 0, "last_run_id": None,
        "scoreboard": {"account_twr_pct": None, "shadow_twr_pct": None, "qqq_pct": None,
                       "value_add_pp": None, "vs_qqq_pp": None, "value_add_12w_pp": None,
                       "value_add_26w_pp": None, "weeks_of_data": 0, "n_marks": 0,
                       "sample_note": "표본 0주 — 실력 판정 불가"},
        "triggers": {"T1": {"active": False, "since": None, "until": None},
                     "T2": {"active": False, "since": None},
                     "T4": {"cash_over_runs": 0, "fire": False},
                     "T5": {"unfilled": [], "fire": False},
                     "NOOP": {"unchanged_weeks": 0, "flag": False},
                     "authority": "FULL", "sleeve_max_pct_effective": C.SLEEVE_MAX_PCT,
                     "sgov_authority": True},
        "pending_orders": [], "holdings_last_mark": None, "shadow_last_mark": None,
        "open_theses": [],
        "sgov": {"held": False, "weight": 0.0, "entered_run_id": None, "runs_held": 0,
                 "expires_after_runs_held": C.SGOV_EXPIRY_RUNS},
        "last_targets": None, "unchanged_weeks": 0,
        "decider_history": {d: [] for d in C.DECIDER_IDS},
        "brier": {d: {"n": 0, "score": None, "display": False} for d in C.DECIDER_IDS},
        "watchdog_triggers": None,
    }


def rs_shaped_drifts(tickers, rs_order=DEFAULT_RS_ORDER):
    """(drift, vol) maps: rs_order names get descending strong drifts, every other
    non-benchmark ticker a weak negative drift."""
    drift, vol = {}, {}
    for t in tickers:
        if t in rs_order:
            i = list(rs_order).index(t)
            drift[t], vol[t] = 0.004 - 0.0008 * i, 0.002
        else:
            drift[t], vol[t] = -0.001, 0.008
    return drift, vol


def make_run(tmp_state, run_id, holdings_text=TODAY_BOOK, prices=None, regime="UP", *,
             as_of=FIXED_AS_OF, earnings=None, digest=None, extra_tickers=(),
             rs_order=DEFAULT_RS_ORDER):
    """Build a complete P0 run dir under `tmp_state/run/<run_id>/`:
    holdings.json (book.parse_holdings), indicators.json (fetch_indicators.compute_all on
    synthetic frames whose last close == prices[T]; QQQ shaped to give `regime`),
    digest.json (`digest` or first-run defaults) and book.json (book --table logic,
    in-process with an explicit run dir). Returns the run dir path (str)."""
    import config as C
    import book
    import fetch_indicators as FI

    if regime not in _REGIME_SHAPE:
        raise ValueError(f"regime must be UP or DOWN, got {regime!r}")
    prices = dict(today_prices() if prices is None else prices)
    rd = os.path.join(str(tmp_state), "run", run_id)
    os.makedirs(rd, exist_ok=True)

    h = book.parse_holdings(holdings_text)
    write_json(os.path.join(rd, "holdings.json"),
               {"schema": "holdings/v6", "run_id": run_id, "source": "args",
                "raw": holdings_text, "positions": h["positions"], "cash_usd": h["cash_usd"]})

    tickers = list(dict.fromkeys(list(C.ALL_TICKERS) + list(h["positions"]) + list(extra_tickers)))
    end_prices = {t: prices.get(t, 100.0) for t in tickers}
    drift, vol = rs_shaped_drifts(tickers, rs_order)
    drift[C.BENCHMARK], vol[C.BENCHMARK] = _REGIME_SHAPE[regime]
    frames = synthetic_frames(tickers, end_prices=end_prices, drift=drift, vol=vol)
    ind = FI.compute_all(frames, as_of_date=as_of, earnings=earnings)
    ind["run_id"] = run_id
    assert ind["regime"]["regime"] == regime, ind["regime"]
    write_json(os.path.join(rd, "indicators.json"), ind)

    write_json(os.path.join(rd, "digest.json"),
               digest if digest is not None else first_run_digest(run_id))
    book.write_table(rd, run_id=run_id)
    return rd
