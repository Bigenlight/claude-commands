"""Team 3 — watchdog.py: events, triggers.json, watchdog marks, dry run, exit codes.

Offline: prices come from a synthetic long CSV (helpers.synthetic_frames + write_csv).
notify-send is never called (notify=False / --no-notify).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

import config as C
import helpers as H
import ledger as L
import watchdog as W

SK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
END = "2026-10-13"          # last synthetic session (Tue)
AS_OF = "2026-10-14"        # evaluation date


def _csv(tmp_path, nvda=203.0, qqq_move=-0.075, end=END):
    prices = H.today_prices()
    prices["NVDA"] = nvda
    frames = H.synthetic_frames(C.ALL_TICKERS, end_date=end, end_prices=prices,
                                drift={"QQQ": 0.0015}, vol={"QQQ": 0.006})
    q = frames["QQQ"]
    last = q.index[-1]
    new_close = float(q["Close"].iloc[-2]) * (1 + qqq_move)
    q.loc[last, "Close"] = new_close
    q.loc[last, "Low"] = min(float(q.loc[last, "Low"]), new_close)
    q.loc[last, "High"] = max(float(q.loc[last, "High"]), new_close)
    return H.write_csv(frames, str(tmp_path / "prices.csv")), frames


def _regime(frames):
    import fetch_indicators as FI
    return FI.compute_all(frames, as_of_date=AS_OF)["regime"]["regime"]


def _seed_ledger(regime, sgov=None):
    px = H.today_prices()
    acct = {"positions": {"QQQ": 2, "NVDA": 1}, "cash_usd": 143.06,
            "total_usd": round(2 * px["QQQ"] + px["NVDA"] + 143.06, 2)}
    core = {"positions": {"QQQ": 2, "QQQM": 1}, "cash_usd": 68.59, "total_usd": 1895.99}
    L.append({"type": "epoch", "date": "2026-10-10", "run_id": "20261010-0830", "version": C.VERSION,
              "account": {"positions": core["positions"], "cash_usd": 68.59}, "total_usd": 1895.99})
    L.append({"type": "mark", "date": "2026-10-09", "run_id": "20261010-0830", "source": "run",
              "prices": px, "qqq_px": px["QQQ"], "account": acct, "shadow": dict(core, orders=[]),
              "paper": {d: dict(core) for d in C.DECIDER_IDS}, "supersedes": None})
    L.append({"type": "decision", "date": "2026-10-10", "run_id": "20261010-0830", "regime": regime,
              "targets": {"CORE": 0.87, "NVDA": 0.13},
              "theses": [{"ticker": "NVDA", "status": "OPEN", "entered_run_id": "20261010-0830",
                          "entered_date": "2026-10-10", "entry_price": 237.47, "horizon_weeks": 8,
                          "horizon_end_date": "2026-12-05", "reason_code": "GUIDANCE_RAISE",
                          "evidence_ids": ["E1"], "thesis_30w": "mock",
                          "kill_conditions": [{"type": "PRICE_BELOW", "value": 205.0},
                                              {"type": "DATE", "value": "2026-12-05"}]}],
              "sgov": sgov or {"weight": 0.0, "entered_run_id": None, "runs_held": 0},
              "paper_books": {"D1_TREND": {"positions": {"QQQ": 2, "NVDA": 1}, "cash_usd": 143.06}},
              "unchanged_weeks": 0})


def _snapshot(d):
    out = {}
    for root, _, files in os.walk(d):
        for f in files:
            p = os.path.join(root, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, d)] = fh.read()
    return out


# ---------------------------------------------------------------- 28
def test_watchdog_events_and_triggers_json(state, tmp_path):
    csv, frames = _csv(tmp_path)
    regime = _regime(frames)
    assert regime == "UP"
    _seed_ledger(regime)

    # dry run first: prints the would-be triggers, writes nothing at all
    before = _snapshot(state)
    dry = W.run(as_of=AS_OF, csv=csv, dry_run=True, notify=False)
    assert _snapshot(state) == before
    assert dry["dry_run"] is True and dry["would_append_mark"] is True and dry["mark_appended"] is False

    trig = W.run(as_of=AS_OF, csv=csv, notify=False)
    kinds = {(e["kind"], e["ticker"]) for e in trig["events"]}
    assert ("KILL_PRICE", "NVDA") in kinds and ("SHOCK_MOVE", "QQQ") in kinds
    assert not any(k == "REGIME_FLIP" for k, _ in kinds)
    kp = next(e for e in trig["events"] if e["kind"] == "KILL_PRICE")
    assert kp["severity"] == "HIGH" and kp["detail"]["close"] == pytest.approx(203.0)
    assert kp["detail"]["value"] == 205.0 and kp["first_seen"] == AS_OF
    assert trig["event_run_needed"] is True
    assert trig["affected_tickers"] == ["NVDA", "QQQ"]
    assert trig["mark_appended"] is True and trig["notified"] is False
    assert trig["as_of_close_date"] == END and trig["consumed_by_run"] is None
    assert trig["regime"] == {"regime": "UP", "prev_regime": "UP", "flipped": False}

    on_disk = H.read_json(C.TRIGGERS_JSON)
    assert on_disk["events"] == trig["events"] and on_disk["schema"] == "triggers/v6"

    recs = L.load()
    mk = [r for r in recs if r["type"] == "mark"][-1]
    assert mk["source"] == "watchdog" and mk["run_id"] == "WD-20261013" and mk["date"] == END
    assert mk["account"]["positions"] == {"QQQ": 2, "NVDA": 1}
    assert mk["account"]["total_usd"] == pytest.approx(
        2 * mk["prices"]["QQQ"] + 203.0 + 143.06, abs=0.01)
    # shadow carried (no roll), paper D1 from the decision's paper_books, others from the mark
    assert mk["shadow"]["positions"] == {"QQQ": 2, "QQQM": 1} and "orders" not in mk["shadow"]
    assert mk["paper"]["D1_TREND"]["positions"] == {"QQQ": 2, "NVDA": 1}
    assert mk["paper"]["D3_MACRO"]["positions"] == {"QQQ": 2, "QQQM": 1}
    # the kill is recorded once as a sticky trigger line -> digest reports it FIRED
    kl = [r for r in recs if r["type"] == "trigger"]
    assert [(r["kind"], r["ticker"], r["state"]) for r in kl] == [("KILL_PRICE", "NVDA", "FIRED")]
    dg = L.digest(recs, triggers=on_disk)
    ot = dg["open_theses"][0]
    assert ot["kill_fired"] is True
    assert {k["type"]: k["status"] for k in ot["kill_conditions"]}["PRICE_BELOW"] == "FIRED"


def test_watchdog_regime_flip_sgov_expiry_and_stale(state, tmp_path):
    csv, frames = _csv(tmp_path, nvda=240.0, qqq_move=0.001)
    _seed_ledger("DOWN" if _regime(frames) == "UP" else "UP",
                 sgov={"weight": 0.2, "entered_run_id": "x", "runs_held": C.SGOV_EXPIRY_RUNS - 1})
    # runs_held == EXPIRY-1: aggregate still keeps SGOV on the next run (it expires only when it
    # reads runs_held == SGOV_EXPIRY_RUNS) -> no warning yet
    dry = W.run(as_of=AS_OF, csv=csv, dry_run=True, notify=False)
    assert "SGOV_EXPIRY" not in {e["kind"] for e in dry["events"]}
    dec = dict(L._last(L.load(), "decision"),
               sgov={"weight": 0.2, "entered_run_id": "x", "runs_held": C.SGOV_EXPIRY_RUNS})
    L.append(dec)
    trig = W.run(as_of=AS_OF, csv=csv, notify=False)
    kinds = {e["kind"] for e in trig["events"]}
    assert {"REGIME_FLIP", "SGOV_EXPIRY"} <= kinds
    assert "KILL_PRICE" not in kinds and "SHOCK_MOVE" not in kinds
    assert trig["event_run_needed"] is True and "SGOV" in trig["affected_tickers"]

    # a held name missing from the data -> STALE_DATA, account not marked, others are
    L.append({"type": "mark", "date": "2026-10-13", "run_id": "x", "source": "run",
              "account": {"positions": {"QQQ": 2, "ZZZZ": 1}, "cash_usd": 1.0, "total_usd": 2000.0},
              "shadow": {"positions": {"QQQ": 2}, "cash_usd": 1.0, "total_usd": 1500.0}, "paper": {}})
    csv2, _ = _csv(tmp_path, nvda=240.0, qqq_move=0.001, end="2026-10-14")
    trig = W.run(as_of="2026-10-15", csv=csv2, notify=False)
    stale = [e for e in trig["events"] if e["kind"] == "STALE_DATA"]
    assert [e["ticker"] for e in stale] == ["ZZZZ"] and stale[0]["severity"] == "LOW"
    mk = L.load()[-1]
    assert mk["type"] == "mark" and mk["account"] is None and mk["shadow"]["positions"] == {"QQQ": 2}


# ---------------------------------------------------------------- 29
def test_watchdog_no_new_bar_and_no_epoch(state, tmp_path):
    # empty ledger -> exit 4, nothing written anywhere in the state dir
    csv, frames = _csv(tmp_path)
    assert W.main(["--as-of", AS_OF, "--csv", csv, "--no-notify"]) == 4
    assert os.listdir(state) == []

    _seed_ledger(_regime(frames))
    first = W.run(as_of=AS_OF, csv=csv, notify=False)
    assert first["mark_appended"] is True
    n = len(L.load())
    # same session again (e.g. the weekend): no new mark, no duplicate kill line,
    # triggers.json rewritten with mark_appended=false and first_seen carried
    again = W.run(as_of="2026-10-15", csv=csv, notify=False)
    assert again["mark_appended"] is False
    assert len(L.load()) == n
    on_disk = H.read_json(C.TRIGGERS_JSON)
    assert on_disk["mark_appended"] is False and on_disk["eval_date"] == "2026-10-15"
    kp = next(e for e in on_disk["events"] if e["kind"] == "KILL_PRICE")
    assert kp["first_seen"] == AS_OF


def test_watchdog_cli_dry_run_and_lock(state, tmp_path):
    csv, frames = _csv(tmp_path)
    _seed_ledger(_regime(frames))
    before = _snapshot(state)
    env = dict(os.environ, US_ADVISOR_STATE=str(state), PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.run([sys.executable, os.path.join(SK, "scripts", "watchdog.py"), "--as-of", AS_OF,
                        "--csv", csv, "--dry-run", "--ledger", C.LEDGER_JSONL],
                       capture_output=True, text=True, env=env, timeout=120)
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout)
    assert out["dry_run"] is True and out["event_run_needed"] is True
    assert _snapshot(state) == before
    assert W.main(["--as-of", "14-10-2026"]) == 2
    # another watchdog holding the lock -> "already running", nothing written
    import fcntl
    lp = os.path.join(str(state), "watchdog.lock")
    with open(lp, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        assert W.run(as_of=AS_OF, csv=csv, notify=False) is None
    assert not os.path.exists(C.TRIGGERS_JSON)


def test_watchdog_total_data_failure_exits_3(state, tmp_path):
    _seed_ledger("UP")
    empty = tmp_path / "empty.csv"
    empty.write_text("date,ticker,open,high,low,close,volume\n")
    before = _snapshot(state)
    assert W.main(["--as-of", AS_OF, "--csv", str(empty), "--no-notify"]) == 3
    assert _snapshot(state) == before        # not even the lock file


def test_watchdog_never_supersedes_a_run_mark_and_recovers_from_null_account(state, tmp_path):
    csv, frames = _csv(tmp_path, nvda=240.0, qqq_move=0.001)
    _seed_ledger(_regime(frames))
    # a watchdog mark with account=null (held name unpriced) must not erase the holdings
    L.append({"type": "mark", "date": "2026-10-12", "run_id": "WD-20261012", "source": "watchdog",
              "account": None, "shadow": None, "paper": {}})
    trig = W.run(as_of=AS_OF, csv=csv, notify=False)
    assert trig["mark_appended"] is True
    mk = L.load()[-1]
    assert mk["account"]["positions"] == {"QQQ": 2, "NVDA": 1}
    assert L.digest(L.load())["holdings_last_mark"]["positions"] == {"QQQ": 2, "NVDA": 1}
    # same date: a run mark wins over a watchdog mark regardless of append order
    recs = [{"type": "mark", "date": "2026-10-16", "source": "run", "account": {"total_usd": 1.0}},
            {"type": "mark", "date": "2026-10-16", "source": "watchdog", "account": {"total_usd": 2.0}}]
    assert L.marks(recs) == [("2026-10-16", 1.0)]


def test_session_as_of_keeps_the_just_closed_session():
    from datetime import date
    d = date.fromisoformat(W._session_as_of())
    assert abs((d - date.today()).days) <= 1
