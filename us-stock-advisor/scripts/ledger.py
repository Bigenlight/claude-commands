#!/usr/bin/env python3
"""us-stock-advisor v6.0 — append-only JSONL ledger, scoreboard, damage-cap triggers.

Install path: ~/.claude/skills/us-stock-advisor/scripts/ledger.py

One JSON object per line in state/ledger.jsonl. No hash chain: appends hold an
exclusive fcntl.flock on state/ledger.lock and write+flush+fsync one line each.
Line types (config.LEDGER_TYPES): epoch, mark, flow, order, fill, decision, vote, trigger.
Every line has type, ts_utc, date, run_id.

Date semantics: mark.date = US session date (as_of_close_date); epoch/decision/vote/
order/flow/fill/trigger .date = KST run date taken from run_id (YYYYMMDD-HHMM).

Books marked on every `mark` line:
  account  the reported holdings (book.json), valued at the run's prices
  shadow   core-only whole-share book (same flows, same solver, COMMISSION_PCT charged);
           rolled with book.solve(core_only_targets()) at every run mark
  paper    one book per decider, rolled to that decider's last paper_targets
Headline: value_add_pp = account TWR - shadow TWR (unit-value chain, flows removed).

CLI (all take --run-id R; --ledger PATH overrides config.LEDGER_JSONL for tests; the
lock and triggers.json then live next to that ledger)
  --digest                         -> run/R/digest.json (works on an absent ledger)
  --mark [--deposit USD] [--withdraw USD] [--force-remark]
                                   epoch (first time) + fill lines + flow + mark
  --log                            vote x3, order xN, trigger edges, decision;
                                   resets triggers.json (events [], consumed_by_run R)
  --clear-t2 --confirm "re-enable" trigger T2 CLEARED (Theo's manual re-enable)
  --show [--last N]                human-readable tail
Exit: 0 ok · 2 usage / missing or invalid input (incl. a second flow for one run)
      · 5 refused (duplicate run mark; vote/order/decision already logged for this run;
      --clear-t2 while T2 is not active)

Pinned rulings (documented here, not in config):
  * At the epoch the shadow and every paper book are set equal to the account (no roll).
  * A flow line carries receive_date = the mark it arrives with (flows(): receive_date,
    else the first mark dated >= flow.date).
  * weeks_of_data = (last mark date - first mark date).days // 7 + 1 (mark dates are US
    session dates; the epoch line's KST date would make run 1 read as week 0).
  * T2, once CLEARED, re-arms only after TRAILING_26W_DAYS of new marks.
  * pending order runs_unfilled = max(1, carried) + #UNFILLED fills; an order re-recommended
    (same ticker+action) after an UNFILLED fill carries the previous count.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402

EXIT_OK, EXIT_USAGE, EXIT_REFUSED = 0, 2, 5
CLEAR_T2_CONFIRM = "re-enable"
EDGE_KINDS = ("T1", "T2", "T4", "T5", "NOOP")          # state-change trigger lines from --log
KILL_KIND = {"PRICE_BELOW": "KILL_PRICE", "DATE": "KILL_DATE", "EVENT": "KILL_EVENT",
             "RS_RANK_BELOW": "KILL_RS"}
NON_SLEEVE_KEYS = ("CORE", "SGOV", "CASH")
VOTE_POS_KEYS = ("ticker", "action", "reason_code", "evidence_ids", "horizon_weeks", "kill_conditions")


def _book():
    """Team 1's book module (solve/apply_orders/value/core_only_targets), imported lazily
    so --digest/--show work even while book.py is being rebuilt."""
    import book
    return book


# ================================================================= small helpers
def _now_utc_iso():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _d(s):
    return date.fromisoformat(str(s)[:10])


def _kst_today():
    return datetime.now(C.KST).date()


def run_date(run_id):
    """KST run date from 'YYYYMMDD-HHMM' (or 'WD-YYYYMMDD'); falls back to today KST."""
    s = str(run_id or "")
    if s.startswith("WD-"):
        s = s[3:]
    try:
        return datetime.strptime(s[:8], "%Y%m%d").date().isoformat()
    except ValueError:
        return _kst_today().isoformat()


def _r(x, n=4):
    return None if x is None else round(float(x), n)


def _strip_fences(text):
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def _read_json(path, fences=False):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        txt = f.read()
    try:
        return json.loads(_strip_fences(txt) if fences else txt)
    except ValueError:
        return None


def _atomic_write_json(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _ledger_path(path=None):
    return os.path.abspath(path or C.LEDGER_JSONL)


def _sibling(path, name):
    """state file `name` next to the ledger (config path when the ledger is the default)."""
    path = _ledger_path(path)
    default = {"ledger.lock": C.LEDGER_LOCK, "triggers.json": C.TRIGGERS_JSON}.get(name)
    if default and path == os.path.abspath(C.LEDGER_JSONL):
        return default
    return os.path.join(os.path.dirname(path), name)


def triggers_path(ledger_path=None):
    return _sibling(ledger_path, "triggers.json")


# ================================================================= load / append
_HELD_LOCKS: dict = {}


@contextlib.contextmanager
def locked(path=None):
    """Exclusive flock on the ledger lock for a read-check-append sequence. Re-entrant
    within this process (append() inside a locked() block does not deadlock)."""
    lp = _sibling(path, "ledger.lock")
    if lp in _HELD_LOCKS:
        yield
        return
    os.makedirs(os.path.dirname(lp), exist_ok=True)
    fd = os.open(lp, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        _HELD_LOCKS[lp] = fd
        yield
    finally:
        _HELD_LOCKS.pop(lp, None)
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def load(path=None):
    """All ledger records in file order. Blank lines are skipped; corrupt lines (not JSON,
    not an object with a 'type') are skipped and counted in load.corrupt."""
    path = _ledger_path(path)
    recs, bad = [], 0
    if os.path.exists(path):
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                try:
                    r = json.loads(s)
                except ValueError:
                    bad += 1
                    continue
                if not isinstance(r, dict) or "type" not in r:
                    bad += 1
                    continue
                recs.append(r)
    load.corrupt = bad
    return recs


load.corrupt = 0


def _append(rec, path=None):
    if not isinstance(rec, dict) or rec.get("type") not in C.LEDGER_TYPES:
        raise ValueError(f"ledger line type must be one of {C.LEDGER_TYPES}: {rec!r:.200}")
    rec = dict(rec)
    rec.setdefault("ts_utc", _now_utc_iso())
    rec.setdefault("date", None)
    rec.setdefault("run_id", None)
    line = json.dumps(rec, ensure_ascii=False, allow_nan=False) + "\n"
    path = _ledger_path(path)
    with locked(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "ab") as f:
            # a torn tail (crash mid-write) must not swallow the next line
            if f.tell() > 0:
                with open(path, "rb") as rf:
                    rf.seek(-1, os.SEEK_END)
                    if rf.read(1) != b"\n":
                        f.write(b"\n")
            f.write(line.encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
    return rec


def append(rec: dict, path=None) -> None:
    """Validate type, add ts_utc, flock, write+flush+fsync one line."""
    _append(rec, path)


# ================================================================= record selectors
def _of(recs, typ):
    return [r for r in recs if r.get("type") == typ]


def _last(recs, typ):
    for r in reversed(recs):
        if r.get("type") == typ:
            return r
    return None


def _first(recs, typ):
    for r in recs:
        if r.get("type") == typ:
            return r
    return None


def _mark_lines(recs):
    """Effective marks: the last line per mark date (a --force-remark supersedes), by date."""
    by = {}
    for r in recs:
        if r.get("type") == "mark" and r.get("date"):
            d = str(r["date"])[:10]
            if r.get("source") == "watchdog" and by.get(d, {}).get("source") == "run":
                continue                  # a carried-forward watchdog mark never supersedes a run mark
            by[d] = r
    return [by[d] for d in sorted(by)]


def last_account_mark(recs):
    """The last mark (ledger order) whose account book is priced (watchdog marks may carry
    account=null when a held name had no price)."""
    for r in reversed(recs):
        if r.get("type") == "mark" and isinstance(r.get("account"), dict):
            return r
    return None


def _book_state(m, book):
    if book == "account":
        return m.get("account")
    if book == "shadow":
        return m.get("shadow")
    return (m.get("paper") or {}).get(book)


def marks(recs, book="account"):
    """[(date, total_usd)] for 'account' | 'shadow' | <decider_id>, one point per date."""
    out = []
    for m in _mark_lines(recs):
        st = _book_state(m, book)
        if isinstance(st, dict) and st.get("total_usd") is not None:
            out.append((str(m["date"])[:10], float(st["total_usd"])))
    return out


def flows(recs):
    """{receiving mark date: net external flow USD}. A flow is received by the mark named
    in its receive_date, else by the first mark dated >= flow.date (port of v5.1.2
    external_flows' idea). Flows no mark has received yet are omitted."""
    mdates = sorted({str(m["date"])[:10] for m in _mark_lines(recs)})
    out = {}
    for f in _of(recs, "flow"):
        try:
            usd = float(f.get("usd") or 0.0)
        except (TypeError, ValueError):
            continue
        if f.get("in_epoch") or ("receive_date" in f and f["receive_date"] is None):
            continue                      # inside the epoch's base value: never a TWR flow
        recv = f.get("receive_date")
        if recv is None and f.get("date"):
            recv = next((d for d in mdates if d >= str(f["date"])[:10]), None)
        if recv is None:
            continue
        out[str(recv)[:10]] = out.get(str(recv)[:10], 0.0) + usd
    return {d: round(v, 2) for d, v in out.items() if abs(v) > 1e-9}


def _pt(p):
    if isinstance(p, dict):
        return str(p.get("date"))[:10], float(p["total_usd"])
    return str(p[0])[:10], float(p[1])


def twr_detail(points, flows_by_date):
    """Port of v5.1.2 score_recs._twr. Unit-value chain: growth *= (V_b - F_b) / V_a.
    Returns (pct, pnl_usd, net_flow_usd)."""
    pts = [_pt(p) for p in points]
    growth, pnl, net = 1.0, 0.0, 0.0
    for (_, va), (db, vb_raw) in zip(pts, pts[1:]):
        fb = float(flows_by_date.get(db, 0.0))
        vb = vb_raw - fb
        net += fb
        pnl += vb - va
        if va > 0:
            growth *= vb / va
    return ((growth - 1.0) * 100.0 if len(pts) > 1 else 0.0), pnl, net


def twr(points, flows) -> float:
    """Time-weighted return in percent over `points` [(date, total_usd)]."""
    return twr_detail(points, flows)[0]


def _weeks(points):
    if not points:
        return 0
    return (_d(points[-1][0]) - _d(points[0][0])).days // 7 + 1


def _window(points, days):
    """Trailing window ending at the last point; None while weeks of data < days//7.
    Base = the latest point dated <= last - days (else the first point)."""
    if len(points) < 2 or _weeks(points) < days // 7:
        return None
    cutoff = _d(points[-1][0]) - timedelta(days=days)
    base = 0
    for i, (d, _) in enumerate(points):
        if _d(d) <= cutoff:
            base = i
    w = points[base:]
    return w if len(w) >= 2 else None


def _aligned(recs, a="account", b="shadow"):
    pa, pb = marks(recs, a), marks(recs, b)
    common = {d for d, _ in pa} & {d for d, _ in pb}
    return [p for p in pa if p[0] in common], [p for p in pb if p[0] in common]


def trailing_value_add(recs, days, book="account") -> float | None:
    """account (or a paper book) TWR minus shadow TWR over the trailing `days`, in pp.
    None until weeks_of_data >= days // 7."""
    pa, ps = _aligned(recs, book, "shadow")
    wa = _window(pa, days)
    if wa is None:
        return None
    start = wa[0][0]
    ws = [p for p in ps if p[0] >= start]
    fl = flows(recs)
    return _r(twr(wa, fl) - twr(ws, fl))


def sessions_held(entered_date, as_of) -> int:
    """Business days between entry and as_of (numpy.busday_count; ignores US holidays)."""
    import numpy as np
    try:
        return max(0, int(np.busday_count(str(entered_date)[:10], str(as_of)[:10])))
    except Exception:
        return 0


def _add_weeks(d, weeks):
    return (_d(d) + timedelta(weeks=int(weeks))).isoformat()


# ================================================================= scoreboard / brier
def sample_note(weeks):
    """Short honesty note: first sentence of config.HONESTY_LINE_KO."""
    return C.HONESTY_LINE_KO.split(". ")[0].format(weeks=weeks)


def scoreboard(recs):
    pa, ps = _aligned(recs)
    acc_all = marks(recs, "account")
    weeks = _weeks(acc_all)
    sb = {"account_twr_pct": None, "shadow_twr_pct": None, "qqq_pct": None, "value_add_pp": None,
          "vs_qqq_pp": None, "value_add_12w_pp": None, "value_add_26w_pp": None,
          "weeks_of_data": weeks, "n_marks": len(acc_all), "sample_note": sample_note(weeks)}
    if not acc_all:
        return sb
    fl = flows(recs)
    a = twr(acc_all, fl)
    s = twr(ps, fl) if ps else None
    q = None
    qq = [(str(m["date"])[:10], m.get("qqq_px")) for m in _mark_lines(recs) if m.get("qqq_px")]
    if qq and float(qq[0][1]) > 0:
        q = (float(qq[-1][1]) / float(qq[0][1]) - 1.0) * 100.0
    sb.update({
        "account_twr_pct": _r(a), "shadow_twr_pct": _r(s), "qqq_pct": _r(q),
        "value_add_pp": _r(twr(pa, fl) - s) if s is not None and pa else None,
        "vs_qqq_pp": _r(a - q) if q is not None else None,
        "value_add_12w_pp": trailing_value_add(recs, C.TRAILING_12W_DAYS),
        "value_add_26w_pp": trailing_value_add(recs, C.TRAILING_26W_DAYS),
    })
    return sb


def _mark_for_run(recs, run_id, fallback_date=None):
    """The mark taken in run `run_id`, else the last mark dated <= fallback_date."""
    ms = _mark_lines(recs)
    for m in reversed(ms):
        if m.get("run_id") == run_id:
            return m
    if fallback_date:
        cand = [m for m in ms if str(m["date"])[:10] <= str(fallback_date)[:10]]
        return cand[-1] if cand else None
    return None


def brier(recs) -> dict:
    """Per decider: (p_qqq_up_20d - 1[QQQ higher BRIER_HORIZON_SESSIONS later])^2 averaged
    over votes with a mark >= that many sessions after the vote's mark. Always logged;
    score shown (display=True) only at n >= BRIER_MIN_N."""
    ms = [m for m in _mark_lines(recs) if m.get("qqq_px")]
    out = {}
    for d in C.DECIDER_IDS:
        sq = []
        for v in _of(recs, "vote"):
            if v.get("decider_id") != d or v.get("p_qqq_up_20d") is None:
                continue
            m0 = _mark_for_run(recs, v.get("run_id"), v.get("date"))
            if not m0 or not m0.get("qqq_px"):
                continue
            later = next((m for m in ms if sessions_held(m0["date"], m["date"])
                          >= C.BRIER_HORIZON_SESSIONS), None)
            if later is None:
                continue
            hit = 1.0 if float(later["qqq_px"]) > float(m0["qqq_px"]) else 0.0
            p = min(1.0, max(0.0, float(v["p_qqq_up_20d"])))
            sq.append((p - hit) ** 2)
        n = len(sq)
        raw = _r(sum(sq) / n) if n else None
        disp = n >= C.BRIER_MIN_N
        out[d] = {"n": n, "score": raw if disp else None, "raw_score": raw, "display": disp}
    return out


# ================================================================= orders / theses / books
def pending_orders(recs):
    """Orders of the latest decision with no FILLED fill. runs_unfilled = max(1, carried)
    + number of UNFILLED fills; carried from a same (ticker, action) order of an earlier
    decision that was seen UNFILLED."""
    carry, cur = {}, []
    fills = _of(recs, "fill")
    for dec in _of(recs, "decision"):
        rid = dec.get("run_id")
        cur, nxt = [], {}
        for o in _of(recs, "order"):
            if o.get("run_id") != rid:
                continue
            key = (o.get("ticker"), o.get("action"))
            fs = [f for f in fills if f.get("ref_run_id") == rid
                  and (f.get("ticker"), f.get("action")) == key]
            if any(f.get("status") == "FILLED" for f in fs):
                continue
            n_unf = sum(1 for f in fs if f.get("status") == "UNFILLED")
            runs = max(1, carry.get(key, 0)) + n_unf
            cur.append({"ref_run_id": rid, "ticker": o.get("ticker"), "action": o.get("action"),
                        "shares": o.get("shares"), "limit_ref_price": o.get("limit_ref_price"),
                        "runs_unfilled": runs})
            if n_unf:
                nxt[key] = runs
        carry = nxt
    return cur


def _prices_from_indicators(ind):
    out = {}
    for t, v in ((ind or {}).get("tickers") or {}).items():
        if isinstance(v, dict) and v.get("price") is not None:
            out[t] = float(v["price"])
    return out


def _latest_book_state(recs, book):
    """{'positions','cash_usd'} of a shadow/paper book: the latest in ledger order of a
    mark's book or (paper only) a decision's paper_books entry."""
    for r in reversed(recs):
        if r.get("type") == "mark":
            st = _book_state(r, book)
        elif r.get("type") == "decision" and book not in ("account", "shadow"):
            st = (r.get("paper_books") or {}).get(book)
        else:
            continue
        if isinstance(st, dict) and st.get("positions") is not None:
            return {"positions": {t: int(n) for t, n in st["positions"].items() if int(n) > 0},
                    "cash_usd": float(st.get("cash_usd") or 0.0)}
    return None


def _value(positions, cash, prices):
    v = _book().value(positions, cash, prices)
    return round(float(v["total_usd"]), 2), list(v.get("unpriced") or [])


def _compact_orders(orders):
    return [{"ticker": o.get("ticker"), "action": o.get("action"), "shares": int(o.get("shares") or 0),
             "limit_ref_price": o.get("limit_ref_price")} for o in orders or []]


def roll_book(positions, cash, targets, prices):
    """Solve `targets` on a book and apply with COMMISSION_PCT. Returns a book dict."""
    bk = _book()
    res = bk.solve(targets, dict(positions), float(cash), prices)
    orders = res.get("orders") or []
    pos2, cash2 = bk.apply_orders(dict(positions), float(cash), orders, C.COMMISSION_PCT)
    pos2 = {t: int(n) for t, n in pos2.items() if int(n) > 0}
    total, unpriced = _value(pos2, cash2, prices)
    return {"positions": pos2, "cash_usd": round(float(cash2), 2), "total_usd": total,
            "orders": _compact_orders(orders), "unpriced": unpriced}


def _kill_status(k, ticker, entered_date, ind_t, as_of, recs, wd_events):
    typ, val = k.get("type"), k.get("value")
    kind = KILL_KIND.get(typ)
    out = {"type": typ, "value": val, "status": "UNCHECKED", "checked_value": None}
    # sticky: the watchdog recorded this kill as FIRED since entry, or flags it now
    for r in recs:
        if (r.get("type") == "trigger" and r.get("kind") == kind and r.get("ticker") == ticker
                and r.get("state") == "FIRED" and str(r.get("date") or "") >= str(entered_date or "")
                and (r.get("detail") or {}).get("value") == val):
            out["status"] = "FIRED"
            out["checked_value"] = (r.get("detail") or {}).get("checked_value")
            return out
    for e in wd_events or []:
        if e.get("kind") == kind and e.get("ticker") == ticker and (
                (e.get("detail") or {}).get("value") in (None, val)):
            out["status"] = "FIRED"
            out["checked_value"] = (e.get("detail") or {}).get("close")
            return out
    try:
        if typ == "DATE" and as_of:
            out["checked_value"] = str(as_of)[:10]
            out["status"] = "FIRED" if str(as_of)[:10] >= str(val)[:10] else "OK"
        elif typ == "PRICE_BELOW" and ind_t and ind_t.get("price") is not None:
            out["checked_value"] = ind_t["price"]
            out["status"] = "FIRED" if float(ind_t["price"]) < float(val) else "OK"
        elif typ == "RS_RANK_BELOW" and ind_t and ind_t.get("rs_rank") is not None:
            out["checked_value"] = ind_t["rs_rank"]
            out["status"] = "FIRED" if int(ind_t["rs_rank"]) > int(val) else "OK"
    except (TypeError, ValueError):
        out["status"] = "UNCHECKED"
    return out


# ================================================================= triggers
def _last_trigger(recs, kind):
    for r in reversed(recs):
        if r.get("type") == "trigger" and r.get("kind") == kind:
            return r
    return None


def evaluate_triggers(recs, digest_partial) -> dict:
    """Code-enforced damage caps. digest_partial needs 'scoreboard', 'pending_orders',
    'as_of_close_date'. Authority: T2 > T1 > FULL."""
    sb = digest_partial.get("scoreboard") or {}
    as_of = digest_partial.get("as_of_close_date") or _kst_today().isoformat()
    weeks = int(sb.get("weeks_of_data") or 0)
    va12, va26 = sb.get("value_add_12w_pp"), sb.get("value_add_26w_pp")

    # T1: fires on the metric, then stays active until `until` even if it recovers
    t1 = {"active": False, "since": None, "until": None, "value_add_12w_pp": va12}
    last1 = _last_trigger(recs, "T1")
    if (last1 and last1.get("state") in ("FIRED", "ACTIVE") and last1.get("until")
            and str(as_of) < str(last1["until"])):
        t1.update(active=True, since=(last1.get("detail") or {}).get("since") or last1.get("date"),
                  until=last1["until"])
    elif va12 is not None and va12 < C.T1_PP and weeks >= C.T1_WEEKS:
        t1.update(active=True, since=str(as_of)[:10], until=_add_weeks(as_of, C.T1_DURATION_WEEKS))

    # T2: active until a T2 CLEARED line; after a clear it re-arms only after 26w of marks
    t2 = {"active": False, "since": None, "value_add_26w_pp": va26}
    last2 = _last_trigger(recs, "T2")
    if last2 and last2.get("state") in ("FIRED", "ACTIVE"):
        t2.update(active=True, since=(last2.get("detail") or {}).get("since") or last2.get("date"))
    else:
        armed = True
        if last2 and last2.get("state") == "CLEARED":
            armed = (_d(as_of) - _d(last2.get("date"))).days >= C.TRAILING_26W_DAYS
        if armed and va26 is not None and va26 < C.T2_PP and weeks >= C.T2_WEEKS:
            t2.update(active=True, since=str(as_of)[:10])

    # T4: consecutive decisions with post-trade cash > CASH_MAX_PCT and no SGOV
    n4 = 0
    for dec in reversed(_of(recs, "decision")):
        cw = dec.get("post_cash_weight")
        sg = float((dec.get("sgov") or {}).get("weight") or 0.0)
        if cw is not None and float(cw) > C.CASH_MAX_PCT and sg <= 0:
            n4 += 1
        else:
            break
    t4 = {"cash_over_runs": n4, "fire": n4 >= C.T4_CASH_WEEKS}

    # T5: a recommended order unfilled for >= T5_UNFILLED_RUNS runs
    unf = [{"ticker": p["ticker"], "action": p["action"], "shares": p["shares"],
            "runs_unfilled": p["runs_unfilled"]} for p in digest_partial.get("pending_orders") or []]
    t5 = {"unfilled": unf, "fire": any(u["runs_unfilled"] >= C.T5_UNFILLED_RUNS for u in unf)}

    last_dec = _last(recs, "decision")
    uw = int((last_dec or {}).get("unchanged_weeks") or 0)
    noop = {"unchanged_weeks": uw, "flag": uw >= C.NOOP_FLAG_WEEKS}

    authority = "T2_SUSPENDED" if t2["active"] else ("T1_REDUCED" if t1["active"] else "FULL")
    sleeve_eff = {"T2_SUSPENDED": 0.0, "T1_REDUCED": C.T1_SLEEVE_CUT}.get(authority, C.SLEEVE_MAX_PCT)
    return {"T1": t1, "T2": t2, "T4": t4, "T5": t5, "NOOP": noop, "authority": authority,
            "sleeve_max_pct_effective": sleeve_eff, "sgov_authority": not t2["active"]}


def _edge_state(trig, kind):
    """(active, key) of one kind from evaluate_triggers output; key changes => new line."""
    if kind == "T1":
        return trig["T1"]["active"], trig["T1"]["until"]
    if kind == "T2":
        return trig["T2"]["active"], None
    if kind == "T4":
        return trig["T4"]["fire"], None
    if kind == "T5":
        return trig["T5"]["fire"], None
    return trig["NOOP"]["flag"], None


def _persisted_state(recs, kind):
    last = _last_trigger(recs, kind)
    if not last:
        return False, None
    act = last.get("state") in ("FIRED", "ACTIVE")
    return act, (last.get("until") if kind == "T1" and act else None)


# ================================================================= digest
def _as_of(recs, indicators):
    if indicators and indicators.get("as_of_close_date"):
        return str(indicators["as_of_close_date"])[:10]
    lm = _last(recs, "mark")
    if lm and lm.get("date"):
        return str(lm["date"])[:10]
    return _kst_today().isoformat()


def _defaults_sgov():
    return {"held": False, "weight": 0.0, "entered_run_id": None, "runs_held": 0,
            "expires_after_runs_held": C.SGOV_EXPIRY_RUNS}


def digest(recs, indicators=None, run_id=None, triggers=None) -> dict:
    """State digest for one run (schema digest/v6). Works on an empty ledger."""
    epoch = _first(recs, "epoch")
    decisions = _of(recs, "decision")
    last_dec = decisions[-1] if decisions else None
    last_mark = last_account_mark(recs)
    as_of = _as_of(recs, indicators)
    # theses are dated in KST run dates: compare them against the run date, not the
    # (earlier) US session date, so horizon / min-hold / DATE kills are not a run late
    ref_date = run_date(run_id) if run_id else as_of
    ind_t = (indicators or {}).get("tickers") or {}
    prices = dict((last_mark or {}).get("prices") or {})
    prices.update(_prices_from_indicators(indicators))
    wd_events = (triggers or {}).get("events") or []

    sb = scoreboard(recs)
    pend = pending_orders(recs)

    hl = None
    if last_mark and isinstance(last_mark.get("account"), dict):
        a = last_mark["account"]
        hl = {"date": last_mark.get("date"), "positions": dict(a.get("positions") or {}),
              "cash_usd": a.get("cash_usd"), "total_usd": a.get("total_usd")}
    sh = _latest_book_state(recs, "shadow")
    shadow_last = None
    if last_mark and isinstance(last_mark.get("shadow"), dict):
        s = last_mark["shadow"]
        shadow_last = {"positions": dict(s.get("positions") or {}), "cash_usd": s.get("cash_usd"),
                       "total_usd": s.get("total_usd")}
    elif sh:
        shadow_last = dict(sh, total_usd=None)

    # current weights of the last reported account at today's prices
    cur_w = {}
    if hl:
        tot = float(hl.get("cash_usd") or 0.0)
        vals = {}
        for t, n in (hl["positions"] or {}).items():
            if t in prices:
                vals[t] = int(n) * float(prices[t])
        tot += sum(vals.values())
        if tot > 0:
            cur_w = {t: v / tot for t, v in vals.items()}

    open_theses = []
    for th in (last_dec or {}).get("theses") or []:
        if th.get("status", "OPEN") != "OPEN":
            continue
        t = th.get("ticker")
        it = ind_t.get(t) if isinstance(ind_t.get(t), dict) else None
        kills = [_kill_status(k, t, th.get("entered_date"), it, ref_date if indicators or last_mark else None,
                              recs, wd_events) for k in th.get("kill_conditions") or []]
        held = sessions_held(th.get("entered_date"), ref_date)
        hed = th.get("horizon_end_date")
        open_theses.append({
            "ticker": t, "entered_date": th.get("entered_date"), "entered_run_id": th.get("entered_run_id"),
            "entry_price": th.get("entry_price"),
            "current_price": (it or {}).get("price", prices.get(t)),
            "horizon_end_date": hed, "horizon_reached": bool(hed and ref_date >= str(hed)[:10]),
            "horizon_weeks": th.get("horizon_weeks"),
            "reason_code": th.get("reason_code"), "evidence_ids": th.get("evidence_ids") or [],
            "thesis_30w": th.get("thesis_30w"), "kill_conditions": kills,
            "sessions_held": held, "min_hold_met": held >= C.MIN_HOLD_SESSIONS,
            "current_weight": _r(cur_w.get(t, 0.0)),
            "kill_fired": any(k["status"] == "FIRED" for k in kills)})

    sg = _defaults_sgov()
    if last_dec and last_dec.get("sgov"):
        s = last_dec["sgov"]
        w = float(s.get("weight") or 0.0)
        sg.update(held=w > 0, weight=w, entered_run_id=s.get("entered_run_id"),
                  runs_held=int(s.get("runs_held") or 0))

    # decider history: last 8 own votes, numbers only
    hist = {}
    for d in C.DECIDER_IDS:
        rows = []
        for v in [v for v in _of(recs, "vote") if v.get("decider_id") == d][-8:]:
            tw = v.get("target_weights") or {}
            m0 = _mark_for_run(recs, v.get("run_id"), v.get("date"))
            pvs = None
            if m0:
                pp, ps = _aligned(recs, d, "shadow")
                pp = [p for p in pp if p[0] >= str(m0["date"])[:10]]
                ps = [p for p in ps if p[0] >= str(m0["date"])[:10]]
                if len(pp) >= 2:
                    fl = flows(recs)
                    pvs = _r(twr(pp, fl) - twr(ps, fl))
            rows.append({"run_id": v.get("run_id"), "date": v.get("date"),
                         "sgov_weight": v.get("sgov_weight"),
                         "sleeve": {t: w for t, w in tw.items() if t not in NON_SLEEVE_KEYS and w},
                         "p_qqq_up_20d": v.get("p_qqq_up_20d"),
                         "paper_vs_shadow_pp_to_date": pvs})
        hist[d] = rows

    run_marks = {m.get("run_id") for m in _of(recs, "mark") if m.get("source") == "run"}
    partial = {"scoreboard": sb, "pending_orders": pend, "as_of_close_date": as_of}
    trig = evaluate_triggers(recs, partial)
    return {
        "schema": "digest/v6", "run_id": run_id, "as_of_close_date": as_of,
        "first_run": not decisions,
        "epoch_start": (epoch or {}).get("date"),
        "runs_since_epoch": len(run_marks),
        "last_run_id": (last_dec or {}).get("run_id"),
        "scoreboard": sb, "triggers": trig, "pending_orders": pend,
        "holdings_last_mark": hl, "shadow_last_mark": shadow_last,
        "open_theses": open_theses, "sgov": sg,
        "last_targets": (last_dec or {}).get("targets"),
        "unchanged_weeks": int((last_dec or {}).get("unchanged_weeks") or 0),
        "decider_history": hist, "brier": brier(recs),
        "watchdog_triggers": triggers,
        "ledger_corrupt_lines": getattr(load, "corrupt", 0),
    }


def read_triggers(ledger_path=None):
    return _read_json(triggers_path(ledger_path))


# ================================================================= CLI commands
def _err(msg):
    sys.stderr.write(f"ledger: {msg}\n")


def cmd_digest(run_id, ledger_path):
    rd = C.run_dir(run_id)
    recs = load(ledger_path)
    ind = _read_json(os.path.join(rd, "indicators.json"))
    dg = digest(recs, indicators=ind, run_id=run_id, triggers=read_triggers(ledger_path))
    _atomic_write_json(os.path.join(rd, "digest.json"), dg)
    print(json.dumps({"digest": os.path.join(rd, "digest.json"), "first_run": dg["first_run"],
                      "epoch_start": dg["epoch_start"], "authority": dg["triggers"]["authority"],
                      "pending_orders": len(dg["pending_orders"]),
                      "open_theses": len(dg["open_theses"])}, ensure_ascii=False))
    return EXIT_OK


def _account_from_book(bk):
    pos = {}
    for t, v in (bk.get("positions") or {}).items():
        n = v.get("shares") if isinstance(v, dict) else v
        if n is None:
            continue
        if int(n) != float(n):
            raise ValueError(f"whole shares only: {t} {n}")
        if int(n) > 0:
            pos[str(t).upper()] = int(n)
    return pos, float(bk.get("cash_usd") or 0.0)


def cmd_mark(run_id, ledger_path, deposit=0.0, withdraw=0.0, force_remark=False):
    rd = C.run_dir(run_id)
    bk = _read_json(os.path.join(rd, "book.json"))
    ind = _read_json(os.path.join(rd, "indicators.json"))
    if bk is None or ind is None:
        _err(f"--mark needs {rd}/book.json and indicators.json")
        return EXIT_USAGE
    if deposit < 0 or withdraw < 0:
        _err("--deposit/--withdraw take positive USD amounts")
        return EXIT_USAGE
    flow = round(float(deposit) - float(withdraw), 2)
    try:
        positions, cash = _account_from_book(bk)
    except (TypeError, ValueError) as e:
        _err(str(e))
        return EXIT_USAGE
    as_of = str(ind.get("as_of_close_date") or bk.get("as_of_close_date") or "")[:10]
    if not as_of:
        _err("no as_of_close_date in indicators.json/book.json")
        return EXIT_USAGE
    rdate = run_date(run_id)
    with locked(ledger_path):
        recs = load(ledger_path)
        prev = [m for m in _of(recs, "mark") if str(m.get("date"))[:10] == as_of and m.get("source") == "run"]
        if prev and not force_remark:
            _err(f"a run mark for {as_of} already exists (run {prev[-1].get('run_id')}); "
                 "use --force-remark to supersede it")
            return EXIT_REFUSED
        # idempotency (re-mark or rerun after a crash): this run's flow/fill lines count once
        run_flows = [f for f in _of(recs, "flow") if f.get("run_id") == run_id and not f.get("in_epoch")]
        if run_flows and flow:
            _err(f"run {run_id} already recorded a flow of {sum(float(f.get('usd') or 0) for f in run_flows):+.2f}; "
                 "refusing a second --deposit/--withdraw for the same run")
            return EXIT_USAGE
        roll_flow = flow or round(sum(float(f.get("usd") or 0.0) for f in run_flows), 2)
        done_fills = {(f.get("ref_run_id"), f.get("ticker"), f.get("action"))
                      for f in _of(recs, "fill") if f.get("run_id") == run_id}
        # books roll from the state BEFORE any superseded mark of this session
        sup = {m.get("ts_utc") for m in prev}
        base = [r for r in recs if not (r.get("type") == "mark" and r.get("ts_utc") in sup)]
        last_mark = _last(recs, "mark")
        prices = dict((last_mark or {}).get("prices") or {})
        prices.update(_prices_from_indicators(ind))
        acc_total, unpriced = _value(positions, cash, prices)
        if unpriced and bk.get("total_usd") is not None:
            acc_total = round(float(bk["total_usd"]), 2)
        account = {"positions": positions, "cash_usd": round(cash, 2), "total_usd": acc_total}
        lines = []
        new_epoch = _first(recs, "epoch") is None
        if new_epoch:
            lines.append({"type": "epoch", "date": rdate, "run_id": run_id, "version": C.VERSION,
                          "note": "v6 epoch start",
                          "account": {"positions": positions, "cash_usd": round(cash, 2)},
                          "prices": {t: prices[t] for t in positions if t in prices},
                          "total_usd": acc_total})
        # fills from book.json reconciliation, matched to the pending orders
        pend = {(p["ref_run_id"], p["ticker"], p["action"]): p for p in pending_orders(recs)}
        rec = bk.get("reconciliation") or {}
        for status, key in (("FILLED", "filled"), ("UNFILLED", "unfilled")):
            for e in rec.get(key) or []:
                if not isinstance(e, dict):
                    continue
                t, act = e.get("ticker"), e.get("action")
                ref = e.get("ref_run_id")
                p = pend.get((ref, t, act)) or next(
                    (q for k, q in pend.items() if k[1] == t and k[2] == act), None)
                ref = ref or (p or {}).get("ref_run_id")
                if (ref, t, act) in done_fills:
                    continue
                done_fills.add((ref, t, act))
                n0 = int((p or e).get("runs_unfilled") or 1)
                lines.append({"type": "fill", "date": rdate, "run_id": run_id, "ref_run_id": ref,
                              "ticker": t, "action": act, "shares": e.get("shares", (p or {}).get("shares")),
                              "status": status,
                              "runs_unfilled": n0 + 1 if status == "UNFILLED" else n0})
        if flow and not new_epoch:
            lines.append({"type": "flow", "date": rdate, "run_id": run_id, "usd": flow,
                          "note": "deposit" if flow > 0 else "withdraw", "receive_date": as_of})
        elif flow and new_epoch:
            # the epoch already contains it; recorded for audit, never a TWR flow
            lines.append({"type": "flow", "date": rdate, "run_id": run_id, "usd": flow,
                          "note": ("deposit" if flow > 0 else "withdraw") + " (inside epoch)",
                          "receive_date": None, "in_epoch": True})
        # shadow + paper books
        if new_epoch:
            same = {"positions": dict(positions), "cash_usd": round(cash, 2), "total_usd": acc_total,
                    "orders": []}
            shadow = dict(same)
            paper = {d: dict(same, positions=dict(positions)) for d in C.DECIDER_IDS}
        else:
            flow = roll_flow
            st = _latest_book_state(base, "shadow") or {"positions": positions, "cash_usd": cash - flow}
            shadow = roll_book(st["positions"], st["cash_usd"] + flow,
                               _book().core_only_targets(), prices)
            last_dec = _last(base, "decision")
            ptargets = (last_dec or {}).get("paper_targets") or {}
            paper = {}
            for d in C.DECIDER_IDS:
                ps = _latest_book_state(base, d) or {"positions": positions, "cash_usd": cash - flow}
                tg = ptargets.get(d)
                try:
                    if tg:
                        paper[d] = roll_book(ps["positions"], ps["cash_usd"] + flow, tg, prices)
                    else:
                        tot, unp = _value(ps["positions"], ps["cash_usd"] + flow, prices)
                        paper[d] = {"positions": ps["positions"], "cash_usd": round(ps["cash_usd"] + flow, 2),
                                    "total_usd": tot, "orders": [], "unpriced": unp}
                except Exception as e:  # one paper book must not block the account mark
                    paper[d] = None
                    _err(f"paper book {d} not rolled: {e}")
        mark = {"type": "mark", "date": as_of, "run_id": run_id, "source": "run",
                "prices": prices, "qqq_px": prices.get(C.BENCHMARK),
                "account": account, "shadow": shadow, "paper": paper,
                "supersedes": prev[-1].get("ts_utc") if prev else None,
                "reconciliation": {k: rec.get(k) for k in ("unexpected", "implied_cash_delta_usd", "warnings")
                                   if k in rec}}
        lines.append(mark)
        for ln in lines:
            _append(ln, ledger_path)
    print(json.dumps({"appended": [ln["type"] for ln in lines], "as_of_close_date": as_of,
                      "account_total_usd": acc_total, "shadow_total_usd": shadow.get("total_usd"),
                      "shadow_orders": shadow.get("orders")}, ensure_ascii=False))
    return EXIT_OK


def _decider_file(rd, d):
    doc = _read_json(os.path.join(rd, "deciders", f"{d}.json"), fences=True)
    return doc if isinstance(doc, dict) else {}


def cmd_log(run_id, ledger_path):
    rd = C.run_dir(run_id)
    agg = _read_json(os.path.join(rd, "aggregate_out.json"))
    if not isinstance(agg, dict) or not agg.get("valid"):
        _err("aggregate_out.json missing or valid=false; nothing logged")
        return EXIT_USAGE
    od = _read_json(os.path.join(rd, "orders.json"))
    if not isinstance(od, dict):
        _err("orders.json missing; nothing logged")
        return EXIT_USAGE
    bk = _read_json(os.path.join(rd, "book.json")) or {}
    ind = _read_json(os.path.join(rd, "indicators.json")) or {}
    rdate = run_date(run_id)
    tpath = triggers_path(ledger_path)
    with locked(ledger_path):
        recs = load(ledger_path)
        if any(r.get("type") in ("decision", "vote", "order") and r.get("run_id") == run_id for r in recs):
            _err(f"run {run_id} already has decision/vote/order lines; refusing to log twice")
            return EXIT_REFUSED
        last_mark = _last(recs, "mark")
        prices = dict((last_mark or {}).get("prices") or {})
        prices.update(_prices_from_indicators(ind))
        prev_dec = _last(recs, "decision")
        lines = []

        # --- votes
        vt = {v.get("decider"): v for v in agg.get("vote_table") or [] if isinstance(v, dict)}
        ptargets = agg.get("paper_targets") or {}
        for d in C.DECIDER_IDS:
            v, f = vt.get(d) or {}, _decider_file(rd, d)
            der = f.get("derisk") or {}
            # `positions` = the VALIDATED entries aggregate.py actually counted (same basis as
            # target_weights); the decider's raw file entries are kept under `positions_raw`.
            raw_pos = f.get("positions") if isinstance(f.get("positions"), list) else []
            lines.append({
                "type": "vote", "date": rdate, "run_id": run_id, "decider_id": d,
                "target_weights": ptargets.get(d) or {},
                "positions": [{k: p.get(k) for k in VOTE_POS_KEYS}
                              for p in v.get("positions") or [] if isinstance(p, dict)],
                "positions_raw": [{k: p.get(k) for k in VOTE_POS_KEYS}
                                  for p in raw_pos if isinstance(p, dict)],
                "sgov_weight": v.get("sgov", (ptargets.get(d) or {}).get("SGOV", 0.0)),
                "derisk_reason_code": der.get("reason_code"),
                "p_qqq_up_20d": v.get("p_qqq_up_20d", f.get("p_qqq_up_20d")),
                "dissent_note": v.get("dissent_note", f.get("dissent_note")),
                "drops": [x for x in agg.get("drops") or [] if x.get("decider") == d],
                "clamps": [x for x in agg.get("clamps") or [] if x.get("decider") == d]})

        # --- orders
        orders = [o for o in od.get("orders") or [] if isinstance(o, dict)]
        for o in orders:
            lines.append({"type": "order", "date": rdate, "run_id": run_id, "ticker": o.get("ticker"),
                          "action": o.get("action"), "shares": int(o.get("shares") or 0),
                          "limit_ref_price": o.get("limit_ref_price"), "usd": o.get("usd"),
                          "sleeve": o.get("sleeve"), "reason": o.get("reason"),
                          "priority": o.get("priority"), "status": "RECOMMENDED"})

        # --- theses
        targets = agg.get("targets") or {}
        sleeve_meta = agg.get("sleeve") or {}
        dropped = {x.get("ticker") for x in od.get("dropped_targets") or [] if isinstance(x, dict)}
        prev_open = {t.get("ticker"): t for t in (prev_dec or {}).get("theses") or []
                     if t.get("status", "OPEN") == "OPEN"}
        buy_px = {o.get("ticker"): o.get("limit_ref_price") for o in orders if o.get("action") == "BUY"}
        theses = []
        for t, w in targets.items():
            if t in NON_SLEEVE_KEYS or not w or float(w) <= 0 or t in dropped:
                continue
            meta = sleeve_meta.get(t) or {}
            kills = [{"type": k.get("type"), "value": k.get("value")}
                     for k in meta.get("kill_conditions") or [] if isinstance(k, dict)]
            if t in prev_open:
                th = dict(prev_open[t])
                if meta.get("kill_override") and kills:
                    th["kill_conditions"] = kills
                th["status"] = "OPEN"
                # Past its horizon but kept by a real 2/3 vote (rule MEDIAN, not frozen by an event
                # scope) with a horizon_weeks: the vote extends the thesis, so re-base the horizon
                # from this run. Otherwise horizon_reached stays true forever and the watchdog
                # emits HORIZON every day.
                hed, hw_new = th.get("horizon_end_date"), meta.get("horizon_weeks")
                if (hed and rdate >= str(hed)[:10] and hw_new and meta.get("rule") == "MEDIAN"
                        and not meta.get("frozen") and meta.get("action") in ("HOLD", "TRIM")):
                    th["horizon_weeks"] = int(hw_new)
                    th["horizon_end_date"] = _add_weeks(rdate, int(hw_new))
                    th["horizon_rebased_run_id"] = run_id
            else:
                hw = int(meta.get("horizon_weeks") or C.HORIZON_WEEKS_MIN)
                codes = meta.get("reason_codes") or []
                th = {"ticker": t, "status": "OPEN", "entered_run_id": run_id, "entered_date": rdate,
                      "entry_price": buy_px.get(t, prices.get(t)), "horizon_weeks": hw,
                      "horizon_end_date": _add_weeks(rdate, hw),
                      "reason_code": codes[0] if codes else None,
                      "evidence_ids": meta.get("evidence_ids") or [], "kill_conditions": kills,
                      "thesis_30w": meta.get("thesis_30w")}
            theses.append(th)
        kept = {th["ticker"] for th in theses}
        whys = {x.get("ticker"): x.get("why") for x in agg.get("exits") or [] if isinstance(x, dict)}
        closed = [{"ticker": t, "why": whys.get(t, "TARGET_ZERO"), "exit_price": prices.get(t)}
                  for t in prev_open if t not in kept]

        # --- SGOV runs_held
        # Semantics (pinned): the entry/renewal run logs runs_held=0; each later run that keeps SGOV
        # without a renewing vote logs +1. aggregate.py keeps SGOV while digest.sgov.runs_held <
        # SGOV_EXPIRY_RUNS and expires it on the run that READS runs_held == SGOV_EXPIRY_RUNS, i.e.
        # SGOV survives the entry run + SGOV_EXPIRY_RUNS unrenewed runs and is sold on the next one.
        # watchdog.py warns (SGOV_EXPIRY) once the last decision logged runs_held >= SGOV_EXPIRY_RUNS,
        # meaning "the next weekly run sells SGOV unless re-voted".
        # An EVENT run that leaves SGOV out of scope (aggregate sgov.rule == "OUT_OF_SCOPE") freezes it:
        # runs_held is carried unchanged, so mid-week event runs never burn SGOV's grace runs.
        sga = agg.get("sgov") or {}
        w_sg = float(targets.get("SGOV", sga.get("weight", 0.0)) or 0.0)
        psg = (prev_dec or {}).get("sgov") or {}
        if w_sg > 0 and "SGOV" not in dropped:
            if float(psg.get("weight") or 0.0) > 0:
                sgov = {"weight": w_sg, "entered_run_id": psg.get("entered_run_id"),
                        "runs_held": (0 if sga.get("renewed")
                                      else int(psg.get("runs_held") or 0)
                                      + (0 if sga.get("rule") == "OUT_OF_SCOPE" else 1))}
            else:
                sgov = {"weight": w_sg, "entered_run_id": run_id, "runs_held": 0}
        else:
            sgov = {"weight": 0.0, "entered_run_id": None, "runs_held": 0}

        # --- paper books solved to paper_targets at today's prices
        paper_books, paper_errors = {}, {}
        for d, tg in ptargets.items():
            st = _latest_book_state(recs, d)
            if st is None:
                continue
            try:
                b = roll_book(st["positions"], st["cash_usd"], tg, prices)
                paper_books[d] = {k: b[k] for k in ("positions", "cash_usd", "total_usd", "orders")}
            except Exception as e:
                paper_errors[d] = str(e)[:200]
                paper_books[d] = {"positions": st["positions"], "cash_usd": st["cash_usd"]}

        post = od.get("post_trade") or {}
        cw = (post.get("weights") or {}).get("CASH")
        if cw is None and post.get("cash_pct") is not None:
            cw = float(post["cash_pct"]) / 100.0
        trig_file = _read_json(tpath) or {}
        decision = {
            "type": "decision", "date": rdate, "run_id": run_id,
            "as_of_close_date": agg.get("as_of_close_date") or ind.get("as_of_close_date"),
            "regime": agg.get("regime"), "targets": targets, "orders_n": len(orders),
            "theses": theses, "closed_theses": closed, "sgov": sgov,
            "unchanged": bool(agg.get("unchanged")),
            "unchanged_weeks": int(agg.get("unchanged_weeks") or 0),
            "noop_flag": bool(agg.get("noop_flag")),
            "drops": len(agg.get("drops") or []), "clamps": len(agg.get("clamps") or []),
            "no_majority": len(agg.get("no_majority") or []),
            "authority": (bk.get("caps_in_effect") or {}).get("authority")
            or (agg.get("caps") or {}).get("authority") or "FULL",
            "p_qqq_up_20d_median": agg.get("p_qqq_up_20d_median"),
            "post_cash_weight": _r(cw), "conversion_used": od.get("conversion_used"),
            "paper_targets": ptargets, "paper_books": paper_books, "paper_errors": paper_errors,
            "event_scope": agg.get("event_scope"),
            "event_consumed": [{"kind": e.get("kind"), "ticker": e.get("ticker")}
                               for e in trig_file.get("events") or [] if isinstance(e, dict)]}

        # --- trigger edges: persisted state vs state after this decision
        after_recs = recs + lines + [decision]
        part = {"scoreboard": scoreboard(after_recs), "pending_orders": pending_orders(after_recs),
                "as_of_close_date": _as_of(after_recs, ind)}
        after = evaluate_triggers(after_recs, part)
        for kind in EDGE_KINDS:
            now_act, now_key = _edge_state(after, kind)
            was_act, was_key = _persisted_state(recs, kind)
            if now_act == was_act and (not now_act or now_key == was_key):
                continue
            sub = after[kind]
            lines.append({"type": "trigger", "date": rdate, "run_id": run_id, "kind": kind,
                          "state": "FIRED" if now_act else "CLEARED", "ticker": None,
                          "detail": dict(sub, authority=after["authority"]),
                          "until": sub.get("until") if kind == "T1" and now_act else None})
        lines.append(decision)
        for ln in lines:
            _append(ln, ledger_path)

        # --- consume the watchdog events
        tf = dict(trig_file) if trig_file else {"schema": "triggers/v6"}
        tf.update(events=[], event_run_needed=False, affected_tickers=[], consumed_by_run=run_id,
                  consumed_utc=_now_utc_iso())
        _atomic_write_json(tpath, tf)
    print(json.dumps({"appended": {t: sum(1 for ln in lines if ln["type"] == t)
                                   for t in ("vote", "order", "trigger", "decision")},
                      "theses": [t["ticker"] for t in theses], "authority": after["authority"]},
                     ensure_ascii=False))
    return EXIT_OK


def cmd_clear_t2(run_id, ledger_path, confirm):
    if confirm != CLEAR_T2_CONFIRM:
        _err(f'refused: --clear-t2 needs --confirm "{CLEAR_T2_CONFIRM}" exactly')
        return EXIT_USAGE
    with locked(ledger_path):
        recs = load(ledger_path)
        t2 = evaluate_triggers(recs, {"scoreboard": scoreboard(recs), "pending_orders": [],
                                      "as_of_close_date": _as_of(recs, None)})["T2"]
        if not t2["active"]:
            _err("refused: T2 is not active (a CLEARED line would disarm the T2 cap for 26 weeks)")
            return EXIT_REFUSED
        _append({"type": "trigger", "date": run_date(run_id), "run_id": run_id, "kind": "T2",
                 "state": "CLEARED", "ticker": None,
                 "detail": {"by": "manual --clear-t2", "since": t2.get("since"),
                            "value_add_26w_pp": t2.get("value_add_26w_pp")}, "until": None},
                ledger_path)
    print(json.dumps({"appended": "trigger T2 CLEARED"}))
    return EXIT_OK


def cmd_show(ledger_path, last_n):
    recs = load(ledger_path)
    for r in recs[-last_n:] if last_n else recs:
        t = r.get("type")
        head = f"{r.get('ts_utc', '')[:19]} {t:<8} {r.get('date')} {r.get('run_id')}"
        if t == "mark":
            a, s = r.get("account") or {}, r.get("shadow") or {}
            extra = f"src={r.get('source')} acct={a.get('total_usd')} shadow={s.get('total_usd')} qqq={r.get('qqq_px')}"
        elif t in ("order", "fill"):
            extra = f"{r.get('action')} {r.get('ticker')} {r.get('shares')} {r.get('status')}"
        elif t == "trigger":
            extra = f"{r.get('kind')} {r.get('state')} {r.get('ticker') or ''} until={r.get('until')}"
        elif t == "vote":
            extra = f"{r.get('decider_id')} {r.get('target_weights')}"
        elif t == "decision":
            extra = f"targets={r.get('targets')} orders={r.get('orders_n')} authority={r.get('authority')}"
        elif t == "flow":
            extra = f"usd={r.get('usd')} {r.get('note')}"
        else:
            extra = f"total={r.get('total_usd')}"
        print(f"{head} {extra}")
    if load.corrupt:
        print(f"({load.corrupt} corrupt line(s) skipped)")
    return EXIT_OK


def _parse_args(argv):
    p = argparse.ArgumentParser(prog="ledger.py", description="us-stock-advisor v6 ledger")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--digest", action="store_true")
    g.add_argument("--mark", action="store_true")
    g.add_argument("--log", action="store_true")
    g.add_argument("--clear-t2", action="store_true")
    g.add_argument("--show", action="store_true")
    p.add_argument("--run-id")
    p.add_argument("--ledger", help="ledger path override (tests); default config.LEDGER_JSONL")
    p.add_argument("--deposit", type=float, default=0.0)
    p.add_argument("--withdraw", type=float, default=0.0)
    p.add_argument("--force-remark", action="store_true")
    p.add_argument("--confirm")
    p.add_argument("--last", type=int, default=20)
    a = p.parse_args(argv)
    if not a.show and not a.run_id:
        p.error("--run-id is required")
    if (a.deposit or a.withdraw) and not a.mark:
        p.error("--deposit/--withdraw only with --mark")
    return a


def main(argv=None):
    try:
        a = _parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)
    if a.digest:
        return cmd_digest(a.run_id, a.ledger)
    if a.mark:
        return cmd_mark(a.run_id, a.ledger, a.deposit, a.withdraw, a.force_remark)
    if a.log:
        return cmd_log(a.run_id, a.ledger)
    if a.clear_t2:
        return cmd_clear_t2(a.run_id, a.ledger, a.confirm)
    return cmd_show(a.ledger, a.last)


if __name__ == "__main__":
    sys.exit(main())
