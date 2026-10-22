"""Team 2 tests: aggregate.py (plan tests 12-20). Offline; state lives in tmp dirs only."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

SK = Path(__file__).resolve().parents[1]
SCRIPTS = SK / "scripts"
FIX = SK / "tests" / "fixtures"
# conftest.py (Team 1) sets this before any script import; this is only a fallback.
if not os.environ.get("US_ADVISOR_STATE"):
    os.environ["US_ADVISOR_STATE"] = tempfile.mkdtemp(prefix="us_adv_t2_")
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import config as C  # noqa: E402
import aggregate as A  # noqa: E402

# Book facts of 2026-10-07 closes (test data, mirrors the plan's shared context).
PRICES = {"QQQ": 757.73, "QQQM": 311.94, "NVDA": 237.47, "GOOGL": 350.50, "MSFT": 529.76,
          "META": 721.31, "AMZN": 259.92, "AAPL": 336.67, "AMD": 645.86, "TSM": 472.20,
          "AVGO": 376.51, "MU": 1088.00, "PLTR": 194.12, "SOXX": 582.82, "SMH": 625.03,
          "SOXQ": 103.0, "XLK": 201.0, "IGV": 110.0, "SPYM": 91.0, "SGOV": 100.0}
MENU = ["NVDA", "AMZN", "PLTR", "XLK", "SOXQ", "IGV"]
AS_OF = "2026-10-07"
BASE = {"QQQ": 2, "QQQM": 1}
BASE_CASH = 68.59


# ---------------------------------------------------------------- run-dir builder (shared with test_render)
def first_run_digest(**over) -> dict:
    d = {"schema": "digest/v6", "run_id": None, "first_run": True, "epoch_start": None, "runs_since_epoch": 0,
         "scoreboard": {"account_twr_pct": None, "shadow_twr_pct": None, "qqq_pct": None, "value_add_pp": None,
                        "vs_qqq_pp": None, "value_add_12w_pp": None, "value_add_26w_pp": None,
                        "weeks_of_data": 0, "n_marks": 0, "sample_note": None},
         "triggers": {"T1": {"active": False, "since": None, "until": None}, "T2": {"active": False, "since": None},
                      "T4": {"cash_over_runs": 0, "fire": False}, "T5": {"unfilled": [], "fire": False},
                      "NOOP": {"unchanged_weeks": 0, "flag": False}, "authority": "FULL",
                      "sleeve_max_pct_effective": C.SLEEVE_MAX_PCT, "sgov_authority": True},
         "pending_orders": [], "holdings_last_mark": None, "open_theses": [],
         "sgov": {"held": False, "weight": 0.0, "entered_run_id": None, "runs_held": 0,
                  "expires_after_runs_held": C.SGOV_EXPIRY_RUNS},
         "last_targets": None, "unchanged_weeks": 0, "decider_history": {}, "brier": {},
         "watchdog_triggers": None}
    d.update(over)
    return d


def build_run(state, run_id, positions=None, cash=BASE_CASH, regime="UP", menu=None, rs=None,
              digest=None, caps=None, prices=None) -> Path:
    """Write indicators.json, book.json, digest.json, evidence.json for a run dir; return the dir."""
    positions = dict(BASE if positions is None else positions)
    prices = dict(PRICES, **(prices or {}))
    rs = rs or {}
    rd = Path(state) / "run" / run_id
    (rd / "deciders").mkdir(parents=True, exist_ok=True)
    total = cash + sum(n * prices[t] for t, n in positions.items())
    pos = {}
    dg = digest if digest is not None else first_run_digest()
    thesis_t = {str(th.get("ticker", "")).upper() for th in dg.get("open_theses") or []}
    for t, n in positions.items():
        # mirrors book.build_table: a RESIDUAL_FILLER without an open thesis is tagged FILLER
        sl = "CORE" if t in C.CORE_TICKERS else ("SGOV" if t == C.DERISK_TICKER else
                                                  "FILLER" if t == C.RESIDUAL_FILLER and t not in thesis_t
                                                  else "SLEEVE")
        pos[t] = {"shares": n, "price": prices[t], "usd": round(n * prices[t], 2),
                  "pct": round(n * prices[t] / total * 100, 1), "sleeve": sl}
    lot = {}
    for t in C.ALL_TICKERS:
        px = prices[t]
        g = C.group_of(t)
        lb = px * (1 + C.PRICE_BUFFER_PCT)
        lot[t] = {"price": px, "lot_usd_buffered": round(lb, 2), "lot_pct": round(lb / total * 100, 1),
                  "feasible": g in ("core", "derisk") or lb <= C.SLEEVE_POSITION_MAX_PCT * total,
                  "max_shares_in_cash": int(cash // lb), "group": g}
    caps = caps or {"authority": "FULL", "sleeve_max_pct": C.SLEEVE_MAX_PCT,
                    "sleeve_position_max_pct": C.SLEEVE_POSITION_MAX_PCT,
                    "sleeve_max_positions": C.SLEEVE_MAX_POSITIONS,
                    "sgov_max_pct": C.SGOV_MAX_UP if regime == "UP" else C.SGOV_MAX_DOWN,
                    "sgov_authority": True}
    sleeve_usd = sum(p["usd"] for t, p in pos.items() if p["sleeve"] == "SLEEVE")
    book = {"schema": "book/v6", "run_id": run_id, "as_of_close_date": AS_OF, "total_usd": round(total, 2),
            "cash_usd": cash, "cash_pct": round(cash / total * 100, 1), "positions": pos,
            "core_usd": round(sum(p["usd"] for p in pos.values() if p["sleeve"] == "CORE"), 2),
            "sleeve_usd": round(sleeve_usd, 2), "held_sleeve": [t for t, p in pos.items() if p["sleeve"] == "SLEEVE"],
            "caps_in_effect": caps, "lot_table": lot, "candidate_menu": list(menu or MENU),
            "candidate_reasons": {}, "hidden_infeasible": [],
            "reconciliation": {"pending_orders": [], "filled": [], "unfilled": [], "unexpected": [],
                               "implied_cash_delta_usd": 0.0, "warnings": []}}
    up = regime == "UP"
    ind = {"schema": "indicators/v6", "as_of_run_iso": "2026-10-08T10:00:00Z", "as_of_close_date": AS_OF,
           "benchmark": "QQQ",
           "regime": {"regime": regime, "qqq_close": prices["QQQ"], "sma_200": 700.1 if up else 800.0,
                      "above_sma200": up, "monthly_close": 739.77, "sma_10m": 669.49 if up else 760.0,
                      "above_sma10m": up, "eval_month": "2026-09-30", "reason": "test"},
           "tickers": {t: {"price": prices[t], "as_of_close_date": AS_OF, "rs_rank": rs.get(t),
                           "next_earnings_date": None, "earnings_in_sessions": None, "stale": False}
                       for t in C.ALL_TICKERS},
           "rs_ranking": [], "errors": [], "summary": {}}
    (rd / "book.json").write_text(json.dumps(book))
    (rd / "indicators.json").write_text(json.dumps(ind))
    (rd / "digest.json").write_text(json.dumps(digest if digest is not None else first_run_digest()))
    shutil.copy(FIX / "mock_run" / "evidence.json", rd / "evidence.json")
    return rd


def copy_deciders(rd: Path, scenario: str = "mock_run") -> None:
    for f in (FIX / scenario / "deciders").glob("*.json"):
        shutil.copy(f, rd / "deciders" / f.name)


def run_cli(state, run_id, *extra):
    env = dict(os.environ, US_ADVISOR_STATE=str(state), PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run([sys.executable, str(SCRIPTS / "aggregate.py"), "--run-id", run_id, *extra],
                          env=env, capture_output=True, text=True, timeout=60)


def load_out(rd: Path) -> dict:
    return json.loads((rd / "aggregate_out.json").read_text())


def nonzero(targets: dict) -> dict:
    return {k: v for k, v in targets.items() if v > 1e-9}


# ---------------------------------------------------------------- decider doc factories
def dec(did, sleeve=None, positions=None, sgov=0.0, dcode=None, devids=None, p=0.6):
    sleeve = sleeve or {}
    tw = {"CORE": round(1 - sgov - sum(sleeve.values()), 6), "SGOV": sgov, **sleeve}
    return {"decider_id": did, "regime_seen": "UP", "target_weights": tw, "positions": positions or [],
            "derisk": {"sgov_weight": sgov, "reason_code": dcode, "evidence_ids": devids or []},
            "p_qqq_up_20d": p, "dissent_note": "test"}


def pos(t, code="GUIDANCE_RAISE", ev=("E1",), action="ENTER", kills=None, horizon=8):
    return {"ticker": t, "action": action, "reason_code": code, "evidence_ids": list(ev), "horizon_weeks": horizon,
            "kill_conditions": kills if kills is not None else [{"type": "DATE", "value": "2026-12-05"}],
            "thesis_30w": f"{t} test thesis"}


def ex(t, code="BETTER_USE"):
    return {"ticker": t, "action": "EXIT", "reason_code": code, "evidence_ids": []}


def three(*docs):
    return dict(zip(C.DECIDER_IDS, docs))


def agg(rd, docs, scope=None):
    ctx = A.build_ctx(str(rd), scope)
    return A.aggregate(docs, ctx), ctx


def rules(out, rule):
    return [d for d in out["drops"] + out["clamps"] if d["rule"] == rule]


D1, D2, D3 = C.DECIDER_IDS


# ---------------------------------------------------------------- 12
def test_refuses_without_evidence_or_three_deciders(tmp_path):
    state = tmp_path / "state"
    rd = build_run(state, "20261008-1900")
    copy_deciders(rd)
    ev = rd / "evidence.json"
    saved = ev.read_text()
    ev.unlink()
    r = run_cli(state, "20261008-1900")
    assert r.returncode == 2 and "REFUSED" in r.stderr
    assert not (rd / "aggregate_out.json").exists()

    ev.write_text(saved)
    (rd / "deciders" / "D3_MACRO.json").unlink()
    r = run_cli(state, "20261008-1900")
    assert r.returncode == 2 and "D3_MACRO" in r.stderr
    assert not (rd / "aggregate_out.json").exists()

    # all inputs present -> runs; a garbage decider file is a core-only vote, not a crash
    (rd / "deciders" / "D3_MACRO.json").write_text("Sorry, I cannot produce JSON today.")
    r = run_cli(state, "20261008-1900")
    assert r.returncode == 0, r.stderr
    out = load_out(rd)
    assert any(d["rule"] == "INVALID_FILE" and d["decider"] == D3 for d in out["drops"])
    assert out["paper_targets"][D3] == {"CORE": 1.0}
    # no CLI flag can bypass the guard
    assert "--force" not in (SCRIPTS / "aggregate.py").read_text()


# ---------------------------------------------------------------- 13
def test_scenario_a_two_of_three_nvda_median(tmp_path):
    state = tmp_path / "state"
    rd = build_run(state, "20261008-1900")
    copy_deciders(rd)
    d1 = rd / "deciders" / "D1_TREND.json"
    d1.write_text("```json\n" + d1.read_text() + "\n```\n")      # fenced output must still parse
    r = run_cli(state, "20261008-1900")
    assert r.returncode == 0, r.stderr
    out = load_out(rd)
    assert out["schema"] == "aggregate/v6" and out["valid"] is True and out["run_id"] == "20261008-1900"
    assert out["targets"] == {"CORE": 0.87, "SGOV": 0.0, "NVDA": 0.13}
    nv = out["sleeve"]["NVDA"]
    assert nv["votes"] == 2 and nv["action"] == "ENTER" and nv["rule"] == "MEDIAN"
    assert nv["weights"] == [0.13, 0.15, 0.0]
    assert {"type": "PRICE_BELOW", "value": 205.0} in nv["kill_conditions"]      # median decider = D1
    assert nv["horizon_weeks"] == 8 and nv["evidence_ids"] == ["E1", "E2"]
    assert out["sgov"]["weight"] == 0.0 and out["sgov"]["votes"] == 0
    assert out["paper_targets"][D3] == {"CORE": 1.0}
    assert out["paper_targets"][D1] == {"CORE": 0.87, "NVDA": 0.13}
    assert out["drops"] == [] and out["no_majority"] == []
    vt = {v["decider"]: v for v in out["vote_table"]}
    assert vt[D3]["unchanged"] is True and vt[D1]["unchanged"] is False
    assert out["unchanged"] is False and out["unchanged_weeks"] == 0
    assert out["p_qqq_up_20d_median"] == 0.6 and out["forced_sells"] == {}


# ---------------------------------------------------------------- 14
def test_scenario_b_split_vote_is_core_with_no_majority(tmp_path):
    state = tmp_path / "state"
    rd = build_run(state, "20261008-1900")
    copy_deciders(rd, "mock_run_split")
    r = run_cli(state, "20261008-1900")
    assert r.returncode == 0, r.stderr
    out = load_out(rd)
    assert nonzero(out["targets"]) == {"CORE": 1.0}
    assert sorted(n["ticker"] for n in out["no_majority"]) == ["AMZN", "NVDA", "PLTR"]
    assert all(n["votes"] == 1 and len(n["proposers"]) == 1 for n in out["no_majority"])
    assert out["unchanged"] is True and out["unchanged_weeks"] == 1 and out["noop_flag"] is False

    # the no-op counter carries over from the digest and flags at NOOP_FLAG_WEEKS
    rd2 = build_run(state, "20261115-0830", digest=first_run_digest(unchanged_weeks=C.NOOP_FLAG_WEEKS - 1))
    copy_deciders(rd2, "mock_run_split")
    assert run_cli(state, "20261115-0830").returncode == 0
    out2 = load_out(rd2)
    assert out2["unchanged_weeks"] == C.NOOP_FLAG_WEEKS and out2["noop_flag"] is True


# ---------------------------------------------------------------- 15
def test_bad_code_and_non_enum_dropped_to_core(tmp_path):
    rd = build_run(tmp_path / "state", "R15")
    docs = three(dec(D1, {"NVDA": 0.13}, [pos("NVDA", code="OVERBOUGHT")]),
                 dec(D2, {"TSLA": 0.13}, [pos("TSLA")]),
                 dec(D3, {"NVDA": 0.15}, [pos("NVDA")]))
    out, _ = agg(rd, docs)
    assert any(d["decider"] == D1 and d["ticker"] == "NVDA" and d["rule"] == "BAD_CODE" for d in out["drops"])
    assert any(d["decider"] == D2 and d["ticker"] == "TSLA" and d["rule"] == "NON_MENU_TICKER" for d in out["drops"])
    assert out["paper_targets"][D1] == {"CORE": 1.0}
    assert out["paper_targets"][D2] == {"CORE": 1.0}
    assert out["sleeve"]["NVDA"]["votes"] == 1
    assert nonzero(out["targets"]) == {"CORE": 1.0}
    assert out["no_majority"] == [{"ticker": "NVDA", "votes": 1, "proposers": [D3]}]
    # an EXIT-only enum code on an entry is equally a BAD_CODE
    clean, drops, _ = A.validate_decider(dec(D1, {"NVDA": 0.13}, [pos("NVDA", code="CAP_DRIFT")]),
                                         A.build_ctx(str(rd)), D1)
    assert clean["paper_targets"] == {"CORE": 1.0} and drops[0]["rule"] == "BAD_CODE"
    # BANNED tickers (leveraged etc.) never survive
    clean, drops, _ = A.validate_decider(dec(D1, {"TQQQ": 0.2}, [pos("TQQQ")]), A.build_ctx(str(rd)), D1)
    assert clean["paper_targets"] == {"CORE": 1.0} and drops[0]["rule"] == "NON_MENU_TICKER"


# ---------------------------------------------------------------- 16
def test_evidence_and_rs_flag_requirements(tmp_path):
    state = tmp_path / "state"
    ctx5 = A.build_ctx(str(build_run(state, "R16a", rs={"NVDA": 5, "XLK": 1})))
    ctx2 = A.build_ctx(str(build_run(state, "R16b", rs={"NVDA": 2})))

    _, drops, _ = A.validate_decider(dec(D1, {"NVDA": 0.13}, [pos("NVDA", "GUIDANCE_RAISE", ev=())]), ctx5, D1)
    assert [d["rule"] for d in drops] == ["NO_EVIDENCE"]
    # an E-ID about another ticker does not count
    _, drops, _ = A.validate_decider(dec(D1, {"NVDA": 0.13}, [pos("NVDA", "GUIDANCE_RAISE", ev=("E4",))]), ctx5, D1)
    assert [d["rule"] for d in drops] == ["NO_EVIDENCE"]
    # an E-ID that is not in evidence.json does not count
    _, drops, _ = A.validate_decider(dec(D1, {"NVDA": 0.13}, [pos("NVDA", "GUIDANCE_RAISE", ev=("E99",))]), ctx5, D1)
    assert [d["rule"] for d in drops] == ["NO_EVIDENCE"]

    clean, drops, _ = A.validate_decider(dec(D2, {"NVDA": 0.13}, [pos("NVDA", "RELATIVE_STRENGTH", ev=())]), ctx5, D2)
    assert [d["rule"] for d in drops] == ["NO_RS_FLAG"] and clean["paper_targets"] == {"CORE": 1.0}
    clean, drops, _ = A.validate_decider(dec(D2, {"NVDA": 0.13}, [pos("NVDA", "RELATIVE_STRENGTH", ev=())]), ctx2, D2)
    assert drops == [] and clean["paper_targets"] == {"CORE": 0.87, "NVDA": 0.13}

    # sector ETF: a MACRO/NDX item supports it
    clean, drops, _ = A.validate_decider(dec(D3, {"XLK": 0.12}, [pos("XLK", "GUIDANCE_RAISE", ev=("E6",))]), ctx5, D3)
    assert drops == [] and clean["sleeve"] == {"XLK": 0.12}

    # ENTER needs a valid kill; PRICE_BELOW above the price is invalid
    clean, drops, _ = A.validate_decider(
        dec(D1, {"NVDA": 0.13}, [pos("NVDA", kills=[{"type": "PRICE_BELOW", "value": 250.0}])]), ctx2, D1)
    assert {d["rule"] for d in drops} == {"BAD_KILL"} and clean["paper_targets"] == {"CORE": 1.0}
    # HOLD on a name that is NOT held is checked as an entry (no evidence loophole)
    _, drops, _ = A.validate_decider(dec(D1, {"NVDA": 0.13}, [pos("NVDA", ev=(), action="HOLD")]), ctx5, D1)
    assert [d["rule"] for d in drops] == ["NO_EVIDENCE"]
    # clamps: per-name cap, horizon, p, sum renorm
    doc = dec(D1, {"NVDA": 0.36}, [pos("NVDA", horizon=40)], p=1.4)
    doc["target_weights"]["CORE"] = 0.84                         # sums to 1.2 -> NVDA 0.30 -> cap 0.25
    clean, drops, clamps = A.validate_decider(doc, ctx2, D1)
    got = {c["rule"] for c in clamps}
    assert {"SUM_RENORM", "HORIZON", "P_RANGE"} <= got
    assert clean["positions"]["NVDA"]["horizon_weeks"] == C.HORIZON_WEEKS_MAX
    assert clean["p_qqq_up_20d"] == 1.0
    assert clean["sleeve"]["NVDA"] == C.SLEEVE_POSITION_MAX_PCT and "CAP_POSITION" in got
    # decider_id mismatch is logged but still processed
    clean, drops, _ = A.validate_decider(dec("D9_WRONG", {"NVDA": 0.13}, [pos("NVDA")]), ctx2, D1)
    assert drops[0]["rule"] == "ID_MISMATCH" and clean["sleeve"] == {"NVDA": 0.13}


# ---------------------------------------------------------------- 17
def test_sgov_rules_by_regime_and_caps(tmp_path):
    state = tmp_path / "state"
    rd_up = build_run(state, "R17up")
    shock = dict(dcode="INDEX_EARNINGS_SHOCK", devids=["E3"])
    out, _ = agg(rd_up, three(dec(D1, sgov=0.2, **shock), dec(D2, sgov=0.2, **shock), dec(D3, sgov=0.1, **shock)))
    assert out["sgov"]["votes"] == 3 and out["sgov"]["rule"] == "MIN_UP" and out["sgov"]["weight"] == 0.1
    assert out["targets"]["SGOV"] == 0.1 and out["targets"]["CORE"] == 0.9 and out["sgov"]["renewed"] is True

    out, _ = agg(rd_up, three(dec(D1, sgov=0.2, **shock), dec(D2, sgov=0.2, **shock), dec(D3)))
    assert out["sgov"]["votes"] == 2 and out["targets"]["SGOV"] == 0.0 and out["sgov"]["rule"] == "NONE"

    # cap: UP cap is SGOV_MAX_UP
    out, _ = agg(rd_up, three(*(dec(d, sgov=0.5, **shock) for d in C.DECIDER_IDS)))
    assert out["targets"]["SGOV"] == C.SGOV_MAX_UP
    assert len([c for c in out["clamps"] if c["rule"] == "CAP_SGOV"]) == 3

    # DOWN: 2/3 and median; TREND_BREAK justified
    rd_dn = build_run(state, "R17dn", regime="DOWN")
    tb = dict(dcode="TREND_BREAK")
    out, _ = agg(rd_dn, three(dec(D1, sgov=0.3, **tb), dec(D2, sgov=0.2, **tb), dec(D3)))
    assert out["sgov"]["votes"] == 2 and out["sgov"]["rule"] == "MEDIAN_DOWN" and out["targets"]["SGOV"] == 0.2
    out, _ = agg(rd_dn, three(*(dec(d, sgov=0.9, **tb) for d in C.DECIDER_IDS)))
    assert out["targets"]["SGOV"] == C.SGOV_MAX_DOWN

    # unjustified codes / evidence
    out, _ = agg(rd_up, three(dec(D1, sgov=0.2, dcode="CREDIT_EVENT", devids=["E6"]),   # E6 not fetch-verified
                              dec(D2, sgov=0.2, dcode="VIX_SPIKE"), dec(D3, sgov=0.2, dcode="INDEX_EARNINGS_SHOCK",
                                                                        devids=["E1"])))   # E1 is a + guidance
    assert len(rules(out, "DERISK_UNJUSTIFIED")) == 3 and out["targets"]["SGOV"] == 0.0

    # scenario C fixtures: TREND_BREAK in UP is dropped -> 2 votes < 3 -> no SGOV
    rd_c = build_run(state, "20261008-2000")
    copy_deciders(rd_c, "mock_run_derisk")
    assert run_cli(state, "20261008-2000").returncode == 0
    out = load_out(rd_c)
    dr = rules(out, "DERISK_UNJUSTIFIED")
    assert len(dr) == 1 and dr[0]["decider"] == D2 and dr[0]["ticker"] == "SGOV"
    assert out["sgov"]["votes"] == 2 and out["targets"] == {"CORE": 1.0, "SGOV": 0.0}
    # same fixtures in DOWN: all three justified -> median 0.2
    rd_c2 = build_run(state, "20261008-2001", regime="DOWN")
    copy_deciders(rd_c2, "mock_run_derisk")
    assert run_cli(state, "20261008-2001").returncode == 0
    out = load_out(rd_c2)
    assert out["sgov"]["votes"] == 3 and out["targets"]["SGOV"] == 0.2 and rules(out, "DERISK_UNJUSTIFIED") == []

    # T2: no SGOV authority
    rd_t2 = build_run(state, "R17t2", caps={"authority": "T2_SUSPENDED", "sleeve_max_pct": 0.0,
                                             "sleeve_position_max_pct": C.SLEEVE_POSITION_MAX_PCT,
                                             "sleeve_max_positions": C.SLEEVE_MAX_POSITIONS,
                                             "sgov_max_pct": 0.0, "sgov_authority": False})
    out, _ = agg(rd_t2, three(*(dec(d, sgov=0.2, **shock) for d in C.DECIDER_IDS)))
    assert out["targets"]["SGOV"] == 0.0


# ---------------------------------------------------------------- 18
def test_sgov_incumbent_and_expiry(tmp_path):
    state = tmp_path / "state"
    held = {"QQQ": 2, "QQQM": 1, "SGOV": 2}
    shock = dict(dcode="INDEX_EARNINGS_SHOCK", devids=["E3"])
    docs = three(dec(D1, sgov=0.1, **shock), dec(D2, sgov=0.1, **shock), dec(D3))       # 2 < 3 in UP

    dg = first_run_digest(first_run=False, sgov={"held": True, "weight": 0.095, "runs_held": 2,
                                                 "expires_after_runs_held": C.SGOV_EXPIRY_RUNS})
    rd = build_run(state, "R18a", positions=held, digest=dg)
    out, ctx = agg(rd, docs)
    assert out["sgov"]["rule"] == "INCUMBENT" and out["sgov"]["renewed"] is False
    assert A.target_shares(ctx, "SGOV", out["targets"]["SGOV"]) == 2            # keeps exactly the held shares
    assert out["exits"] == [] and out["unchanged"] is True

    dg["sgov"]["runs_held"] = C.SGOV_EXPIRY_RUNS
    rd = build_run(state, "R18b", positions=held, digest=dg)
    out, _ = agg(rd, docs)
    assert out["targets"]["SGOV"] == 0.0
    assert {"ticker": "SGOV", "why": "SGOV_EXPIRED"} in out["exits"]

    # a renewing 3/3 vote keeps it regardless of runs_held
    out, _ = agg(rd, three(*(dec(d, sgov=0.1, **shock) for d in C.DECIDER_IDS)))
    assert out["sgov"]["renewed"] is True and out["targets"]["SGOV"] == 0.1 and out["exits"] == []


# ---------------------------------------------------------------- 19
def _held_nvda_digest(min_hold_met=False, kill_status="OK", kill_fired=False, events=None):
    th = {"ticker": "NVDA", "entered_date": "2026-10-03", "entered_run_id": "20261003-0830", "entry_price": 230.0,
          "current_price": 237.47, "horizon_end_date": "2026-11-28", "horizon_reached": False,
          "reason_code": "GUIDANCE_RAISE", "evidence_ids": ["E1"], "thesis_30w": "held thesis", "horizon_weeks": 8,
          "kill_conditions": [{"type": "PRICE_BELOW", "value": 230.0, "status": kill_status, "checked_value": 237.47}],
          "sessions_held": 25 if min_hold_met else 4, "min_hold_met": min_hold_met, "current_weight": 0.125,
          "kill_fired": kill_fired}
    wt = {"schema": "triggers/v6", "events": events or [], "consumed_by_run": None} if events else None
    return first_run_digest(first_run=False, open_theses=[th], watchdog_triggers=wt)


def test_exit_votes_min_hold_and_kill_override(tmp_path):
    state = tmp_path / "state"
    held = {"QQQ": 2, "NVDA": 1}
    cash = 143.06
    hold = lambda kills=None: pos("NVDA", action="HOLD", ev=(),  # noqa: E731
                                  kills=kills if kills is not None else [{"type": "PRICE_BELOW", "value": 200.0}])

    rd = build_run(state, "R19a", positions=held, cash=cash, digest=_held_nvda_digest(min_hold_met=False))
    out, ctx = agg(rd, three(dec(D1, positions=[ex("NVDA")]), dec(D2, positions=[ex("NVDA")]),
                             dec(D3, {"NVDA": 0.13}, [hold()])))
    nv = out["sleeve"]["NVDA"]
    assert nv["action"] == "HOLD" and nv["rule"] == "INCUMBENT" and nv["exit_votes"] == 2
    assert A.target_shares(ctx, "NVDA", out["targets"]["NVDA"]) == 1 and out["exits"] == []
    assert nv["kill_conditions"] == [{"type": "PRICE_BELOW", "value": 230.0}]      # incumbent keeps its thesis

    out, _ = agg(rd, three(*(dec(d, positions=[ex("NVDA")]) for d in C.DECIDER_IDS)))
    assert out["sleeve"]["NVDA"]["action"] == "EXIT" and {"ticker": "NVDA", "why": "VOTES_3_3"} in out["exits"]
    assert "NVDA" not in out["targets"] and out["forced_sells"] == {}

    rd = build_run(state, "R19b", positions=held, cash=cash, digest=_held_nvda_digest(min_hold_met=True))
    out, _ = agg(rd, three(dec(D1, positions=[ex("NVDA")]), dec(D2, positions=[ex("NVDA")]),
                           dec(D3, {"NVDA": 0.13}, [hold()])))
    assert {"ticker": "NVDA", "why": "VOTES_2_3"} in out["exits"]

    # a held name simply omitted by a decider is not an exit vote (ACTION_WEIGHT_MISMATCH -> current)
    out, ctx = agg(rd, three(dec(D1), dec(D2), dec(D3)))
    assert out["sleeve"]["NVDA"]["action"] == "HOLD" and out["exits"] == []
    assert len(rules(out, "ACTION_WEIGHT_MISMATCH")) == 3 and out["unchanged"] is True

    # kill fired: 3/3 HOLD with a NEW kill -> override; 2/3 -> KILL_FIRED forced sell
    rd = build_run(state, "R19c", positions=held, cash=cash,
                   digest=_held_nvda_digest(kill_status="FIRED", kill_fired=True))
    out, ctx = agg(rd, three(*(dec(d, {"NVDA": 0.13}, [hold()]) for d in C.DECIDER_IDS)))
    nv = out["sleeve"]["NVDA"]
    assert nv["kill_override"] is True and nv["action"] == "HOLD"
    assert A.target_shares(ctx, "NVDA", out["targets"]["NVDA"]) == 1 and out["forced_sells"] == {}

    out, _ = agg(rd, three(dec(D1, {"NVDA": 0.13}, [hold()]), dec(D2, {"NVDA": 0.13}, [hold()]),
                           dec(D3, positions=[ex("NVDA")])))
    assert {"ticker": "NVDA", "why": "KILL_FIRED"} in out["exits"] and out["forced_sells"] == {"NVDA": 1}
    assert "NVDA" not in out["targets"]

    # 3/3 HOLD but one only re-states the fired kill -> no override
    out, _ = agg(rd, three(dec(D1, {"NVDA": 0.13}, [hold()]), dec(D2, {"NVDA": 0.13}, [hold()]),
                           dec(D3, {"NVDA": 0.13}, [hold([{"type": "PRICE_BELOW", "value": 230.0}])])))
    assert out["forced_sells"] == {"NVDA": 1} and out["sleeve"]["NVDA"]["kill_override"] is False

    # kill reported by the watchdog file (thesis itself still OK)
    ev = [{"kind": "KILL_PRICE", "ticker": "NVDA", "severity": "HIGH", "detail": {"close": 203.1, "value": 230.0}}]
    rd = build_run(state, "R19d", positions=held, cash=cash, digest=_held_nvda_digest(events=ev))
    out, _ = agg(rd, three(dec(D1, {"NVDA": 0.13}, [hold()]), dec(D2, {"NVDA": 0.13}, [hold()]),
                           dec(D3, positions=[ex("NVDA")])))
    assert out["forced_sells"] == {"NVDA": 1}

    # horizon reached without a 2/3 renewal -> EXIT HORIZON
    dg = _held_nvda_digest(min_hold_met=True)
    dg["open_theses"][0]["horizon_end_date"] = "2026-10-02"
    rd = build_run(state, "R19e", positions=held, cash=cash, digest=dg)
    out, _ = agg(rd, three(dec(D1, {"NVDA": 0.13}, [hold()]), dec(D2), dec(D3, positions=[ex("NVDA")])))
    assert {"ticker": "NVDA", "why": "HORIZON"} in out["exits"]


# ---------------------------------------------------------------- 20
def _four(d, held_soxq=False):
    s = [pos("AMZN", "CONTRACT_PRODUCT", ("E4",)), pos("PLTR", "CONTRACT_PRODUCT", ("E5",)),
         pos("XLK", "GUIDANCE_RAISE", ("E6",)),
         pos("SOXQ", "GUIDANCE_RAISE", ("E6",), action="HOLD" if held_soxq else "ENTER")]
    return dec(d, {"AMZN": 0.15, "PLTR": 0.12, "XLK": 0.11, "SOXQ": 0.10}, s)


def test_caps_positions_total_trim_and_event_scope(tmp_path):
    state = tmp_path / "state"
    # 4 names voted 3/3 -> SLEEVE_MAX_POSITIONS kept by median
    rd = build_run(state, "R20a")
    out, _ = agg(rd, three(*(_four(d) for d in C.DECIDER_IDS)))
    kept = {t for t in out["targets"] if t not in ("CORE", "SGOV")}
    assert len(kept) == C.SLEEVE_MAX_POSITIONS == 3 and kept == {"AMZN", "PLTR", "XLK"}
    assert [c["ticker"] for c in rules(out, "CAP_POSITIONS")] == ["SOXQ"]
    # held first: a held SOXQ survives, the weakest new name goes
    rd = build_run(state, "R20b", positions={"QQQ": 2, "SOXQ": 2}, cash=174.53)
    out, ctx = agg(rd, three(*(_four(d, held_soxq=True) for d in C.DECIDER_IDS)))
    kept = {t for t in out["targets"] if t not in ("CORE", "SGOV")}
    assert kept == {"SOXQ", "AMZN", "PLTR"}
    assert A.target_shares(ctx, "SOXQ", out["targets"]["SOXQ"]) == 2

    # sleeve total: medians 0.2 x3 = 0.6 -> scaled to caps.sleeve_max_pct
    rd = build_run(state, "R20c")
    a, p, x = (pos("AMZN", "CONTRACT_PRODUCT", ("E4",)), pos("PLTR", "CONTRACT_PRODUCT", ("E5",)),
               pos("XLK", "GUIDANCE_RAISE", ("E6",)))
    out, ctx = agg(rd, three(dec(D1, {"AMZN": 0.2, "PLTR": 0.2}, [a, p]), dec(D2, {"AMZN": 0.2, "XLK": 0.2}, [a, x]),
                             dec(D3, {"PLTR": 0.2, "XLK": 0.2}, [p, x])))
    sl = {t: w for t, w in out["targets"].items() if t not in ("CORE", "SGOV")}
    assert set(sl) == {"AMZN", "PLTR", "XLK"}
    assert abs(sum(sl.values()) - ctx["caps"]["sleeve_max_pct"]) < 1e-5
    assert len(rules(out, "CAP_SLEEVE_TOTAL")) == 3 and abs(out["targets"]["CORE"] - 0.5) < 1e-5
    # T1 reduced cap is honoured from caps_in_effect, not config
    rd = build_run(state, "R20d", caps={"authority": "T1_REDUCED", "sleeve_max_pct": C.T1_SLEEVE_CUT,
                                        "sleeve_position_max_pct": C.SLEEVE_POSITION_MAX_PCT,
                                        "sleeve_max_positions": C.SLEEVE_MAX_POSITIONS,
                                        "sgov_max_pct": C.SGOV_MAX_UP, "sgov_authority": True})
    out, _ = agg(rd, three(*(dec(d, {"AMZN": 0.2}, [a]) for d in C.DECIDER_IDS)))
    assert out["targets"]["AMZN"] == C.T1_SLEEVE_CUT

    # trim: held NVDA at ~37% > SLEEVE_TRIM_ABOVE_PCT -> back to SLEEVE_POSITION_MAX_PCT
    rd = build_run(state, "R20e", positions={"QQQ": 1, "NVDA": 3}, cash=455.86)
    hold = pos("NVDA", action="HOLD", ev=())
    out, _ = agg(rd, three(*(dec(d, {"NVDA": 0.37}, [hold]) for d in C.DECIDER_IDS)))
    nv = out["sleeve"]["NVDA"]
    assert nv["current_weight"] > C.SLEEVE_TRIM_ABOVE_PCT
    assert nv["action"] == "TRIM" and nv["why"] == "CAP_DRIFT"
    assert out["targets"]["NVDA"] == C.SLEEVE_POSITION_MAX_PCT

    # event scope NVDA: an AMZN entry is reverted, NVDA may change
    rd = build_run(state, "R20f")
    docs = three(*(dec(d, {"AMZN": 0.15, "NVDA": 0.13}, [pos("AMZN", "CONTRACT_PRODUCT", ("E4",)), pos("NVDA")])
                   for d in C.DECIDER_IDS))
    out, _ = agg(rd, docs, scope=["NVDA"])
    oos = rules(out, "OUT_OF_SCOPE")
    assert len(oos) == 3 and {d["ticker"] for d in oos} == {"AMZN"}
    assert "AMZN" not in out["targets"] and out["targets"]["NVDA"] == 0.13 and out["event_scope"] == ["NVDA"]
    # same through the CLI flag
    for d, doc in docs.items():
        (rd / "deciders" / f"{d}.json").write_text(json.dumps(doc))
    r = run_cli(state, "R20f", "--event-scope", "NVDA")
    assert r.returncode == 0, r.stderr
    assert "AMZN" not in load_out(rd)["targets"]


# ---------------------------------------------------------------- independent-review regressions
def test_review_regressions_event_scope_hold_adds_and_validity(tmp_path):
    state = tmp_path / "state"
    # SGOV out of an event scope: frozen, never "renewed" by reverted lines, never expired mid-week
    dg = first_run_digest(first_run=False, unchanged_weeks=5,
                          sgov={"held": True, "weight": 0.19, "runs_held": 5, "expires_after_runs_held": 4})
    rd = build_run(state, "R21a", positions={"QQQ": 2, "QQQM": 1, "SGOV": 4}, digest=dg)
    out, ctx = agg(rd, three(dec(D1), dec(D2), dec(D3)), scope=["NVDA"])
    assert out["sgov"]["rule"] == "OUT_OF_SCOPE" and out["sgov"]["renewed"] is False
    assert out["sgov"]["votes"] == 0 and out["exits"] == []
    assert A.target_shares(ctx, "SGOV", out["targets"]["SGOV"]) == 4
    assert out["unchanged_weeks"] == 5                          # event run does not advance the no-op counter
    out, _ = agg(rd, three(dec(D1), dec(D2), dec(D3)))          # weekly run, same inputs -> expires
    assert {"ticker": "SGOV", "why": "SGOV_EXPIRED"} in out["exits"]

    # held NVDA with horizon passed is untouched by an AMZN-scoped event run
    dg = _held_nvda_digest(min_hold_met=True)
    dg["open_theses"][0]["horizon_end_date"] = "2026-10-02"
    rd = build_run(state, "R21b", positions={"QQQ": 2, "NVDA": 1}, cash=143.06, digest=dg)
    out, ctx = agg(rd, three(dec(D1), dec(D2), dec(D3)), scope=["AMZN"])
    assert out["exits"] == [] and A.target_shares(ctx, "NVDA", out["targets"]["NVDA"]) == 1
    assert out["sleeve"]["NVDA"]["rule"] == "OUT_OF_SCOPE"

    # HOLD that ADDS shares is an entry: needs evidence / RS flag / kill
    rd = build_run(state, "R21c", positions={"QQQ": 2, "NVDA": 1}, cash=143.06)
    add = pos("NVDA", "RELATIVE_STRENGTH", ev=(), action="HOLD")
    out, ctx = agg(rd, three(dec(D1, {"NVDA": 0.25}, [add]), dec(D2, {"NVDA": 0.25}, [add]), dec(D3)))
    assert len(rules(out, "NO_RS_FLAG")) == 2
    assert A.target_shares(ctx, "NVDA", out["targets"]["NVDA"]) == 1     # stays at the held share

    # >= 2 invalid decider files -> valid=false, written, exit 3 (solver/ledger refuse it)
    rd = build_run(state, "20261008-2100")
    copy_deciders(rd)
    for d in (D1, D2):
        (rd / "deciders" / f"{d}.json").write_text("")
    r = run_cli(state, "20261008-2100")
    assert r.returncode == 3
    out = load_out(rd)
    assert out["valid"] is False and out["invalid_reasons"]
    # evidence.json without items is not a clerk bundle
    rd = build_run(state, "20261008-2101")
    copy_deciders(rd)
    (rd / "evidence.json").write_text("{}")
    assert run_cli(state, "20261008-2101").returncode == 3 and load_out(rd)["valid"] is False


# ---------------------------------------------------------------- integration review 1 fixes
def test_held_filler_is_not_a_sleeve_position(tmp_path):
    """Review issue A: a held SPYM filler (book.py tags it FILLER, not in held_sleeve) is the solver's
    free variable -- core-only votes produce no drops and SPYM never enters targets/sleeve."""
    import helpers
    import book as B
    state = tmp_path / "state"
    rd = Path(helpers.make_run(str(state), "20261010-0830", holdings_text="QQQ 2, SPYM 2, cash 100"))
    bk = json.loads((rd / "book.json").read_text())
    assert bk["positions"]["SPYM"]["sleeve"] == "FILLER" and "SPYM" not in bk["held_sleeve"]   # book.py contract
    shutil.copy(FIX / "mock_run" / "evidence.json", rd / "evidence.json")
    out, ctx = agg(rd, three(dec(D1), dec(D2), dec(D3)))
    assert out["drops"] == [] and out["exits"] == [] and out["forced_sells"] == {}
    assert "SPYM" not in out["targets"] and "SPYM" not in out["sleeve"]
    assert "SPYM" not in ctx["held_sleeve"] and "SPYM" not in ctx["held_shares"]
    assert nonzero(out["targets"]) == {"CORE": 1.0} and out["unchanged"] is True
    # the solver keeps the filler (no sell/rebuy churn)
    (rd / "aggregate_out.json").write_text(json.dumps(out))
    assert B.solve_run(str(rd))["orders"] == []
    # the in-test book builder mirrors the tag; a filler WITH an open thesis is a real sleeve name
    rd2 = build_run(state, "R_FILL", positions={"QQQ": 2, "SPYM": 2}, cash=100.0)
    out2, _ = agg(rd2, three(dec(D1), dec(D2), dec(D3)))
    assert out2["drops"] == [] and "SPYM" not in out2["sleeve"]
    th = {"ticker": "SPYM", "kill_conditions": [], "min_hold_met": True}
    rd3 = build_run(state, "R_FILL_TH", positions={"QQQ": 2, "SPYM": 2}, cash=100.0,
                    digest=first_run_digest(first_run=False, open_theses=[th]))
    out3, _ = agg(rd3, three(dec(D1), dec(D2), dec(D3)))
    assert out3["sleeve"]["SPYM"]["held"] is True and out3["sleeve"]["SPYM"]["action"] == "HOLD"


def _event_kill_digest(kill_status="OK", kill_fired=False):
    dg = _held_nvda_digest(min_hold_met=False, kill_status=kill_status, kill_fired=kill_fired)
    dg["open_theses"][0]["kill_conditions"] = [
        {"type": "PRICE_BELOW", "value": 200.0, "status": "OK", "checked_value": 237.47},
        {"type": "EVENT", "value": "US enacts an NVDA China data-center GPU export ban", "status": "OPEN"},
        {"type": "DATE", "value": "2026-11-28", "status": "OK"}]
    return dg


def test_clerk_tagged_event_kill_fires(tmp_path):
    """Review issue B: an evidence item thesis_tags {ticker, kill_type EVENT, fires true} is a fired kill."""
    state = tmp_path / "state"
    held, cash = {"QQQ": 2, "NVDA": 1}, 143.06
    hold = lambda kills: pos("NVDA", action="HOLD", ev=(), kills=kills)  # noqa: E731
    new_kill = [{"type": "PRICE_BELOW", "value": 190.0}]
    rd = build_run(state, "R_EV1", positions=held, cash=cash, digest=_event_kill_digest())
    shutil.copy(FIX / "mock_run_event_kill" / "evidence.json", rd / "evidence.json")

    ctx = A.build_ctx(str(rd))
    th = A._thesis_map(ctx["digest"])["NVDA"]
    fired, keys = A._kill_state("NVDA", th, ctx["digest"], ctx["evidence_ids"])
    assert fired is True and ("EVENT", "US enacts an NVDA China data-center GPU export ban") in keys
    assert A._kill_state("NVDA", th, ctx["digest"])[0] is False          # without the evidence: not fired

    # 2/3 HOLD cannot override -> forced sell inside the min-hold window
    out, _ = agg(rd, three(dec(D1, {"NVDA": 0.13}, [hold(new_kill)]), dec(D2, {"NVDA": 0.13}, [hold(new_kill)]),
                           dec(D3, positions=[ex("NVDA")])))
    assert {"ticker": "NVDA", "why": "KILL_FIRED"} in out["exits"] and out["forced_sells"] == {"NVDA": 1}
    assert "NVDA" not in out["targets"] and out["unchanged"] is False
    # 3/3 HOLD with a genuinely new kill -> override
    out, _ = agg(rd, three(*(dec(d, {"NVDA": 0.13}, [hold(new_kill)]) for d in C.DECIDER_IDS)))
    assert out["sleeve"]["NVDA"]["kill_override"] is True and out["forced_sells"] == {}

    # non-firing tag (E1) and a firing tag on another ticker (E4, AMZN) do not fire NVDA
    rd2 = build_run(state, "R_EV2", positions=held, cash=cash, digest=_event_kill_digest())
    ev = json.loads((FIX / "mock_run_event_kill" / "evidence.json").read_text())
    ev["items"] = [it for it in ev["items"] if it["id"] != "E7"]
    (rd2 / "evidence.json").write_text(json.dumps(ev))
    out, _ = agg(rd2, three(dec(D1, {"NVDA": 0.13}, [hold(new_kill)]), dec(D2, {"NVDA": 0.13}, [hold(new_kill)]),
                            dec(D3, positions=[ex("NVDA")])))
    assert out["forced_sells"] == {} and out["exits"] == [] and out["sleeve"]["NVDA"]["action"] == "HOLD"


def test_kill_override_new_kill_must_differ_from_all_thesis_kills(tmp_path):
    """Review issue E: re-stating the thesis' other (unfired) kills is not a NEW kill."""
    state = tmp_path / "state"
    rd = build_run(state, "R_NK", positions={"QQQ": 2, "NVDA": 1}, cash=143.06,
                   digest=_event_kill_digest(kill_status="FIRED", kill_fired=True))
    restate = [{"type": "DATE", "value": "2026-11-28"}, {"type": "PRICE_BELOW", "value": 200.0}]
    hold = lambda kills: pos("NVDA", action="HOLD", ev=(), kills=kills)  # noqa: E731
    out, _ = agg(rd, three(dec(D1, {"NVDA": 0.13}, [hold([{"type": "PRICE_BELOW", "value": 190.0}])]),
                           dec(D2, {"NVDA": 0.13}, [hold([{"type": "PRICE_BELOW", "value": 190.0}])]),
                           dec(D3, {"NVDA": 0.13}, [hold(restate)])))
    assert out["sleeve"]["NVDA"]["kill_override"] is False and out["forced_sells"] == {"NVDA": 1}
    out, _ = agg(rd, three(*(dec(d, {"NVDA": 0.13}, [hold(restate + [{"type": "DATE", "value": "2026-12-19"}])])
                             for d in C.DECIDER_IDS)))
    assert out["sleeve"]["NVDA"]["kill_override"] is True and out["forced_sells"] == {}


