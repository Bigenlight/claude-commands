#!/usr/bin/env python3
"""us-stock-advisor v6.0 — SINGLE SOURCE OF TRUTH for every constant.

Install path: ~/.claude/skills/us-stock-advisor/scripts/config.py

RULE: SKILL.md carries no gating number. If a number gates a decision, it lives here
and only here. Agents never see this file; Python enforces it.

USER OVERRIDES vs v6_decision_spec.md (Theo, 2026-10-08): SLEEVE_MAX_PCT 0.35->0.50,
SLEEVE_MAX_POSITIONS 2->3, SLEEVE_POSITION_MAX_PCT 0.20->0.25, SLEEVE_TRIM_ABOVE_PCT
0.30->0.35, SGOV_MAX_UP 0.20->0.30, SGOV_MAX_DOWN 0.50->0.60. Damage caps T1/T2 as spec.
"""
from __future__ import annotations

import os
from datetime import timedelta, timezone

VERSION = "6.0.0"
KST = timezone(timedelta(hours=9), "KST")

# ---------------------------------------------------------------- paths
# SKILL_DIR is derived from this file so a copied tree never writes into the live skill.
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# US_ADVISOR_STATE redirects ALL state (tests, dry runs). Resolved once at import time.
STATE_DIR = os.environ.get("US_ADVISOR_STATE") or os.path.join(SKILL_DIR, "state")
LEDGER_JSONL = os.path.join(STATE_DIR, "ledger.jsonl")
LEDGER_LOCK = os.path.join(STATE_DIR, "ledger.lock")
RUNS_DIR = os.path.join(STATE_DIR, "run")
TRIGGERS_JSON = os.path.join(STATE_DIR, "triggers.json")
REPORTS_DIR = os.path.expanduser("~/.claude/us-stock-advisor/reports")  # outside the skills git repo
SLACK_DM_USER = "U0AD7V4SWD9"          # the only delivery destination; top-level thread only


def run_dir(run_id: str) -> str:
    """state/run/<run_id>/ — every agent output and every P4 artifact of one run."""
    return os.path.join(RUNS_DIR, run_id)


# ---------------------------------------------------------------- universe
BENCHMARK = "QQQ"
CORE_TICKERS = ("QQQ", "QQQM")        # one position; order = whole-share tie-break
DERISK_TICKER = "SGOV"
RESIDUAL_FILLER = "SPYM"              # residual cash > CASH_MAX_PCT and no QQQM lot fits
UNIVERSE = {
    "core":   ("QQQ", "QQQM"),
    "broad":  ("SPYM",),
    "sector": ("SOXQ", "XLK", "IGV", "SMH", "SOXX"),
    "name":   ("NVDA", "GOOGL", "MSFT", "META", "AMZN", "AAPL", "AVGO", "TSM", "PLTR", "AMD", "MU"),
    "derisk": ("SGOV",),
}
ALL_TICKERS = tuple(t for g in UNIVERSE.values() for t in g)
SLEEVE_ELIGIBLE = UNIVERSE["broad"] + UNIVERSE["sector"] + UNIVERSE["name"]
BANNED_NOTE = ("leveraged/inverse ETFs, options, shorts, commodities/energy, GLD: not in "
               "UNIVERSE and never added by an agent; a non-UNIVERSE ticker is dropped to core")
# Nasdaq-100 top weights for the INDEX_EARNINGS_SHOCK rule. Refresh by hand quarterly.
NDX_TOP10 = ("NVDA", "MSFT", "AAPL", "AMZN", "AVGO", "META", "GOOGL", "GOOG", "TSLA", "NFLX", "COST")


def group_of(ticker: str) -> str | None:
    for g, ts in UNIVERSE.items():
        if ticker in ts:
            return g
    return None


