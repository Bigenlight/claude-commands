#!/usr/bin/env python3
"""us-stock-advisor v6.1 — daily Python-only watchdog (no LLM, no Slack).

Install path: ~/.claude/skills/us-stock-advisor/scripts/watchdog.py
Scheduled by scripts/install_timer.sh (systemd user timer, Persistent=true).

Each run: load the ledger (needs an `epoch` line, i.e. one weekly skill run), price
ALL_TICKERS + held names (fetch_indicators.load_frames + compute_all), append a
`mark` (source "watchdog", run_id "WD-YYYYMMDD"; account/shadow/paper positions carried
forward and re-priced, no roll) when a new session closed, evaluate events, write
state/triggers.json (atomic replace) and raise a desktop notification. The next weekly
skill run reads triggers.json; `event_run_needed` makes it an event run.

Events: KILL_PRICE (close < value), KILL_RS (rs_rank > value), KILL_DATE / HORIZON
(eval date >= date), REGIME_FLIP (regime != last decision's), SHOCK_MOVE (|1-day move| >=
SHOCK_MOVE_PCT on a held name or QQQ), EARNINGS_D2 (held sleeve name, earnings within
EARNINGS_WARN_SESSIONS), SGOV_EXPIRY (last decision's runs_held >= SGOV_EXPIRY_RUNS, i.e. the next
weekly run sells SGOV unless re-voted: aggregate expires it when it reads runs_held ==
SGOV_EXPIRY_RUNS; see ledger.cmd_log), STALE_DATA.
A KILL_* event is also appended once per thesis as a `trigger` FIRED line so a kill that
fired mid-week stays fired (ledger digest reads it) even if the price recovers.

CLI: watchdog.py [--as-of YYYY-MM-DD] [--csv PATH] [--dry-run] [--no-notify] [--ledger PATH]
Exit: 0 ok (also "already running") · 2 usage · 3 no price data at all (nothing written)
      · 4 no epoch in the ledger ("run the weekly skill once first"; nothing written)
--dry-run prints the triggers JSON and writes nothing (no mark, no triggers.json, no lock).
Partial-bar date: --as-of if given, else the US Eastern date rolled forward after the
16:00 ET close (so the 07:30 KST slot sees the session that just closed). Date-based
events (KILL_DATE, HORIZON) use the KST evaluation date.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
import ledger as L  # noqa: E402

EVENT_RUN_KINDS = ("KILL_PRICE", "KILL_DATE", "KILL_EVENT", "KILL_RS", "REGIME_FLIP", "SHOCK_MOVE")
SEVERITY = {"KILL_PRICE": "HIGH", "KILL_DATE": "HIGH", "KILL_EVENT": "HIGH", "KILL_RS": "HIGH",
            "REGIME_FLIP": "HIGH", "SHOCK_MOVE": "HIGH", "HORIZON": "MED", "EARNINGS_D2": "MED",
            "SGOV_EXPIRY": "MED", "STALE_DATA": "LOW"}


class WatchdogError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def _fi():
    import fetch_indicators
    return fetch_indicators


def _event(kind, ticker, detail):
    return {"kind": kind, "ticker": ticker, "severity": SEVERITY.get(kind, "LOW"), "detail": detail}


def _reprice(state, prices):
    """Carry a book forward at new prices. None if any position is unpriced."""
    if not state:
        return None
    pos = {t: int(n) for t, n in (state.get("positions") or {}).items() if int(n) > 0}
    if any(t not in prices for t in pos):
        return None
    cash = float(state.get("cash_usd") or 0.0)
    total = round(cash + sum(n * float(prices[t]) for t, n in pos.items()), 2)
    return {"positions": pos, "cash_usd": round(cash, 2), "total_usd": total}


def _eval_date(as_of):
    if as_of:
        return str(as_of)[:10]
    return datetime.now(C.KST).date().isoformat()


def evaluate_events(ind, recs, eval_date):
    """Pure: events from indicators + ledger state. Returns (events, regime_info)."""
    tick = ind.get("tickers") or {}
    last_mark = L.last_account_mark(recs)
    last_dec = L._last(recs, "decision")
    acct = ((last_mark or {}).get("account") or {}).get("positions") or {}
    held = sorted(t for t, n in acct.items() if int(n) > 0)
    theses = [t for t in (last_dec or {}).get("theses") or [] if t.get("status", "OPEN") == "OPEN"]
    ev = []

    for th in theses:
        t = th.get("ticker")
        it = tick.get(t) or {}
        for k in th.get("kill_conditions") or []:
            typ, val = k.get("type"), k.get("value")
            try:
                if typ == "PRICE_BELOW" and it.get("price") is not None and float(it["price"]) < float(val):
                    ev.append(_event("KILL_PRICE", t, {"type": typ, "value": val, "close": it["price"]}))
                elif typ == "RS_RANK_BELOW" and it.get("rs_rank") is not None and int(it["rs_rank"]) > int(val):
                    ev.append(_event("KILL_RS", t, {"type": typ, "value": val, "rs_rank": it["rs_rank"]}))
                elif typ == "DATE" and val and eval_date >= str(val)[:10]:
                    ev.append(_event("KILL_DATE", t, {"type": typ, "value": val, "eval_date": eval_date}))
            except (TypeError, ValueError):
                continue
        hed = th.get("horizon_end_date")
        if hed and eval_date >= str(hed)[:10]:
            ev.append(_event("HORIZON", t, {"horizon_end_date": hed, "eval_date": eval_date}))

    reg = (ind.get("regime") or {}).get("regime")
    prev = (last_dec or {}).get("regime")
    flipped = bool(reg and prev and reg != prev)
    if flipped:
        r = ind.get("regime") or {}
        ev.append(_event("REGIME_FLIP", None, {"from": prev, "to": reg,
                                                "above_sma200": r.get("above_sma200"),
                                                "above_sma10m": r.get("above_sma10m")}))

    shock_set = list(dict.fromkeys(held + [C.BENCHMARK]))
    for t in shock_set:
        chg = (tick.get(t) or {}).get("chg_1d_pct")
        if chg is not None and abs(float(chg)) >= C.SHOCK_MOVE_PCT * 100.0:
            ev.append(_event("SHOCK_MOVE", t, {"chg_1d_pct": chg, "close": tick[t].get("price")}))

    sleeve_held = [t for t in held if t in C.SLEEVE_ELIGIBLE]
    for t in sleeve_held:
        n = (tick.get(t) or {}).get("earnings_in_sessions")
        if n is not None and 0 <= int(n) <= C.EARNINGS_WARN_SESSIONS:
            ev.append(_event("EARNINGS_D2", t, {"next_earnings_date": tick[t].get("next_earnings_date"),
                                                "earnings_in_sessions": n}))

    sg = (last_dec or {}).get("sgov") or {}
    if float(sg.get("weight") or 0.0) > 0 and int(sg.get("runs_held") or 0) >= C.SGOV_EXPIRY_RUNS:
        ev.append(_event("SGOV_EXPIRY", C.DERISK_TICKER, {"runs_held": sg.get("runs_held"),
                                                         "expires_after_runs_held": C.SGOV_EXPIRY_RUNS}))

    watch = list(dict.fromkeys(held + [th.get("ticker") for th in theses]))
    for t in watch:
        it = tick.get(t)
        if not it:
            ev.append(_event("STALE_DATA", t, {"reason": "no price data"}))
        elif it.get("stale"):
            ev.append(_event("STALE_DATA", t, {"reason": "stale", "data_age_hours": it.get("data_age_hours")}))
    return ev, {"regime": reg, "prev_regime": prev, "flipped": flipped}


def _carry_first_seen(events, prev_file, today):
    prev = {}
    for e in (prev_file or {}).get("events") or []:
        if isinstance(e, dict) and e.get("first_seen"):
            prev.setdefault((e.get("kind"), e.get("ticker")), e["first_seen"])
    for e in events:
        e["first_seen"] = prev.get((e["kind"], e["ticker"]), today)
    return events


def _kill_lines(events, recs, eval_date):
    """trigger FIRED lines for KILL_* events not yet recorded for the current thesis."""
    last_dec = L._last(recs, "decision")
    entered = {t.get("ticker"): t.get("entered_date") for t in (last_dec or {}).get("theses") or []}
    out = []
    for e in events:
        if not e["kind"].startswith("KILL_"):
            continue
        val = (e.get("detail") or {}).get("value")
        seen = any(r.get("type") == "trigger" and r.get("kind") == e["kind"] and r.get("ticker") == e["ticker"]
                   and r.get("state") == "FIRED" and (r.get("detail") or {}).get("value") == val
                   and str(r.get("date") or "") >= str(entered.get(e["ticker"]) or "")
                   for r in recs)
        if not seen:
            d = dict(e.get("detail") or {})
            d["checked_value"] = d.get("close", d.get("rs_rank", d.get("eval_date")))
            out.append({"type": "trigger", "date": eval_date, "run_id": f"WD-{eval_date.replace('-', '')}",
                        "kind": e["kind"], "state": "FIRED", "ticker": e["ticker"], "detail": d,
                        "until": None})
    return out


def send_notification(events, critical):
    """Desktop notification via notify-send. Returns True on success, never raises."""
    exe = shutil.which("notify-send")
    if not exe:
        return False
    body = f"{len(events)} events: " + ", ".join(
        f"{e['kind']} {e['ticker']}" if e.get("ticker") else e["kind"] for e in events)
    try:
        r = subprocess.run([exe, "-u", "critical" if critical else "normal", "us-advisor", body[:500]],
                           capture_output=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return False


def _locked_or_none(lock_path):
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def _session_as_of():
    """Evaluation date for the partial-bar rule when --as-of is not given: the US Eastern
    date, rolled to the next day once the 16:00 ET close has passed. At the 07:30 KST timer
    slot (evening ET) this keeps the session that just closed; during US hours it drops the
    in-progress bar. (UTC 'today' would drop the just-closed bar every morning.)"""
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    return (datetime.now(ZoneInfo("America/New_York")) + timedelta(hours=8)).date().isoformat()


def run(as_of=None, csv=None, dry_run=False, notify=True, ledger_path=None) -> dict | None:
    """One watchdog pass. Returns the triggers dict (None if another watchdog holds the
    lock). Raises WatchdogError(4) without an epoch, WatchdogError(3) with no data; in
    both cases nothing is written. All reads/fetches happen before any lock or write."""
    lp = L._ledger_path(ledger_path)
    recs = L.load(lp)
    if L._first(recs, "epoch") is None:
        raise WatchdogError(4, "no epoch in the ledger: run the weekly skill once first")
    eval_date = _eval_date(as_of)
    last_mark = L.last_account_mark(recs)
    last_dec = L._last(recs, "decision")
    acct = (last_mark or {}).get("account") or {}
    held = [t for t, n in (acct.get("positions") or {}).items() if int(n) > 0]
    thesis_t = [t.get("ticker") for t in (last_dec or {}).get("theses") or []]
    tickers = list(dict.fromkeys(list(C.ALL_TICKERS) + held + [t for t in thesis_t if t]))

    fi = _fi()
    errors = []
    try:
        frames = fi.load_frames(tickers, csv_path=csv)
        errors += list(getattr(fi, "LAST_LOAD_ERRORS", []) or [])
    except SystemExit as e:
        raise WatchdogError(3, f"price loader exited ({e.code})")
    except Exception as e:
        raise WatchdogError(3, f"price data failure: {e}")
    if not frames:
        raise WatchdogError(3, "no price data at all")
    earnings = {}
    if not csv:
        names = [t for t in held if C.group_of(t) == "name"]
        if names:
            try:
                earnings = fi.fetch_earnings_dates(names) or {}
            except Exception as e:
                errors.append({"ticker": None, "reason": f"earnings: {e}"[:200]})
    ind = fi.compute_all(frames, as_of_date=as_of or _session_as_of(), earnings=earnings)
    tick = ind.get("tickers") or {}
    if not tick:
        raise WatchdogError(3, "no ticker could be priced")
    errors += [e for e in ind.get("errors") or [] if e not in errors]
    prices = {t: float(v["price"]) for t, v in tick.items() if v.get("price") is not None}
    as_of_close = str(ind.get("as_of_close_date") or "")[:10]

    def build_mark(rs):
        """mark line for a session newer than every mark in `rs`, else None."""
        last_date = max((str(m.get("date") or "")[:10] for m in L._of(rs, "mark")), default="")
        if not as_of_close or as_of_close <= last_date:
            return None
        am = L.last_account_mark(rs)
        account = _reprice((am or {}).get("account"), prices)
        shadow = _reprice(L._latest_book_state(rs, "shadow"), prices)
        paper = {d: _reprice(L._latest_book_state(rs, d), prices) for d in C.DECIDER_IDS}
        if not (account or shadow or any(paper.values())):
            return None
        return {"type": "mark", "date": as_of_close, "run_id": f"WD-{as_of_close.replace('-', '')}",
                "source": "watchdog", "prices": prices, "qqq_px": prices.get(C.BENCHMARK),
                "account": account, "shadow": shadow, "paper": paper, "supersedes": None}

    events, regime = evaluate_events(ind, recs, eval_date)
    need = any(e["kind"] in EVENT_RUN_KINDS for e in events)
    affected = {e["ticker"] for e in events if e["kind"] in EVENT_RUN_KINDS and e.get("ticker")}
    if regime["flipped"]:
        affected.add(C.DERISK_TICKER)
    show = list(dict.fromkeys([C.BENCHMARK] + held + [t for t in thesis_t if t]))
    trig = {"schema": "triggers/v6", "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")
            .replace("+00:00", "Z"), "as_of_close_date": as_of_close or None, "eval_date": eval_date,
            "mark_appended": False, "regime": regime,
            "prices": {t: prices.get(t) for t in show},
            "events": events, "event_run_needed": need, "affected_tickers": sorted(affected),
            "notified": False, "consumed_by_run": None, "watchdog_errors": errors}

    if dry_run:
        _carry_first_seen(events, L._read_json(L.triggers_path(lp)), eval_date)
        trig["dry_run"] = True
        trig["would_append_mark"] = build_mark(recs) is not None
        return trig

    fd = _locked_or_none(os.path.join(os.path.dirname(lp), "watchdog.lock"))
    if fd is None:
        return None
    try:
        with L.locked(lp):
            # re-read under the ledger lock: a weekly run may have marked meanwhile
            recs = L.load(lp)
            mark = build_mark(recs)
            if mark is not None:
                L.append(mark, lp)
                trig["mark_appended"] = True
            for ln in _kill_lines(events, recs, eval_date):
                L.append(ln, lp)
            _carry_first_seen(events, L._read_json(L.triggers_path(lp)), eval_date)
            if events and notify:
                trig["notified"] = send_notification(events, need)
            L._atomic_write_json(L.triggers_path(lp), trig)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    return trig


def main(argv=None):
    p = argparse.ArgumentParser(prog="watchdog.py", description="us-stock-advisor daily watchdog")
    p.add_argument("--as-of", help="evaluation date YYYY-MM-DD (default: US-Eastern session date, rolled forward after the 16:00 ET close)")
    p.add_argument("--csv", help="offline long CSV date,ticker,open,high,low,close,volume")
    p.add_argument("--dry-run", action="store_true", help="print triggers JSON, write nothing")
    p.add_argument("--no-notify", action="store_true")
    p.add_argument("--ledger", help="ledger path override (tests)")
    try:
        a = p.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)
    if a.as_of:
        try:
            datetime.strptime(a.as_of, "%Y-%m-%d")
        except ValueError:
            sys.stderr.write("watchdog: --as-of must be YYYY-MM-DD\n")
            return 2
    try:
        trig = run(as_of=a.as_of, csv=a.csv, dry_run=a.dry_run, notify=not a.no_notify,
                   ledger_path=a.ledger)
    except WatchdogError as e:
        sys.stderr.write(f"watchdog: {e}\n")
        return e.code
    if trig is None:
        print("watchdog: already running")
        return 0
    print(json.dumps(trig, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