def test_exit_thesis_broken_after_fired_kill_needs_no_eid(tmp_path):
    """Review issue C: EXIT THESIS_BROKEN on a held name whose kill fired is not dropped NO_EVIDENCE."""
    state = tmp_path / "state"
    held, cash = {"QQQ": 2, "NVDA": 1}, 143.06
    rd = build_run(state, "R_TB1", positions=held, cash=cash,
                   digest=_held_nvda_digest(kill_status="FIRED", kill_fired=True))
    out, _ = agg(rd, three(*(dec(d, positions=[ex("NVDA", "THESIS_BROKEN")]) for d in C.DECIDER_IDS)))
    assert rules(out, "NO_EVIDENCE") == []
    assert out["sleeve"]["NVDA"]["action"] == "EXIT" and out["sleeve"]["NVDA"]["exit_votes"] == 3
    # kill not fired -> the E-ID is still required (reverted, abstains)
    rd = build_run(state, "R_TB2", positions=held, cash=cash, digest=_held_nvda_digest())
    out, _ = agg(rd, three(*(dec(d, positions=[ex("NVDA", "THESIS_BROKEN")]) for d in C.DECIDER_IDS)))
    assert len(rules(out, "NO_EVIDENCE")) == 3 and out["sleeve"]["NVDA"]["action"] == "HOLD"