# ---------------------------------------------------------------- sleeves (user-overridden)
SLEEVE_MAX_PCT = 0.50                 # spec 0.35
SLEEVE_POSITION_MAX_PCT = 0.25        # spec 0.20; at entry; also the lot-feasibility bar
SLEEVE_MAX_POSITIONS = 3              # spec 2
SLEEVE_TRIM_ABOVE_PCT = 0.35          # spec 0.30; trim a held sleeve name back to POSITION_MAX
SGOV_MAX_UP = 0.30                    # spec 0.20; regime UP needs 3/3
SGOV_MAX_DOWN = 0.60                  # spec 0.50; regime DOWN needs 2/3 (equity floor 40%)
SGOV_EXPIRY_RUNS = 4                  # SGOV survives at most this many weekly runs without a renewing vote
CASH_MAX_PCT = 0.10                   # cash is a residual, never a target
MIN_HOLD_SESSIONS = 20                # EXIT needs 3/3 before, 2/3 after
MAX_ORDER_LINES = 4
PRICE_BUFFER_PCT = 0.01               # a share fits iff px*(1+this) <= budget (fees/FX/gap)
WEIGHT_SUM_TOLERANCE = 0.02           # |sum(target_weights) - 1| above this -> renormalise + log
VOTES_REQUIRED_SLEEVE = 2
VOTES_REQUIRED_SGOV_UP = 3
VOTES_REQUIRED_SGOV_DOWN = 2
VOTES_REQUIRED_EXIT_AFTER_HOLD = 2
VOTES_REQUIRED_EXIT_WITHIN_HOLD = 3
VOTES_REQUIRED_KILL_OVERRIDE = 3      # re-affirm a kill-fired name only with 3/3 + new kill
HORIZON_WEEKS_MIN = 2
HORIZON_WEEKS_MAX = 26

# ---------------------------------------------------------------- regime (input only)
REGIME_SMA_MONTHS = 10                # monthly closes, completed months only
REGIME_SMA_DAYS = 200
REGIME_UP = "UP"
REGIME_DOWN = "DOWN"
HISTORY_PERIOD = "2y"                 # >= 13 completed months for the 10-month SMA

# ---------------------------------------------------------------- research freshness
CATALYST_MAX_AGE_DAYS = 7
NEW_FLAG_HOURS = 72
BACKGROUND_MAX_AGE_DAYS = 45
CANDIDATE_MENU_MAX = 6
# Session counts (earnings_in_sessions, sessions_held) use numpy.busday_count: weekends only,
# US holidays ignored, so they can overcount by 1-2 sessions. Accepted.
EARNINGS_MENU_SESSIONS = 10           # a name with earnings inside this window enters the menu
RS_RANK_FLAG_MAX = 3                  # RELATIVE_STRENGTH / SECTOR_MOMENTUM need rs_rank <= this
EVENT_TYPES = ("GUIDANCE", "EARNINGS", "ANALYST_T1", "REGULATORY_EXPORT", "CONTRACT_PRODUCT",
               "MA", "CAPEX", "MACRO_PRINT", "POLICY_ENACTED", "SHOCK_MOVE")
MATERIALITY = ("HIGH", "MED", "LOW")
DIRECTIONS = ("+", "-", "mixed")
AGE_TAGS = ("NEW", "RECENT", "BACKGROUND")
SCOUT_IDS = ("S1_CORE", "S2_CANDIDATES", "S3_MACRO", "S4_REDFLAGS")
DECIDER_IDS = ("D1_TREND", "D2_FUNDAMENTAL", "D3_MACRO")

# ---------------------------------------------------------------- reason codes (closed enum)
ENTRY_CODES = ("EARNINGS_BEAT_RAISE", "GUIDANCE_RAISE", "ESTIMATE_REVISION_UP", "CONTRACT_PRODUCT",
               "REGULATORY_RELIEF", "RELATIVE_STRENGTH", "SECTOR_MOMENTUM")
EXIT_CODES = ("THESIS_BROKEN", "BETTER_USE", "HORIZON_REACHED", "CAP_DRIFT")
DERISK_CODES = ("TREND_BREAK", "INDEX_EARNINGS_SHOCK", "POLICY_SHOCK_ENACTED", "CREDIT_EVENT")
REASON_CODES = {"ENTER": ENTRY_CODES, "HOLD": ENTRY_CODES, "EXIT": EXIT_CODES, "DERISK": DERISK_CODES}
EVIDENCE_REQUIRED_CODES = ("EARNINGS_BEAT_RAISE", "GUIDANCE_RAISE", "ESTIMATE_REVISION_UP",
                           "CONTRACT_PRODUCT", "REGULATORY_RELIEF", "THESIS_BROKEN",
                           "INDEX_EARNINGS_SHOCK", "POLICY_SHOCK_ENACTED", "CREDIT_EVENT")
