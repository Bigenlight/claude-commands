#!/usr/bin/env python3
"""us-stock-advisor v5 — the accountability loop. A skill that never grades
itself is how we got here.

Install path: ~/.claude/skills/us-stock-advisor/scripts/score_recs.py
Cron (nightly): 0 14 * * 1-5  python3 ~/.claude/skills/us-stock-advisor/scripts/score_recs.py --score

Reads  state/recommendations.jsonl  (one line per run, INCLUDING no-ops)
Writes state/scorecard.csv
Emits  --track-record  -> the <track_record> block injected into Phase 2
       --header        -> the Korean header numbers printed at the TOP of every report

Two cumulative curves, always:
  1. actual vs "100% QQQ from day 0"          -> is the whole skill worth running?
  2. actual vs the un-overridden mechanical baseline -> the LLM layer's ISOLATED P&L.
Curve 2 is what the pre-committed kill trigger reads.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402

HORIZONS = [1, 5, 20]


def load_recs(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    return rows


def _line_hash(line):
    import hashlib
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


def _read_lines(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [ln for ln in f.read().splitlines() if ln.strip()]


def append_rec(path, rec):
    """Append-only INTEGRITY CHAIN: each record carries prev_hash = sha256 of the
    prior line, so a silent in-place edit or a deleted line breaks the chain and is
    detectable. Phase 2 runs in the top-level thread (Bash+Write), so a suspended
    LLM could otherwise rewrite this ledger to clear its own suspension; on a broken
    chain the gate fails safe to SUSPENDED. Not unforgeable — just tamper-evident."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = _read_lines(path)
    prev = lines[-1] if lines else None
    rec.setdefault("logged_utc", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    rec["prev_hash"] = _line_hash(prev) if prev is not None else None
    with open(path, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def rewrite_recs(path, recs):
    """Rewrite the whole ledger re-linking the chain. Used only by the legitimate
    nightly write-back (which fills matured override spreads); it must not leave a
    broken chain behind."""
    prev = None
    with open(path, "w") as f:
        for r in recs:
            r["prev_hash"] = _line_hash(prev) if prev is not None else None
            line = json.dumps(r, ensure_ascii=False)
            f.write(line + "\n")
            prev = line


def tip_and_len(path):
    """(sha256 of the last line, number of lines). The EXTERNAL anchor persisted in
    last_run.json each run, so a ledger that is deleted, truncated, or re-linked by
    a forger who cannot also rewrite last_run.json is detectable."""
    lines = _read_lines(path)
    return (_line_hash(lines[-1]) if lines else None, len(lines))


def read_anchor(last_run_path):
    """(expected_tip_hash, expected_len) from last_run.json, or (None, None) when
    there is no prior run at all (a genuine first run)."""
    if not last_run_path or not os.path.exists(last_run_path):
        return (None, None)
    try:
        d = json.load(open(last_run_path))
        return d.get("ledger_tip_hash"), d.get("ledger_len")
    except Exception:
        return (None, None)


def verify_chain(path, expected_tip=None, expected_len=None):
    """True iff the ledger is intact. Two independent guards:

    (1) EXTERNAL ANCHOR (fail-safe): once any run has recorded a length>0, the
        ledger may never be empty, shorter than that length, or carry a different
        last-line hash. This kills full-delete, truncate-to-empty, bottom-truncate,
        and a fully re-linked forgery — the round-1 chain-only check missed all four
        because they leave a *self-consistent* file. A forger must now ALSO rewrite
        last_run.json's tip hash (a second file), which is the accepted bar.
    (2) INTERNAL prev_hash chain: catches a naive mid-file edit.

    An empty ledger passes ONLY when there is no anchor claiming content (first run).
    """
    lines = _read_lines(path)
    anchored = expected_len is not None and expected_len > 0
    if anchored:
        if len(lines) < expected_len:
            return False                      # delete / truncate-empty / bottom-truncate
        if not lines or _line_hash(lines[-1]) != expected_tip:
            return False                      # re-linked forgery / any tip change
    if not lines:
        return not anchored                   # empty ok only with no content anchor
    try:
        if json.loads(lines[0]).get("prev_hash") is not None:
            return False                      # top-truncation
    except Exception:
        return False
    for i in range(1, len(lines)):
        try:
            got = json.loads(lines[i]).get("prev_hash")
        except Exception:
            return False
        if got != _line_hash(lines[i - 1]):
            return False                      # naive mid-file edit
    return True


def ledger_intact(path, last_run_path=None):
    """verify_chain wired to the persisted anchor. Unknown/missing anchor with a
    non-empty ledger is still internally checked; empty+no-anchor = first run = ok."""
    if last_run_path is None:
        last_run_path = C.LAST_RUN_JSON
    tip, ln = read_anchor(last_run_path)
    return verify_chain(path, tip, ln)


def sync_anchor(recs_path, last_run_path=None):
    """Re-point last_run.json's anchor at the current ledger tip+len. Called by the
    LEGITIMATE nightly write-back so re-linking the chain does not look like tamper."""
    if last_run_path is None:
        last_run_path = C.LAST_RUN_JSON
    if not os.path.exists(last_run_path):
        return
    try:
        d = json.load(open(last_run_path))
    except Exception:
        return
    tip, ln = tip_and_len(recs_path)
    d["ledger_tip_hash"], d["ledger_len"] = tip, ln
    with open(last_run_path, "w") as f:
        json.dump(d, f, indent=2)


# ------------------------------------------------------------------ pricing
def price_frame(tickers, csv_path=None):
    if csv_path:
        import pandas as pd
        return pd.read_csv(csv_path, parse_dates=["Date"]).set_index("Date").sort_index()
    import yfinance as yf
    d = yf.download(list(dict.fromkeys(tickers)), period="2y", interval="1d",
                    auto_adjust=True, progress=False)
    return d["Close"] if "Close" in d else d


def fwd(df, ticker, date, n):
    if ticker not in df.columns:
        return None
    s = df[ticker].dropna()
    idx = s.index[s.index >= date]
    if len(idx) == 0:
        return None
    i0 = s.index.get_loc(idx[0])
    if i0 + n >= len(s):
        return None
    return float(s.iloc[i0 + n] / s.iloc[i0] - 1.0) * 100.0


# ------------------------------------------------------------------ scoring
def score(recs_path, out_csv, csv_path=None):
    recs = load_recs(recs_path)
    if not recs:
        sys.stderr.write("no recs to score\n")
        return []
    import pandas as pd
    tickers = sorted({r["ticker"] for r in recs if r.get("ticker")} | {C.BENCHMARK})
    df = price_frame(tickers, csv_path)
    out = []
    dirty = False
    for r in recs:
        d = pd.Timestamp(r["date"])
        row = {"date": r["date"], "type": r.get("type"), "ticker": r.get("ticker"),
               "action": r.get("action"), "regime": r.get("regime")}
        for h in HORIZONS:
            rr = fwd(df, r.get("ticker"), d, h) if r.get("ticker") else None
            bb = fwd(df, C.BENCHMARK, d, h)
            row[f"fwd_{h}d"] = None if rr is None else round(rr, 2)
            row[f"qqq_{h}d"] = None if bb is None else round(bb, 2)
            row[f"excess_{h}d"] = (None if rr is None or bb is None else round(rr - bb, 2))
        # the LLM layer's isolated P&L: decision vs the plan it overrode.
        # Computed at maturity and WRITTEN BACK into recommendations.jsonl —
        # without the write-back the suspension counter reads None forever and
        # override_suspended() can never fire.
        if r.get("type") == "override" and r.get("spread_vs_baseline_pp") is None:
            a = fwd(df, r.get("ticker") or C.BENCHMARK, d, 20)
            b = fwd(df, C.BENCHMARK, d, 20)
            if a is not None and b is not None:
                r["spread_vs_baseline_pp"] = round(a - b, 2)
                dirty = True
        row["spread_vs_baseline_pp"] = r.get("spread_vs_baseline_pp")
        out.append(row)
    if dirty:
        rewrite_recs(recs_path, recs)     # re-link the chain, don't break it
        sync_anchor(recs_path)            # ...and re-point the anchor, or the legit
                                          # nightly write-back would read as tamper
    os.makedirs(os.path.dirname(out_csv), exist_ok=True)
    with open(out_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
        w.writeheader()
        w.writerows(out)
    sys.stderr.write(f"scored {len(out)} recs -> {out_csv}\n")
    return out


# ------------------------------------------------- cumulative + track record
def _trading_days_between(s, a, b):
    """Sessions in [a, b] on the benchmark's own index. The kill trigger counts
    forward TRADING days, not calendar days and not runs."""
    import pandas as pd
    return int(((s.index >= pd.Timestamp(a)) & (s.index <= pd.Timestamp(b))).sum())


def cumulative(recs_path, csv_path=None):
    """actual vs 100%-QQQ-from-day-0, and actual vs the mechanical baseline."""
    recs = load_recs(recs_path)
    equity = [r for r in recs if r.get("type") == "portfolio_mark"]
    if not equity:
        return None
    import pandas as pd
    df = price_frame([C.BENCHMARK], csv_path)
    d0, dN = pd.Timestamp(equity[0]["date"]), pd.Timestamp(equity[-1]["date"])
    s = df[C.BENCHMARK].dropna() if hasattr(df, "columns") else df.dropna()
    try:
        q0 = float(s[s.index <= d0].iloc[-1]); qN = float(s[s.index <= dN].iloc[-1])
        qqq_ret = (qN / q0 - 1) * 100
    except Exception:
        qqq_ret = None
    # unit-value (TWR): chain per-mark returns with the deposit stripped from
    # the receiving mark, so a mid-window KRW deposit — a named run trigger —
    # cannot bias the headline the kill trigger reads.
    growth = 1.0
    for a, b in zip(equity, equity[1:]):
        va = float(a["total_usd"])
        vb = float(b["total_usd"]) - float(b.get("deposit_usd", 0) or 0)
        if va > 0:
            growth *= vb / va
    actual = (growth - 1) * 100 if len(equity) > 1 else 0.0
    base = [r.get("baseline_cum_return_pct") for r in equity if r.get("baseline_cum_return_pct") is not None]
    baseline_ret = base[-1] if base else None
    spread = (None if (actual is None or baseline_ret is None) else round(actual - baseline_ret, 2))

    # the kill trigger may only fire on MATURE evidence: KILL_EVAL_TRADING_DAYS of
    # forward, post-cutoff data. Without this it would arm on the second run.
    mature = (len(equity) >= 2
              and _trading_days_between(s, equity[0]["date"], equity[-1]["date"])
              >= C.KILL_EVAL_TRADING_DAYS)
    ovr = [r for r in recs if r.get("type") == "override"
           and r.get("spread_vs_baseline_pp") is not None]
    ovr_hits = sum(1 for r in ovr if float(r["spread_vs_baseline_pp"]) > 0)
    ovr_hitrate = round(ovr_hits / len(ovr), 3) if ovr else None
    tamper = not ledger_intact(recs_path)
    return {
        "since": equity[0]["date"],
        "ledger_tamper_detected": tamper,
        "actual_cum_pct": None if actual is None else round(actual, 2),
        "qqq_cum_pct": None if qqq_ret is None else round(qqq_ret, 2),
        "vs_qqq_pp": None if (actual is None or qqq_ret is None) else round(actual - qqq_ret, 2),
        "mechanical_baseline_cum_pct": baseline_ret,
        "llm_layer_spread_pp": spread,
        "override_hit_rate": ovr_hitrate,
        "override_privileges_at_risk": (ovr_hitrate is not None and len(ovr) >= 3
                                        and ovr_hitrate < C.KILL_MIN_OVERRIDE_HITRATE),
        "eval_trading_days": (None if len(equity) < 2 else
                              _trading_days_between(s, equity[0]["date"], equity[-1]["date"])),
        "eval_mature": mature,
        "kill_trigger_armed": (baseline_ret is not None and actual is not None and mature
                               and spread < C.KILL_SATELLITE_IF_SPREAD_BELOW_PP),
        "board_reconvene_armed": (actual is not None and qqq_ret is not None and mature
                                  and (actual - qqq_ret) < -C.BOARD_RECONVENE_IF_CORE_TRAILS_QQQ_PP),
    }


def track_record(recs_path, out_csv, csv_path=None):
    rows = score(recs_path, out_csv, csv_path) if not os.path.exists(out_csv) else \
        list(csv.DictReader(open(out_csv)))
    rows = rows[-C.TRACK_RECORD_WINDOW:]
    graded = [r for r in rows if r.get("excess_20d") not in (None, "", "None")]
    hits = sum(1 for r in graded if float(r["excess_20d"]) > 0)
    cum = cumulative(recs_path, csv_path) or {}
    mean_excess = (sum(float(r["excess_20d"]) for r in graded) / len(graded)) if graded else None
    hr = (hits / len(graded)) if graded else None
    return {
        "last_n": len(rows),
        "graded": len(graded),
        "hit_rate_vs_qqq": round(hr, 3) if hr is not None else None,
        "mean_excess_20d_pp": None if mean_excess is None else round(mean_excess, 2),
        "dartboard_base_rate": C.DARTBOARD_BASE_RATE,   # 39.8%, NOT 50%
        "beats_dartboard": (hr > C.DARTBOARD_BASE_RATE) if graded else None,
        # expansion is evidence-gated in exactly one place, here:
        "expand_satellite_ok": (bool(cum.get("eval_mature")) and hr is not None
                                and hr > C.EXPAND_SATELLITE_IF_HITRATE_ABOVE
                                and mean_excess is not None and mean_excess > 0),
        "cumulative": cum,
        "recent": rows[-10:],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--recs", default=C.RECS_JSONL)
    ap.add_argument("--out", default=C.SCORECARD_CSV)
    ap.add_argument("--prices-csv")
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--track-record", action="store_true")
    ap.add_argument("--header", action="store_true")
    a = ap.parse_args()
    if a.score:
        score(a.recs, a.out, a.prices_csv)
    if a.track_record:
        print(json.dumps(track_record(a.recs, a.out, a.prices_csv), indent=2, ensure_ascii=False))
    if a.header:
        print(json.dumps(cumulative(a.recs, a.prices_csv), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