def _post_sleeve(res):
    return sum(w for t, w in res["post_trade"]["weights"].items() if t not in ("CORE", "SGOV", "CASH"))


def test_sleeve_total_drift_is_force_trimmed(tmp_path):
    """Review ruling 3: a 63% held sleeve with every name under the trim line must be cut to the cap
    even though the solver's held-name rounding keeps every share of the scaled targets."""
    import helpers
    import book as B
    state = tmp_path / "state"
    rd = Path(helpers.make_run(str(state), "20261010-0830", holdings_text="QQQ 1, NVDA 2, AMZN 2, PLTR 2, cash 50"))
    shutil.copy(FIX / "mock_run" / "evidence.json", rd / "evidence.json")
    ctx = A.build_ctx(str(rd))
    cap = ctx["caps"]["sleeve_max_pct"]
    held_w = sum(A.current_weight(ctx, t) for t in ("NVDA", "AMZN", "PLTR"))
    assert held_w > cap + 0.1 and all(A.current_weight(ctx, t) < C.SLEEVE_TRIM_ABOVE_PCT for t in ("NVDA", "AMZN", "PLTR"))

    hold = [pos(t, action="HOLD", ev=()) for t in ("NVDA", "AMZN", "PLTR")]
    cw = {t: round(A.current_weight(ctx, t), 4) for t in ("NVDA", "AMZN", "PLTR")}
    for label, docs in (("silent", three(dec(D1), dec(D2), dec(D3))),
                        ("hold", three(*(dec(d, dict(cw), hold) for d in C.DECIDER_IDS)))):
        out = A.aggregate(docs, ctx)
        assert out["forced_sells"], label
        assert out["unchanged"] is False, label
        # held-name rounding alone would have kept every share (the original defect)
        assert all(A.solver_shares(ctx, t, out["targets"][t]) == 2 for t in out["forced_sells"]), label
        assert all(out["sleeve"][t]["why"] == "CAP_DRIFT" and out["sleeve"][t]["action"] in ("TRIM", "EXIT")
                   for t in out["forced_sells"]), label
        assert rules(out, "CAP_SLEEVE_DRIFT"), label
        # most overweight first: AMZN (largest current weight vs the same scale factor)
        assert "AMZN" in out["forced_sells"], label
        out["run_id"] = "20261010-0830"
        (rd / "aggregate_out.json").write_text(json.dumps(out))
        res = B.solve_run(str(rd))
        sells = {o["ticker"]: o["shares"] for o in res["orders"] if o["action"] == "SELL"}
        assert sells and all(sells.get(t) == k for t, k in out["forced_sells"].items()), (label, res["orders"])
        assert _post_sleeve(res) <= cap + 1e-4, (label, res["post_trade"])
        assert res["post_trade"]["cash_usd"] >= 0

    # under the cap -> no CAP_DRIFT forced sells
    rd2 = Path(helpers.make_run(str(state), "20261017-0830", holdings_text="QQQ 2, NVDA 1, cash 143.06"))
    shutil.copy(FIX / "mock_run" / "evidence.json", rd2 / "evidence.json")
    out = A.aggregate(three(dec(D1), dec(D2), dec(D3)), A.build_ctx(str(rd2)))
    assert out["forced_sells"] == {} and out["unchanged"] is True
    # event run: frozen names are never force-trimmed
    out = A.aggregate(three(dec(D1), dec(D2), dec(D3)), dict(ctx, event_scope=["MSFT"]))
    assert out["forced_sells"] == {}