RS_FLAG_CODES = ("RELATIVE_STRENGTH", "SECTOR_MOMENTUM")
# DERISK code -> evidence predicate (every key must match one cited E-ID; entity_in -> NDX_TOP10)
DERISK_EVIDENCE_RULES = {
    "TREND_BREAK": {"regime": REGIME_DOWN},
    "INDEX_EARNINGS_SHOCK": {"event_type": "GUIDANCE", "materiality": "HIGH", "direction": "-",
                             "entity_in": "NDX_TOP10", "verified_by_fetch": True},
    "POLICY_SHOCK_ENACTED": {"event_type": "POLICY_ENACTED", "verified_by_fetch": True},
    "CREDIT_EVENT": {"verified_by_fetch": True},
}
NOT_CODES_NOTE = ("overbought/RSI, resistance, 'ahead of FOMC/CPI/earnings', valuation, uncertainty, "
                  "VIX, positioning, 'wait for pullback' are NOT codes: such a line is dropped to core")
KILL_TYPES = ("PRICE_BELOW", "DATE", "EVENT", "RS_RANK_BELOW")
THESIS_MAX_WORDS = 30
DISSENT_MAX_WORDS = 60

# ---------------------------------------------------------------- watchdog / events
SHOCK_MOVE_PCT = 0.07                 # one-session |move| on a held name (QQQ included)
EARNINGS_WARN_SESSIONS = 2            # earnings D-2 on a held sleeve name
MAX_DATA_AGE_HOURS = 96
WATCHDOG_ONCALENDAR = "*-*-* 07:30:00 Asia/Seoul"
SYSTEMD_UNIT = "us-advisor-watchdog"
WEEKLY_RUN_DOW_KST = 5                # Saturday (Mon=0)
WEEKLY_RUN_HOUR_KST = 8

# ---------------------------------------------------------------- damage caps (as spec)
T1_PP = -3.0
T1_WEEKS = 12
T1_SLEEVE_CUT = 0.15
T1_DURATION_WEEKS = 8
T2_PP = -5.0
T2_WEEKS = 26
T4_CASH_WEEKS = 2
T5_UNFILLED_RUNS = 2
NOOP_FLAG_WEEKS = 6
TRAILING_12W_DAYS = 84
TRAILING_26W_DAYS = 182
BRIER_MIN_N = 20                      # logged always, displayed only at n >= this
BRIER_HORIZON_SESSIONS = 20
FLOW_WARN_USD = 5.0                   # |reported cash - explained cash| above this -> declare a flow

# ---------------------------------------------------------------- costs (KIS)
FX_SPREAD_PCT = 0.001
COMMISSION_PCT = 0.0007               # charged to shadow and paper books on every trade
CGT_ALLOWANCE_KRW = 2_500_000

# ---------------------------------------------------------------- epoch / ledger
EPOCH_START = None                    # set by the ledger 'epoch' line on the first --mark; never hardcode
LEDGER_TYPES = ("epoch", "mark", "flow", "order", "fill", "decision", "vote", "trigger")
TRIGGER_KINDS = ("T1", "T2", "T4", "T5", "NOOP", "KILL_PRICE", "KILL_DATE", "KILL_EVENT", "KILL_RS",
                 "REGIME_FLIP", "SHOCK_MOVE", "EARNINGS_D2", "SGOV_EXPIRY", "HORIZON", "STALE_DATA")

# ---------------------------------------------------------------- report
NARRATIVE_MAX_CHARS = 2500
SLACK_MAX_MESSAGES = 2
HONESTY_LINE_KO = ("표본 {weeks}주 — 실력 판정 불가. T1/T2는 손실 상한(damage cap)이며 "
                   "실력 검증이 아님(12주 추적오차 σ≈3.5%p, T1은 소음만으로도 약 20% 확률로 발동).")
