#!/usr/bin/env python3
"""us-stock-advisor v6.0 — the book: holdings, lot table, candidate menu, whole-share solver.

Install path: ~/.claude/skills/us-stock-advisor/scripts/book.py

No LLM token enters any number this file emits. Whole shares (int) everywhere.

CLI
---
  book.py --holdings "QQQ 2, QQQM 1, cash 68.59" --run-id R   -> state/run/R/holdings.json
  book.py --holdings last --run-id R      -> positions + cash from the last ledger `mark` (exit 4 if none)
  book.py --table --run-id R              -> reads holdings.json, indicators.json, digest.json (optional)
                                             -> state/run/R/book.json
  book.py --solve --run-id R              -> reads aggregate_out.json, book.json, digest.json (optional)
                                             -> state/run/R/orders.json
Exit: 0 ok · 2 bad input / missing or invalid prerequisite · 4 `--holdings last` without a ledger mark

Holdings grammar (comma / newline / ';' / '/' separated, case-insensitive):
  `TICKER N`, `N TICKER`, `TICKER N @avg` (avg ignored), optional `주`/`shares` after N,
  `cash X` / `현금 X` / `예수금 X` / `$X` / `X USD`. Non-integer shares -> "whole shares only".

Importable (pinned): parse_holdings, value, lot_table, candidate_menu, solve, apply_orders,
core_only_targets. Plus: caps_in_effect, reconcile, build_table, write_table, solve_run.

Solver (see v6 team plan, "Solver algorithm (pinned)"):
  1. Sleeve/SGOV share targets n = floor(w*total / (px*(1+PRICE_BUFFER_PCT))); n == 0 with w > 0
     -> dropped (LOT_TOO_SMALL), weight returns to core. For a HELD name the held portion is
     rounded unbuffered (n = max(n, min(held, round(w*total/px)))) so the cash buffer never
     trims an incumbent by one share per run. Held names absent from targets -> 0 (EXIT),
     except a held RESIDUAL_FILLER (SPYM) absent from targets, which is a free variable that
     the core search may keep or sell (no filler churn). forced_sells SELL first (priority 1).
  2. Core: enumerate (nQQQ, nQQQM[, filler kept]) minimising buffered cash_final, then fewer
     lines, fewer core lines, more QQQ. A core SELL+BUY combo (conversion) is admissible only
     if no non-conversion combo can fund the non-core buys, or the best non-conversion combo
     leaves cash_final/total > CASH_MAX_PCT (plan rule (a) read as "cannot be funded without
     a conversion"; the literal reading would turn today's SELL QQQM 1 / BUY NVDA 1 into a
     SELL QQQ 2 / BUY QQQM 4 swap).
  3. Filler: residual > CASH_MAX_PCT and no QQQM lot fits -> BUY SPYM (sleeve "FILLER").
  4. Line cap: > max_lines -> drop FILLER, then the smallest sleeve/SGOV BUY, then core BUYs
     (re-solving each time, so a SELL that only funded a dropped BUY disappears with it);
     SELLs and forced sells are never dropped.
  5. post_trade via apply_orders at limit_ref_price (no buffer, no commission).
Warnings are dicts {"code", "detail"}; codes: LOT_TOO_SMALL, NO_PRICE, LINE_CAP,
LINE_CAP_UNRESOLVED, INFEASIBLE, T2_SUSPENDED, DECLARE_FLOW, UNEXPECTED_POSITION_CHANGE.
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402

EPS = 1e-9
CORE = "CORE"


# ================================================================= io helpers
def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write_json(path, obj):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, f".{os.path.basename(path)}.tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return path


def _warn(code, detail):
    return {"code": code, "detail": detail}


# ================================================================= holdings parsing
_NUM = r"[-+]?\d+(?:\.\d+)?"
_TICK = r"[A-Za-z][A-Za-z0-9.\-]{0,9}"
_SHARE_SFX = r"(?:\s*(?:주|shares?|sh))?"
_AVG = r"(?:\s*@\s*\$?\s*\d+(?:\.\d+)?)?"
_CASH_WORDS = r"(?:cash|현금|예수금)"
_RE_CASH = [
    re.compile(rf"^{_CASH_WORDS}\s*[:=]?\s*\$?\s*({_NUM})\s*(?:usd|달러)?$", re.I),
    re.compile(rf"^\$\s*({_NUM})(?:\s*{_CASH_WORDS})?$", re.I),
    re.compile(rf"^\$?\s*({_NUM})\s*(?:usd|달러|{_CASH_WORDS})$", re.I),
]
_RE_POS = [
    re.compile(rf"^({_TICK})\s*[:=]?\s*({_NUM}){_SHARE_SFX}{_AVG}$", re.I),
    re.compile(rf"^({_NUM}){_SHARE_SFX}\s+({_TICK}){_AVG}$", re.I),
]
_RESERVED = {"CASH", "USD"}


def _shares(tok, raw):
    v = float(tok)
    if not v.is_integer():
        raise ValueError(f"whole shares only: {raw!r}")
    if v < 0:
        raise ValueError(f"negative shares: {raw!r}")
    return int(v)


def parse_holdings(text: str) -> dict:
    """'QQQ 2, QQQM 1, cash 68.59' -> {"positions": {"QQQ": 2, "QQQM": 1}, "cash_usd": 68.59}.
    Raises ValueError on fractional/negative shares, negative cash, unknown tokens,
    duplicates or empty input."""
    if text is None or not str(text).strip():
        raise ValueError("empty holdings")
    positions, cash = {}, None
    for piece in re.split(r"[,\n;/]+", str(text)):
        s = re.sub(r"\s+", " ", piece.strip())
        if not s:
            continue
        m = next((r.match(s) for r in _RE_CASH if r.match(s)), None)
        if m:
            v = float(m.group(1))
            if v < 0:
                raise ValueError(f"negative cash: {s!r}")
            if cash is not None:
                raise ValueError(f"cash given twice: {s!r}")
            cash = round(v, 2)
            continue
        m = _RE_POS[0].match(s)
        if m:
            t, n = m.group(1), m.group(2)
        else:
            m = _RE_POS[1].match(s)
            if not m:
                raise ValueError(f"unrecognized holdings token: {s!r}")
            n, t = m.group(1), m.group(2)
        t = t.upper()
        if t in _RESERVED:
            raise ValueError(f"unrecognized holdings token: {s!r}")
        k = _shares(n, s)
        if t in positions:
            raise ValueError(f"ticker given twice: {t}")
        positions[t] = k
    return {"positions": {t: n for t, n in positions.items() if n > 0},
            "cash_usd": 0.0 if cash is None else cash}


def holdings_from_ledger(path=None):
    """{positions, cash_usd, date} of the last `mark` line's account (own JSONL reader; no
    ledger.py import). None if the ledger is absent or has no mark."""
    path = path or C.LEDGER_JSONL
    if not os.path.isfile(path):
        return None
    last = None
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and rec.get("type") == "mark" and isinstance(rec.get("account"), dict):
                last = rec
    if last is None:
        return None
    acc = last["account"]
    return {"positions": {str(t).upper(): int(n) for t, n in (acc.get("positions") or {}).items() if int(n) > 0},
            "cash_usd": round(float(acc.get("cash_usd") or 0.0), 2), "date": last.get("date")}


# ================================================================= valuation
def _price(prices, t):
    try:
        v = float((prices or {}).get(t))
    except (TypeError, ValueError):
        return None
    return v if v > 0 and v == v else None


def _px_map(prices):
    return {str(t).upper(): round(_price(prices, t), 2) for t in (prices or {}) if _price(prices, t)}


def value(positions: dict, cash_usd: float, prices: dict) -> dict:
    """Mark the book. weights are fractions per ticker + "CASH"; *_pct are percent.
    Positions without a price are listed in `unpriced` and excluded from the total."""
    cash = float(cash_usd or 0.0)
    usd, unpriced = {}, []
    for t, n in (positions or {}).items():
        n = int(n)
        if n == 0:
            continue
        px = _price(prices, t)
        if px is None:
            unpriced.append(t)
            continue
        usd[t] = n * px
    total = cash + sum(usd.values())
    core = sum(v for t, v in usd.items() if t in C.CORE_TICKERS)
    sgov = usd.get(C.DERISK_TICKER, 0.0)
    sleeve = total - cash - core - sgov

    def frac(x):
        return round(x / total, 6) if total > 0 else 0.0

    def pct(x):
        return round(x / total * 100.0, 4) if total > 0 else 0.0

    weights = {t: frac(v) for t, v in usd.items()}
    weights["CASH"] = frac(cash)
    return {"total_usd": round(total, 2), "cash_usd": round(cash, 2), "cash_pct": pct(cash),
            "weights": weights,
            "core_usd": round(core, 2), "core_pct": pct(core),
            "sleeve_usd": round(sleeve, 2), "sleeve_pct": pct(sleeve),
            "sgov_usd": round(sgov, 2), "sgov_pct": pct(sgov),
            "unpriced": unpriced}


def lot_table(total_usd, prices, position_max_pct, *, cash_usd=0.0, tickers=None) -> dict:
    """Per ticker (default ALL_TICKERS): one buffered whole-share lot vs the per-position cap.
    feasible iff price*(1+PRICE_BUFFER_PCT) <= position_max_pct*total_usd; core/derisk always
    feasible (when priced). Unpriced -> price None, feasible False."""
    total = float(total_usd or 0.0)
    cash = max(float(cash_usd or 0.0), 0.0)
    cap = float(position_max_pct or 0.0) * total
    out = {}
    for t in (tickers or C.ALL_TICKERS):
        g = C.group_of(t)
        px = _price(prices, t)
        if px is None:
            out[t] = {"price": None, "lot_usd_buffered": None, "lot_pct": None, "feasible": False,
                      "max_shares_in_cash": 0, "group": g}
            continue
        lot = px * (1 + C.PRICE_BUFFER_PCT)
        feas = True if g in ("core", "derisk") else (lot <= cap + EPS)
        out[t] = {"price": round(px, 2), "lot_usd_buffered": round(lot, 2),
                  "lot_pct": round(lot / total * 100.0, 2) if total > 0 else None,
                  "feasible": bool(feas), "max_shares_in_cash": int(math.floor(cash / lot + EPS)),
                  "group": g}
    return out


def _default_held_sleeve(positions):
    return [t for t, n in (positions or {}).items()
            if int(n) > 0 and t not in C.CORE_TICKERS and t != C.DERISK_TICKER
            and t != C.RESIDUAL_FILLER]


def candidate_menu_with_reasons(lot, positions, indicators, max_n=C.CANDIDATE_MENU_MAX, *,
                                held_sleeve=None):
    """(menu, reasons). Order: held sleeve names (always, even if infeasible now) -> feasible
    sleeve-eligible names with earnings_in_sessions <= EARNINGS_MENU_SESSIONS -> feasible
    names by rs_rank ascending. Deduped, cut at max_n."""
    tick = (indicators or {}).get("tickers", indicators) or {}
    held = list(held_sleeve) if held_sleeve is not None else _default_held_sleeve(positions)
    menu, reasons = [], {}

    def add(t, why):
        if t not in reasons and len(menu) < max_n:
            menu.append(t)
            reasons[t] = why

    for t in held:
        add(t, "held")
    feas = [t for t in C.SLEEVE_ELIGIBLE if (lot.get(t) or {}).get("feasible")]

    def rank(t):
        r = (tick.get(t) or {}).get("rs_rank")
        return r if isinstance(r, int) else 10 ** 6

    earn = []
    for t in feas:
        e = (tick.get(t) or {}).get("earnings_in_sessions")
        if isinstance(e, int) and 0 <= e <= C.EARNINGS_MENU_SESSIONS:
            earn.append((e, rank(t), t))
    for e, _, t in sorted(earn):
        add(t, f"earnings_in_{e}")
    for t in sorted((t for t in feas if rank(t) < 10 ** 6), key=lambda t: (rank(t), t)):
        add(t, f"rs_rank_{rank(t)}")
    return menu, reasons


def candidate_menu(lot, positions, indicators, max_n=C.CANDIDATE_MENU_MAX, *, held_sleeve=None) -> list:
    return candidate_menu_with_reasons(lot, positions, indicators, max_n, held_sleeve=held_sleeve)[0]


def caps_in_effect(authority="FULL", regime=C.REGIME_UP) -> dict:
    """Caps after damage-cap triggers. T1_REDUCED: sleeve total -> T1_SLEEVE_CUT;
    T2_SUSPENDED: sleeve 0, SGOV authority off. Position cap never exceeds the sleeve cap."""
    authority = authority or "FULL"
    sgov = C.SGOV_MAX_DOWN if regime == C.REGIME_DOWN else C.SGOV_MAX_UP
    sleeve, sgov_auth = C.SLEEVE_MAX_PCT, True
    if authority == "T1_REDUCED":
        sleeve = C.T1_SLEEVE_CUT
    elif authority == "T2_SUSPENDED":
        sleeve, sgov, sgov_auth = 0.0, 0.0, False
    elif authority != "FULL":
        raise ValueError(f"unknown authority {authority!r}")
    return {"authority": authority, "sleeve_max_pct": sleeve,
            "sleeve_position_max_pct": min(C.SLEEVE_POSITION_MAX_PCT, sleeve),
            "sleeve_max_positions": C.SLEEVE_MAX_POSITIONS if sleeve > 0 else 0,
            "sgov_max_pct": sgov, "sgov_authority": sgov_auth}


# ================================================================= fill reconciliation
def _empty_recon():
    return {"pending_orders": [], "filled": [], "unfilled": [], "unexpected": [],
            "implied_cash_delta_usd": 0.0, "warnings": []}


def reconcile(positions, cash_usd, digest) -> dict:
    """Pending orders (digest.pending_orders) vs the share delta since the last mark
    (digest.holdings_last_mark). FILLED iff the delta has the order's sign and |delta| >=
    shares (each fill consumes its shares from the delta). Unexplained deltas -> unexpected.
    implied_cash_delta_usd = cash_reported - (last_mark.cash + filled cash effects)."""
    digest = digest or {}
    last = digest.get("holdings_last_mark")
    if not isinstance(last, dict) or last.get("positions") is None:
        return _empty_recon()
    pending = [dict(o) for o in (digest.get("pending_orders") or [])]
    prev = {str(t).upper(): int(n) for t, n in (last.get("positions") or {}).items()}
    now = {str(t).upper(): int(n) for t, n in (positions or {}).items()}
    delta = {t: now.get(t, 0) - prev.get(t, 0) for t in set(prev) | set(now)}
    remaining = dict(delta)
    explained = {t: 0 for t in delta}
    filled, unfilled, cash_fx = [], [], 0.0
    for o in pending:
        t = str(o.get("ticker", "")).upper()
        n = int(o.get("shares") or 0)
        sign = 1 if str(o.get("action", "")).upper() == "BUY" else -1
        r = remaining.get(t, 0)
        if n > 0 and r * sign >= n:
            remaining[t] = r - sign * n
            explained[t] = explained.get(t, 0) + sign * n
            filled.append(dict(o, status="FILLED"))
            cash_fx += -sign * n * float(o.get("limit_ref_price") or 0.0)
        else:
            unfilled.append(dict(o, status="UNFILLED"))
    unexpected = [{"ticker": t, "delta_shares": delta[t], "explained_shares": explained.get(t, 0),
                   "unexplained_shares": remaining[t]}
                  for t in sorted(remaining) if remaining[t] != 0]
    implied = round(float(cash_usd or 0.0) - (float(last.get("cash_usd") or 0.0) + cash_fx), 2)
    warns = []
    if abs(implied) > C.FLOW_WARN_USD:
        warns.append(_warn("DECLARE_FLOW", f"implied cash delta {implied:+.2f} USD vs last mark "
                                           f"{last.get('date')}; declare --deposit/--withdraw if real"))
    if unexpected:
        warns.append(_warn("UNEXPECTED_POSITION_CHANGE",
                           ", ".join(f"{u['ticker']} {u['unexplained_shares']:+d}" for u in unexpected)))
    return {"pending_orders": pending, "filled": filled, "unfilled": unfilled,
            "unexpected": unexpected, "implied_cash_delta_usd": implied, "warnings": warns}


# ================================================================= book table
def build_table(run_dir_path, run_id=None) -> dict:
    """book/v6 dict from run_dir/{holdings.json, indicators.json, digest.json (optional)}."""
    hp = os.path.join(run_dir_path, "holdings.json")
    ip = os.path.join(run_dir_path, "indicators.json")
    dp = os.path.join(run_dir_path, "digest.json")
    for p in (hp, ip):
        if not os.path.isfile(p):
            raise FileNotFoundError(f"missing {p}")
    hold = _read_json(hp)
    ind = _read_json(ip)
    dig = _read_json(dp) if os.path.isfile(dp) else {}
    run_id = run_id or hold.get("run_id") or os.path.basename(os.path.normpath(run_dir_path))

    positions = {str(t).upper(): int(n) for t, n in (hold.get("positions") or {}).items() if int(n) > 0}
    cash = float(hold.get("cash_usd") or 0.0)
    tick = ind.get("tickers") or {}
    prices = {t: v["price"] for t, v in tick.items() if isinstance(v, dict) and v.get("price")}
    regime = ((ind.get("regime") or {}).get("regime")) or C.REGIME_UP
    authority = ((dig.get("triggers") or {}).get("authority")) or "FULL"
    caps = caps_in_effect(authority, regime)
    val = value(positions, cash, prices)
    total = val["total_usd"]

    thesis_tickers = {str(th.get("ticker", "")).upper() for th in (dig.get("open_theses") or [])}
    held_sleeve = [t for t in positions if t not in C.CORE_TICKERS and t != C.DERISK_TICKER
                   and (t != C.RESIDUAL_FILLER or t in thesis_tickers)]

    def tag(t):
        if t in C.CORE_TICKERS:
            return "CORE"
        if t == C.DERISK_TICKER:
            return "SGOV"
        return "SLEEVE" if t in held_sleeve else "FILLER"

    pos_out = {}
    for t, n in positions.items():
        px = _price(prices, t)
        usd = n * px if px else None
        pos_out[t] = {"shares": n, "price": round(px, 2) if px else None,
                      "usd": round(usd, 2) if usd is not None else None,
                      "pct": round(usd / total * 100.0, 2) if (usd is not None and total > 0) else None,
                      "sleeve": tag(t)}

    lot_tickers = list(dict.fromkeys(list(C.ALL_TICKERS) + list(positions)))
    lot = lot_table(total, prices, caps["sleeve_position_max_pct"], cash_usd=cash, tickers=lot_tickers)
    menu, reasons = candidate_menu_with_reasons(lot, positions, ind, held_sleeve=held_sleeve)
    hidden = [t for t in C.SLEEVE_ELIGIBLE if not lot[t]["feasible"] and t not in held_sleeve]
    return {
        "schema": "book/v6", "run_id": run_id, "as_of_close_date": ind.get("as_of_close_date"),
        "regime": regime,
        "total_usd": total, "cash_usd": round(cash, 2), "cash_pct": round(val["cash_pct"], 2),
        "positions": pos_out,
        "core_usd": val["core_usd"], "core_pct": round(val["core_pct"], 2),
        "sleeve_usd": val["sleeve_usd"], "sleeve_pct": round(val["sleeve_pct"], 2),
        "sgov_usd": val["sgov_usd"], "sgov_pct": round(val["sgov_pct"], 2),
        "held_sleeve": held_sleeve, "unpriced": val["unpriced"],
        "caps_in_effect": caps,
        "prices": {t: round(float(p), 2) for t, p in prices.items()},
        "lot_table": lot, "candidate_menu": menu, "candidate_reasons": reasons,
        "hidden_infeasible": hidden,
        "reconciliation": reconcile(positions, cash, dig),
    }


def write_table(run_dir_path, run_id=None) -> dict:
    b = build_table(run_dir_path, run_id=run_id)
    _write_json(os.path.join(run_dir_path, "book.json"), b)
    return b


# ================================================================= solver
def core_only_targets() -> dict:
    return {CORE: 1.0}


def apply_orders(positions, cash_usd, orders, commission_pct=0.0):
    """Apply orders at limit_ref_price. BUY costs px*(1+commission), SELL yields
    px*(1-commission). Returns (positions without zero lines, cash)."""
    pos = {str(t).upper(): int(n) for t, n in (positions or {}).items()}
    cash = float(cash_usd or 0.0)
    c = float(commission_pct or 0.0)
    for o in orders or []:
        t = str(o["ticker"]).upper()
        n = int(o["shares"])
        px = float(o["limit_ref_price"])
        if n < 0:
            raise ValueError(f"negative shares in order {o}")
        if str(o["action"]).upper() == "BUY":
            pos[t] = pos.get(t, 0) + n
            cash -= n * px * (1 + c)
        elif str(o["action"]).upper() == "SELL":
            if pos.get(t, 0) < n:
                raise ValueError(f"cannot SELL {n} {t}: holding {pos.get(t, 0)}")
            pos[t] = pos[t] - n
            cash += n * px * (1 - c)
        else:
            raise ValueError(f"bad action in order {o}")
    return {t: n for t, n in pos.items() if n != 0}, round(cash, 6)


def _order(t, action, n, px, sleeve, reason, priority):
    return {"ticker": t, "action": action, "shares": int(n), "limit_ref_price": round(px, 2),
            "usd": round(n * px, 2), "sleeve": sleeve, "reason": reason, "priority": priority}


def _solve_once(targets, pos, cash0, px, forced, *, allow_conversion, filler, excluded_buys,
                core_buy_ok, reasons):
    buf = C.PRICE_BUFFER_PCT
    total = value(pos, cash0, px)["total_usd"]
    warns, dropped = [], []
    fill_t = C.RESIDUAL_FILLER
    free_filler = (fill_t in pos and fill_t not in targets and fill_t not in forced and fill_t in px)

    # ---- 1. non-core share targets
    tgt_n, tgt_w = {}, {}
    for t, w in targets.items():
        if t == CORE or t in C.CORE_TICKERS:
            continue
        held = pos.get(t, 0)
        if t not in px:
            if w > 0 or held:
                warns.append(_warn("NO_PRICE", f"{t}: no price; target ignored, holding kept"))
            continue
        w = max(0.0, float(w))
        n = int(math.floor(w * total / (px[t] * (1 + buf)) + EPS)) if total > 0 else 0
        if held > 0:
            n = max(n, min(held, int(math.floor(w * total / px[t] + 0.5))))
        if w > 0 and n == 0:
            dropped.append({"ticker": t, "reason": "LOT_TOO_SMALL"})
            warns.append(_warn("LOT_TOO_SMALL", f"{t}: {w:.3f} x {total:.2f} < one buffered lot "
                                                f"{px[t] * (1 + buf):.2f}; weight returns to core"))
        if t in excluded_buys:
            n = min(n, held)
        tgt_n[t], tgt_w[t] = n, w
    for t in pos:
        if t in C.CORE_TICKERS or t in tgt_n or (t == fill_t and free_filler):
            continue
        if t not in px:
            warns.append(_warn("NO_PRICE", f"{t}: held but unpriced; left untouched"))
            continue
        tgt_n[t] = 0
    for t, k in forced.items():
        if t not in px:
            warns.append(_warn("NO_PRICE", f"{t}: forced sell skipped (no price)"))
            continue
        tgt_n[t] = min(tgt_n.get(t, pos.get(t, 0)), pos.get(t, 0) - k)

    nc_delta = {t: n - pos.get(t, 0) for t, n in tgt_n.items() if n != pos.get(t, 0)}
    sell_nc = sum(-d * px[t] for t, d in nc_delta.items() if d < 0)
    buy_nc = sum(d * px[t] * (1 + buf) for t, d in nc_delta.items() if d > 0)
    base = cash0 + sell_nc - buy_nc
    need_core_funding = buy_nc > cash0 + sell_nc + EPS

    # ---- 2. core enumeration
    core_t = [t for t in C.CORE_TICKERS if t in px]
    hc = {t: pos.get(t, 0) for t in core_t}
    hf = pos.get(fill_t, 0) if free_filler else 0
    liquid = base + sum(hc[t] * px[t] for t in core_t) + (hf * px[fill_t] if hf else 0.0)
    ranges = []
    for t in core_t:
        up = hc[t] + (max(0, int(math.floor(max(liquid, 0.0) / (px[t] * (1 + buf)) + EPS)))
                      if core_buy_ok else 0)
        ranges.append(range(0, up + 1))
    ranges.append(range(0, hf + 1))           # filler shares KEPT (free variable)
    best = {False: None, True: None}
    for combo in itertools.product(*ranges):
        ns, keep_f = combo[:-1], combo[-1]
        cash_f = base + (hf - keep_f) * (px[fill_t] if hf else 0.0)
        core_sell = core_buy = False
        core_lines = 0
        for t, n in zip(core_t, ns):
            d = n - hc[t]
            if d < 0:
                cash_f += -d * px[t]
                core_sell = True
                core_lines += 1
            elif d > 0:
                cash_f -= d * px[t] * (1 + buf)
                core_buy = True
                core_lines += 1
        if cash_f < -1e-6:
            continue
        conv = core_sell and core_buy
        lines = len(nc_delta) + core_lines + (1 if keep_f < hf else 0)
        n_first = ns[0] if ns else 0
        key = (round(cash_f, 2), lines, core_lines, -n_first)
        if best[conv] is None or key < best[conv][0]:
            best[conv] = (key, ns, keep_f, cash_f)
    nonconv, conv = best[False], best[True]
    # Conversion admissibility. Plan rule (a) "non-core buys exceed cash0 + non-core sells" is
    # read as "the deviation cannot be funded WITHOUT a conversion": when a plain core SELL
    # already funds it (today: SELL QQQM 1 -> BUY NVDA 1, residual 7.5%), a bigger
    # SELL QQQ + BUY QQQM swap is churn and is not admissible unless rule (b) holds.
    conv_ok = allow_conversion and conv is not None and (
        nonconv is None or (total > 0 and nonconv[3] / total > C.CASH_MAX_PCT + EPS))
    cands = [c for c in (nonconv, conv if conv_ok else None) if c is not None]
    if not cands:
        return None
    chosen = min(cands, key=lambda c: c[0])
    conversion_used = chosen is conv
    _, ns, keep_f, cash_f = chosen

    # ---- orders
    orders = []
    buy_names = [t for t, d in nc_delta.items() if d > 0]
    for t, d in nc_delta.items():
        sleeve = "SGOV" if t == C.DERISK_TICKER else "SLEEVE"
        if d < 0:
            pr = 1 if t in forced else 2
            why = reasons.get(t) or ("KILL fired: forced sell" if t in forced else
                                     ("exit to core" if tgt_n[t] == 0 else "reduce to target"))
            orders.append(_order(t, "SELL", -d, px[t], sleeve, why, pr))
        else:
            pr = 3 if t == C.DERISK_TICKER else 4
            why = reasons.get(t) or f"target {tgt_w.get(t, 0.0):.3f}"
            orders.append(_order(t, "BUY", d, px[t], sleeve, why, pr))
    if keep_f < hf:
        orders.append(_order(fill_t, "SELL", hf - keep_f, px[fill_t], "FILLER",
                             "release filler to fund core", 2))
    for t, n in zip(core_t, ns):
        d = n - hc[t]
        if d < 0:
            if need_core_funding:
                why = "fund sleeve entry " + ", ".join(buy_names)
            elif conversion_used:
                why = "conversion: cut residual cash below CASH_MAX_PCT"
            else:
                why = "core reduce"
            orders.append(_order(t, "SELL", -d, px[t], "CORE", why, 2))
        elif d > 0:
            why = ("conversion: cut residual cash below CASH_MAX_PCT" if conversion_used
                   else "deploy residual cash to core")
            orders.append(_order(t, "BUY", d, px[t], "CORE", why, 5))

    # ---- 3. filler
    qqqm = C.CORE_TICKERS[-1]
    qqqm_fits = qqqm in px and core_buy_ok and px[qqqm] * (1 + buf) <= cash_f + EPS
    if (filler and total > 0 and cash_f / total > C.CASH_MAX_PCT + EPS and not qqqm_fits
            and fill_t in px and not any(o["ticker"] == fill_t and o["action"] == "SELL" for o in orders)):
        n = int(math.floor(cash_f / (px[fill_t] * (1 + buf)) + EPS))
        if n > 0:
            why = (f"residual filler: cash {cash_f / total * 100:.1f}% > "
                   f"{C.CASH_MAX_PCT * 100:.0f}% and no {qqqm} lot fits")
            ex = next((o for o in orders if o["ticker"] == fill_t and o["action"] == "BUY"), None)
            if ex:
                ex["shares"] += n
                ex["usd"] = round(ex["shares"] * px[fill_t], 2)
            else:
                orders.append(_order(fill_t, "BUY", n, px[fill_t], "FILLER", why, 6))
            cash_f -= n * px[fill_t] * (1 + buf)

    rank = {t: i for i, t in enumerate(C.ALL_TICKERS)}
    orders.sort(key=lambda o: (o["priority"], rank.get(o["ticker"], 999), o["ticker"]))
    return {"orders": orders, "conversion_used": bool(conversion_used), "dropped": dropped,
            "warnings": warns, "total": total, "target_shares": tgt_n}


def solve(targets: dict, positions: dict, cash_usd: float, prices: dict, *, allow_conversion=True,
          forced_sells=None, max_lines=C.MAX_ORDER_LINES, filler=True, reasons=None) -> dict:
    """Whole-share orders for `targets` ({"CORE": w, "SGOV": w, T: w}). Pure, deterministic.
    Returns the orders/v6 body (no schema/run_id)."""
    tg = {}
    for k, v in (targets or {}).items():
        k = str(k).upper()
        try:
            tg[k] = float(v or 0.0)
        except (TypeError, ValueError):
            tg[k] = 0.0
    pos = {}
    for t, n in (positions or {}).items():
        if int(n) != n:
            raise ValueError(f"whole shares only: {t} {n}")
        if int(n) < 0:
            raise ValueError(f"negative position {t} {n}")
        if int(n) > 0:
            pos[str(t).upper()] = int(n)
    px = _px_map(prices)
    cash0 = float(cash_usd or 0.0)
    forced = {}
    for t, k in (forced_sells or {}).items():
        t = str(t).upper()
        k = min(int(k), pos.get(t, 0))
        if k > 0:
            forced[t] = k
    reasons = {str(k).upper(): v for k, v in (reasons or {}).items()}

    flags = {"filler": bool(filler), "core_buy_ok": True}
    excluded, extra_drops, extra_warns = set(), [], []
    plan = None
    for _ in range(len(tg) + len(pos) + 6):
        plan = _solve_once(tg, pos, cash0, px, forced, allow_conversion=allow_conversion,
                           filler=flags["filler"], excluded_buys=excluded,
                           core_buy_ok=flags["core_buy_ok"], reasons=reasons)
        if plan is None:
            # infeasible: drop the smallest non-core buy target and retry
            cand = [t for t, w in tg.items() if t != CORE and t not in C.CORE_TICKERS
                    and w > 0 and t not in excluded and t in px]
            if not cand:
                raise ValueError("solver: no feasible order set")
            t = min(cand, key=lambda x: (tg[x], x))
            excluded.add(t)
            extra_drops.append({"ticker": t, "reason": "INFEASIBLE"})
            extra_warns.append(_warn("INFEASIBLE", f"{t}: buy not fundable; kept at holding"))
            continue
        if len(plan["orders"]) <= max_lines:
            break
        ords = plan["orders"]
        if any(o["sleeve"] == "FILLER" and o["action"] == "BUY" for o in ords) and flags["filler"]:
            flags["filler"] = False
            extra_warns.append(_warn("LINE_CAP", f"{len(ords)} lines > {max_lines}: FILLER dropped"))
            continue
        sb = [o for o in ords if o["action"] == "BUY" and o["sleeve"] in ("SLEEVE", "SGOV")]
        if sb:
            o = min(sb, key=lambda o: (o["usd"], o["ticker"]))
            excluded.add(o["ticker"])
            extra_drops.append({"ticker": o["ticker"], "reason": "LINE_CAP"})
            extra_warns.append(_warn("LINE_CAP", f"{len(ords)} lines > {max_lines}: "
                                                 f"BUY {o['ticker']} dropped"))
            continue
        if any(o["action"] == "BUY" and o["sleeve"] == "CORE" for o in ords) and flags["core_buy_ok"]:
            flags["core_buy_ok"] = False
            extra_warns.append(_warn("LINE_CAP", f"{len(ords)} lines > {max_lines}: core BUY dropped"))
            continue
        extra_warns.append(_warn("LINE_CAP_UNRESOLVED", f"{len(ords)} lines (sells only) > {max_lines}"))
        break

    orders = plan["orders"]
    post_pos, post_cash = apply_orders(pos, cash0, orders)
    pv = value(post_pos, post_cash, px)
    total = pv["total_usd"]
    w = {CORE: 0.0}
    for t, v in pv["weights"].items():
        if t in C.CORE_TICKERS:
            w[CORE] += v
        elif t != "CASH":
            w[t] = v
    w.setdefault(C.DERISK_TICKER, 0.0)
    w["CASH"] = pv["weights"].get("CASH", 0.0)
    w = {k: round(v, 4) for k, v in w.items()}
    cash_pct = round(pv["cash_pct"], 2)
    buf = C.PRICE_BUFFER_PCT
    lot_fits = any(t in px and px[t] * (1 + buf) <= post_cash + EPS
                   for t in (C.CORE_TICKERS[-1], C.RESIDUAL_FILLER))
    under = pv["cash_pct"] <= C.CASH_MAX_PCT * 100 + EPS
    unavoidable = under or not lot_fits
    reason = ("<=CASH_MAX_PCT" if under else
              "no QQQM/SPYM lot improves it below CASH_MAX_PCT" if not lot_fits else
              "avoidable: lot fits but was not bought (line cap / flags)")
    dropped = list(plan["dropped"])
    for d in extra_drops:
        if d not in dropped:
            dropped.append(d)
    return {
        "orders": orders,
        "post_trade": {"positions": post_pos, "cash_usd": round(post_cash, 2), "cash_pct": cash_pct,
                       "total_usd": total, "weights": w},
        "residual": {"cash_usd": round(post_cash, 2), "cash_pct": cash_pct,
                     "unavoidable": bool(unavoidable), "reason": reason},
        "conversion_used": plan["conversion_used"], "lines": len(orders),
        "dropped_targets": dropped, "warnings": plan["warnings"] + extra_warns,
        "total_usd": plan["total"], "target_shares": plan["target_shares"],
    }


def _reasons_from_aggregate(agg):
    out = {}
    for t, s in (agg.get("sleeve") or {}).items():
        codes = s.get("reason_codes") or []
        act = s.get("action") or ""
        votes = s.get("votes")
        parts = [act] + ([codes[0]] if codes else []) + ([f"{votes}/3"] if votes is not None else [])
        out[str(t).upper()] = " ".join(p for p in parts if p).strip()
    for e in agg.get("exits") or []:
        t = str(e.get("ticker", "")).upper()
        if t:
            out[t] = f"EXIT {e.get('why', '')}".strip()
    sg = agg.get("sgov") or {}
    if sg.get("weight") or sg.get("rule") in ("INCUMBENT",):
        codes = sg.get("reason_codes") or []
        out[C.DERISK_TICKER] = " ".join(x for x in ["DERISK", codes[0] if codes else sg.get("rule", ""),
                                                    f"{sg.get('votes', 0)}/3"] if x)
    return out


def solve_run(run_dir_path, run_id=None) -> dict:
    """orders/v6 dict from run_dir/{aggregate_out.json, book.json, digest.json (optional)}.
    Raises FileNotFoundError / ValueError on a missing or invalid prerequisite."""
    ap = os.path.join(run_dir_path, "aggregate_out.json")
    bp = os.path.join(run_dir_path, "book.json")
    dp = os.path.join(run_dir_path, "digest.json")
    if not os.path.isfile(ap):
        raise FileNotFoundError(f"missing {ap}")
    if not os.path.isfile(bp):
        raise FileNotFoundError(f"missing {bp}")
    agg = _read_json(ap)
    if agg.get("valid") is not True:
        raise ValueError("aggregate_out.json valid != true; refusing to solve")
    bk = _read_json(bp)
    dig = _read_json(dp) if os.path.isfile(dp) else {}
    run_id = run_id or agg.get("run_id") or bk.get("run_id")
    targets = dict(agg.get("targets") or core_only_targets())
    warns = []
    authority = ((dig.get("triggers") or {}).get("authority")) or (bk.get("caps_in_effect") or {}).get("authority")
    if authority == "T2_SUSPENDED":
        nz = [t for t, w in targets.items() if t != CORE and (w or 0) > 0]
        if nz:
            warns.append(_warn("T2_SUSPENDED", f"non-core targets {nz} zeroed (T2: paper only)"))
            targets = {t: (w if t == CORE else 0.0) for t, w in targets.items()}
    positions = {t: int(p["shares"]) for t, p in (bk.get("positions") or {}).items()}
    prices = dict(bk.get("prices") or {})
    for t, row in (bk.get("lot_table") or {}).items():
        if row.get("price") and t not in prices:
            prices[t] = row["price"]
    res = solve(targets, positions, bk.get("cash_usd") or 0.0, prices,
                forced_sells=agg.get("forced_sells") or {}, reasons=_reasons_from_aggregate(agg))
    res["warnings"] = warns + res["warnings"]
    return {"schema": "orders/v6", "run_id": run_id, **res}


# ================================================================= CLI
def _parse_args(argv):
    p = argparse.ArgumentParser(prog="book.py", description=__doc__.split("\n")[0])
    m = p.add_mutually_exclusive_group(required=True)
    m.add_argument("--holdings", metavar="TEXT|last", help='"QQQ 2, QQQM 1, cash 68.59" or "last"')
    m.add_argument("--table", action="store_true", help="write run/R/book.json")
    m.add_argument("--solve", action="store_true", help="write run/R/orders.json")
    p.add_argument("--run-id", required=True)
    return p.parse_args(argv)


def main(argv=None):
    a = _parse_args(sys.argv[1:] if argv is None else argv)
    rd = C.run_dir(a.run_id)
    if a.holdings is not None:
        if a.holdings.strip().lower() == "last":
            h = holdings_from_ledger()
            if h is None:
                sys.stderr.write(f"ERROR: no ledger mark in {C.LEDGER_JSONL}; give holdings explicitly\n")
                return 4
            doc = {"schema": "holdings/v6", "run_id": a.run_id, "source": "ledger", "raw": "last",
                   "positions": h["positions"], "cash_usd": h["cash_usd"], "ledger_mark_date": h["date"]}
        else:
            try:
                h = parse_holdings(a.holdings)
            except ValueError as e:
                sys.stderr.write(f"ERROR: {e}\n")
                return 2
            doc = {"schema": "holdings/v6", "run_id": a.run_id, "source": "args", "raw": a.holdings,
                   "positions": h["positions"], "cash_usd": h["cash_usd"]}
        _write_json(os.path.join(rd, "holdings.json"), doc)
        print(json.dumps(doc, ensure_ascii=False))
        return 0
    if a.table:
        try:
            b = write_table(rd, run_id=a.run_id)
        except (FileNotFoundError, ValueError) as e:
            sys.stderr.write(f"ERROR: {e}\n")
            return 2
        sys.stderr.write(f"book: total ${b['total_usd']:,.2f}, cash {b['cash_pct']}%, "
                         f"menu {b['candidate_menu']}, authority {b['caps_in_effect']['authority']}\n")
        return 0
    try:
        o = solve_run(rd, run_id=a.run_id)
    except (FileNotFoundError, ValueError) as e:
        sys.stderr.write(f"ERROR: {e}\n")
        return 2
    _write_json(os.path.join(rd, "orders.json"), o)
    for x in o["orders"]:
        sys.stderr.write(f"  {x['action']} {x['ticker']} {x['shares']} @ {x['limit_ref_price']} ({x['sleeve']})\n")
    sys.stderr.write(f"orders: {o['lines']} lines, post cash ${o['post_trade']['cash_usd']:,.2f} "
                     f"({o['post_trade']['cash_pct']}%), conversion {o['conversion_used']}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
