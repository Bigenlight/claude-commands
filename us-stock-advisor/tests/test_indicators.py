"""Team 1 — fetch_indicators.py (offline; synthetic frames / CSV)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import config as C
import fetch_indicators as FI
import helpers as H

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "scripts", "fetch_indicators.py")


def _run(args, env_state=None):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    if env_state is not None:
        env["US_ADVISOR_STATE"] = str(env_state)
    return subprocess.run([sys.executable, SCRIPT] + args, capture_output=True, text=True, env=env,
                          timeout=120)


def test_partial_bar_dropped_and_age_nonnegative(tmp_path):
    frames = H.synthetic_frames(["QQQ", "NVDA"], end_date="2026-10-08")
    ind = FI.compute_all(frames, as_of_date="2026-10-08")
    nv = ind["tickers"]["NVDA"]
    assert nv["partial_bar_dropped"] == "2026-10-08"
    assert nv["as_of_close_date"] == "2026-10-07"
    assert nv["price"] == round(float(frames["NVDA"]["Close"].iloc[-2]), 2)
    assert nv["data_age_hours"] >= 0
    assert ind["as_of_close_date"] == "2026-10-07"
    assert ind["summary"]["stalest_data_hours"] >= 0

    # a frame that already ends before as_of keeps its last bar
    ind2 = FI.compute_all(H.synthetic_frames(["QQQ"]), as_of_date="2026-10-08")
    assert ind2["tickers"]["QQQ"]["partial_bar_dropped"] is None
    assert ind2["tickers"]["QQQ"]["as_of_close_date"] == "2026-10-07"

    # A4-4: a negative age is an error, never a silent pass
    f, _ = FI.drop_partial_bar(frames["NVDA"], "2026-10-08")
    with pytest.raises(RuntimeError, match="negative data_age_hours"):
        FI._compute_one(f, datetime(2026, 10, 7, 12, tzinfo=timezone.utc), None)

    # same through the CLI in offline CSV mode
    csv = H.write_csv(frames, tmp_path / "px.csv")
    r = _run(["--tickers", "NVDA", "--csv", csv, "--as-of", "2026-10-08"])
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["schema"] == "indicators/v6"
    assert out["tickers"]["NVDA"]["partial_bar_dropped"] == "2026-10-08"
    assert out["tickers"]["NVDA"]["data_age_hours"] >= 0
    assert "signal" not in out["tickers"]["NVDA"] and "levels" not in out["tickers"]["NVDA"]



KST, ET = ZoneInfo("Asia/Seoul"), ZoneInfo("America/New_York")


def _no_wall_clock(monkeypatch):
    def boom():
        raise AssertionError("real wall clock used; tests must inject now=")
    monkeypatch.setattr(FI, "_now_utc", boom)


def test_default_clock_is_us_session_date_with_dst_correct_close(monkeypatch):
    """Ruling 1: default evaluation date = US-Eastern date rolled forward after 16:00 ET;
    close instant = 16:00 America/New_York (not a fixed 21:00 UTC). Clock injected."""
    _no_wall_clock(monkeypatch)
    frames = H.synthetic_frames(["QQQ", "NVDA"], end_date="2026-10-09")     # ends Friday
    fri_close = float(frames["NVDA"]["Close"].iloc[-1])

    # Saturday 08:30 KST (= Friday 19:30 EDT, 23:30 UTC): Friday's completed bar is kept
    sat_kst = datetime(2026, 10, 10, 8, 30, tzinfo=KST)
    assert FI._ref_clock(None, now=sat_kst)[0].isoformat() == "2026-10-10"
    ind = FI.compute_all(frames, now=sat_kst)
    nv = ind["tickers"]["NVDA"]
    assert nv["partial_bar_dropped"] is None
    assert nv["as_of_close_date"] == "2026-10-09" and ind["as_of_close_date"] == "2026-10-09"
    assert nv["price"] == round(fri_close, 2)
    assert nv["data_age_hours"] == 3.5                     # 23:30 UTC - 20:00 UTC (16:00 EDT)
    assert ind["as_of_run_iso"] == "2026-10-09T23:30:00Z"  # now_ref stays the real instant
    assert not ind["errors"]

    # Friday 16:30 EDT (20:30 UTC): bar kept, no negative data_age_hours (old 21:00 UTC bug)
    fri_1630 = datetime(2026, 10, 9, 16, 30, tzinfo=ET)
    ind = FI.compute_all(frames, now=fri_1630)
    assert not ind["errors"], ind["errors"]
    nv = ind["tickers"]["NVDA"]
    assert nv["as_of_close_date"] == "2026-10-09" and nv["partial_bar_dropped"] is None
    assert nv["data_age_hours"] == 0.5

    # Friday 14:00 EDT (US session in progress): Friday's partial bar is dropped
    fri_1400 = datetime(2026, 10, 9, 14, 0, tzinfo=ET)
    ind = FI.compute_all(frames, now=fri_1400)
    nv = ind["tickers"]["NVDA"]
    assert nv["partial_bar_dropped"] == "2026-10-09"
    assert nv["as_of_close_date"] == "2026-10-08"
    assert nv["price"] == round(float(frames["NVDA"]["Close"].iloc[-2]), 2)
    assert nv["data_age_hours"] == 22.0                    # Fri 18:00 UTC - Thu 20:00 UTC

    # DST: the close instant follows New York time
    assert FI._close_utc(datetime(2026, 10, 9).date()) == datetime(2026, 10, 9, 20, tzinfo=timezone.utc)
    assert FI._close_utc(datetime(2026, 1, 9).date()) == datetime(2026, 1, 9, 21, tzinfo=timezone.utc)
    # EST Saturday 08:30 KST (= Friday 18:30 EST) also keeps Friday's bar
    assert FI._ref_clock(None, now=datetime(2026, 1, 10, 8, 30, tzinfo=KST))[0].isoformat() == "2026-01-10"

    # explicit --as-of path unchanged: D, D 12:00 UTC
    d, ref = FI._ref_clock("2026-10-08", now=sat_kst)
    assert d.isoformat() == "2026-10-08" and ref == datetime(2026, 10, 8, 12, tzinfo=timezone.utc)

def test_regime_up_and_down():
    up = H.synthetic_frames(["QQQ"], drift={"QQQ": 0.0015}, vol={"QQQ": 0.006})["QQQ"]
    dn = H.synthetic_frames(["QQQ"], drift={"QQQ": -0.0015}, vol={"QQQ": 0.006})["QQQ"]
    ru, rd = FI.regime_from_qqq(up), FI.regime_from_qqq(dn)
    assert ru["regime"] == "UP" and ru["above_sma200"] and ru["above_sma10m"]
    assert rd["regime"] == "DOWN" and rd["above_sma200"] is False and rd["above_sma10m"] is False
    # completed months only: the series ends 2026-10-07, so October is ignored
    assert ru["eval_month"] == "2026-09-30"
    crashed = up.copy()
    oct_mask = crashed.index >= "2026-10-01"
    crashed.loc[oct_mask, "Close"] = crashed.loc[oct_mask, "Close"] * 0.5
    rc = FI.regime_from_qqq(crashed)
    assert rc["monthly_close"] == ru["monthly_close"] and rc["sma_10m"] == ru["sma_10m"]
    assert rc["above_sma10m"] is True and rc["above_sma200"] is False and rc["regime"] == "DOWN"
    # insufficient history -> default invested
    short = H.synthetic_frames(["QQQ"], days=150)["QQQ"]
    rs = FI.regime_from_qqq(short)
    assert rs["regime"] == "UP" and "insufficient history" in rs["reason"]
    # compute_all carries the same regime block
    ind = FI.compute_all({"QQQ": dn}, as_of_date="2026-10-08")
    assert ind["regime"]["regime"] == "DOWN"


def test_rs_rank_excludes_core_and_sgov():
    tickers = list(C.ALL_TICKERS)
    drift, vol = H.rs_shaped_drifts(tickers)
    frames = H.synthetic_frames(tickers, drift=drift, vol=vol, end_prices=H.today_prices())
    ind = FI.compute_all(frames, as_of_date="2026-10-08")
    tk = ind["tickers"]
    for t in ("QQQ", "QQQM", "SGOV"):
        assert tk[t]["rs_rank"] is None
    ranks = sorted(tk[t]["rs_rank"] for t in C.SLEEVE_ELIGIBLE)
    assert ranks == list(range(1, len(C.SLEEVE_ELIGIBLE) + 1))
    top = max(C.SLEEVE_ELIGIBLE, key=lambda t: tk[t]["rs_score"])
    assert tk[top]["rs_rank"] == 1 and ind["rs_ranking"][0] == top == "NVDA"
    assert ind["rs_ranking"][:5] == list(H.DEFAULT_RS_ORDER[:5])
    assert not set(ind["rs_ranking"]) & {"QQQ", "QQQM", "SGOV"}
    nv = tk["NVDA"]
    assert nv["rs_3m_pp"] == pytest.approx(nv["ret_3m_pct"] - tk["QQQ"]["ret_3m_pct"], abs=0.02)
    assert nv["rs_score"] == pytest.approx((nv["rs_3m_pp"] + nv["rs_6m_pp"]) / 2, abs=0.02)
    assert tk["QQQ"]["rs_3m_pp"] == 0.0
    assert nv["pct_from_52w_high"] <= 0 and nv["high_52w"] >= nv["price"]
    # earnings -> busday count from as_of_close_date
    ind2 = FI.compute_all(frames, as_of_date="2026-10-08", earnings={"NVDA": "2026-10-21", "AMZN": None})
    assert ind2["tickers"]["NVDA"]["next_earnings_date"] == "2026-10-21"
    assert ind2["tickers"]["NVDA"]["earnings_in_sessions"] == 10
    assert ind2["tickers"]["AMZN"]["earnings_in_sessions"] is None


def test_return_since_uses_first_session_on_or_after_date(tmp_path):
    f = H.synthetic_frames(["NVDA"])["NVDA"]
    r = FI.return_since(f, "2026-10-03")            # Saturday -> Monday 10-05
    assert r["since_session_date"] == "2026-10-05"
    base = float(f.loc["2026-10-05", "Close"])
    last = float(f["Close"].iloc[-1])
    assert r["close_at_since"] == round(base, 2)
    assert r["close_now"] == round(last, 2)
    assert r["return_since_pct"] == round((last / base - 1) * 100, 2)
    assert FI.return_since(f, "2026-12-01")["return_since_pct"] is None   # after the last bar

    csv = H.write_csv(H.synthetic_frames(["NVDA", "QQQ"]), tmp_path / "px.csv")
    p = _run(["--since", "NVDA:2026-10-03", "--since", "QQQ:2026-10-05", "--csv", csv,
              "--as-of", "2026-10-08"])
    assert p.returncode == 0, p.stderr
    rows = json.loads(p.stdout)["since"]
    assert rows[0]["ticker"] == "NVDA" and rows[0]["return_since_pct"] == r["return_since_pct"]
    assert set(rows[0]) >= {"ticker", "since", "close_at_since", "close_now", "return_since_pct"}
    assert _run(["--since", "NVDA-2026-10-03", "--csv", csv]).returncode == 2


def test_attach_since_fills_tickers_leaves_macro_null(tmp_path):
    frames = H.synthetic_frames(["NVDA", "QQQ"])
    csv = H.write_csv(frames, tmp_path / "px.csv")
    ev = {"schema": "evidence/v6", "run_id": "R", "items": [
        {"id": "E1", "entity": "NVDA", "tickers": ["NVDA"], "event_type": "GUIDANCE",
         "published_iso": "2026-10-05T14:00:00Z", "return_since_pct": None},
        {"id": "E2", "entity": "MACRO", "tickers": ["QQQ"], "event_type": "MACRO_PRINT",
         "published_iso": "2026-10-05T12:30:00Z", "return_since_pct": None},
        {"id": "E3", "entity": "ZZZZ", "tickers": ["ZZZZ"], "event_type": "MA",
         "published_iso": "2026-10-05T12:30:00Z", "return_since_pct": None}],
        "dropped": [], "calendar": [], "scouts_received": [], "notes": ""}
    path = tmp_path / "evidence.json"
    H.write_json(path, ev)
    r = _run(["--attach-since", str(path), "--csv", csv, "--as-of", "2026-10-08"])
    assert r.returncode in (0, 1), r.stderr        # 1: ZZZZ has no data (reported, not fatal)
    doc = H.read_json(path)
    nv, macro, unk = doc["items"]
    base = float(frames["NVDA"].loc["2026-10-05", "Close"])
    assert isinstance(nv["return_since_pct"], float)
    assert nv["return_since_pct"] == round((float(frames["NVDA"]["Close"].iloc[-1]) / base - 1) * 100, 2)
    assert macro["return_since_pct"] is None
    assert unk["return_since_pct"] is None
    assert doc["calendar"] == [] and doc["schema"] == "evidence/v6"
    first = path.read_bytes()
    r2 = _run(["--attach-since", str(path), "--csv", csv, "--as-of", "2026-10-08"])
    assert r2.returncode == r.returncode
    assert path.read_bytes() == first                # idempotent
    assert _run(["--attach-since", str(tmp_path / "nope.json")]).returncode == 2


def test_run_id_writes_indicators_with_held_extras(state, tmp_path):
    tickers = list(C.ALL_TICKERS) + ["GLD"]
    csv = H.write_csv(H.synthetic_frames(tickers, end_prices=H.today_prices()), tmp_path / "px.csv")
    rd = state / "run" / "R1"
    H.write_json(rd / "holdings.json", {"schema": "holdings/v6", "positions": {"QQQ": 2, "GLD": 1},
                                        "cash_usd": 10.0})
    r = _run(["--run-id", "R1", "--csv", csv, "--as-of", "2026-10-08", "--extra", "TSLA"], state)
    assert r.returncode == 1, r.stderr                # TSLA not in CSV -> partial failure
    ind = H.read_json(rd / "indicators.json")
    assert ind["run_id"] == "R1" and "GLD" in ind["tickers"]
    assert ind["tickers"]["GLD"]["rs_rank"] is None   # not sleeve-eligible
    assert [e["ticker"] for e in ind["errors"]] == ["TSLA"]
    assert ind["summary"] == {"requested": 22, "succeeded": 21, "failed": 1,
                              "stalest_data_hours": ind["summary"]["stalest_data_hours"]}
    assert ind["tickers"]["QQQ"]["price"] == 757.73
    # no data at all -> exit 3, nothing written
    empty_csv = tmp_path / "empty.csv"
    empty_csv.write_text("date,ticker,open,high,low,close,volume\n")
    r3 = _run(["--run-id", "R2", "--csv", str(empty_csv)], state)
    assert r3.returncode == 3
    assert not (state / "run" / "R2" / "indicators.json").exists()
    assert _run(["--bogus"], state).returncode == 2


@pytest.mark.network
def test_network_load_frames_live():
    frames = FI.load_frames(["QQQ", "NVDA"])
    ind = FI.compute_all(frames, requested=["QQQ", "NVDA"])
    assert ind["tickers"]["QQQ"]["price"] > 0
    assert ind["tickers"]["QQQ"]["data_age_hours"] >= 0
    assert ind["regime"]["regime"] in ("UP", "DOWN")
