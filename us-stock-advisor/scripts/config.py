#!/usr/bin/env python3
"""us-stock-advisor v5 — SINGLE SOURCE OF TRUTH for every number.

Install path: ~/.claude/skills/us-stock-advisor/scripts/config.py

RULE (v5, non-negotiable): SKILL.md contains NO gating thresholds. If a number
gates a decision, it lives here and only here. The v4 config.py/SKILL.md double
bookkeeping (min_reward_risk 1.8 vs 2.0) is what this file deletes.
"""

from __future__ import annotations
import os

# ---------------------------------------------------------------- benchmark
BENCHMARK = "QQQ"                 # the core asset AND the accountability yardstick
CORE_TICKER = "QQQ"

# ---------------------------------------------------------------- regime
# Faber 10-month SMA on MONTHLY CLOSES. One term. No macro-fear term. No VIX.
REGIME_SMA_MONTHS = 10
REGIME_EVAL = "monthly_close"     # never intramonth
MIN_MONTHS_BETWEEN_REGIME_FLIPS = 1

# ---------------------------------------------------------------- allocation
TARGETS = {
    # regime -> (core_qqq_pct, satellite_budget_pct)
    "TREND":     (0.90, 0.10),
    "DEFENSIVE": (0.50, 0.00),
}
CASH_TARGET_PCT = 0.00            # cash is not an allocation. It is a residual.
CASH_MAX_PCT = 0.05               # operational float only (FX/settlement)
DEFENSIVE_CASH_EQUIV = "KRW MMF / 파킹통장"   # DEFENSIVE de-risk destination

REBALANCE_DRIFT_BAND_PCT = 0.05   # act only when |actual - target| > 5pp
FRACTIONAL_SHARES = True
MIN_ORDER_USD = 20.0              # below this, skip (KIS fractional minimum-ish)

# gating the allocation % while trusting the derivation is a hole: a plan that is
# 100% equity on paper can execute 100% cash. These bound the paper<->execution gap
# and the total book so no short/leverage/cash-leak passes the % gate.
ALLOCATION_SUM_TOLERANCE_PP = 0.5      # sum(weights) may not exceed 100 by more than this
EXECUTION_DIVERGENCE_TOLERANCE_PP = 2.0  # |derived-order equity − intended equity| ceiling

# ---------------------------------------------------------------- satellite
SATELLITE_MAX_PCT = 0.10          # of total portfolio
SATELLITE_MAX_NAMES = 2
SATELLITE_MIN_HOLD_DAYS = 20
SATELLITE_MAX_ROUNDTRIPS_PER_YEAR = 6
SATELLITE_MIN_RR = 1.5            # was 2.0/1.8. Satellite-only. Never gates the core.
SATELLITE_STOP_ATR_MULT = 1.5
SATELLITE_RSI_HALVE_ABOVE = 75.0  # overbought is a SIZING input, never a veto
EARNINGS_BLACKOUT_SESSIONS = 3    # no satellite entry within N sessions of earnings

# ---------------------------------------------------------------- caps
SINGLE_NAME_MAX_PCT = 0.40        # was 25%. Applies to SINGLE NAMES only.
BROAD_ETF_UNCAPPED = True         # QQQ/SPY/VOO/VTI are NOT single names. No cap. Ever.
BROAD_ETFS = {"QQQ", "SPY", "VOO", "VTI", "IVV", "QQQM"}

# ---------------------------------------------------------------- override channel
OVERRIDE_MAX_EQUITY_REDUCTION_PCT = 0.10   # may cut equity at most 10pp below target
OVERRIDE_CATALYST_MAX_AGE_HOURS = 24
OVERRIDE_EXPIRY_TRADING_DAYS = 10
OVERRIDE_SUSPEND_AFTER_N_BAD = 3           # 3 consecutive negative-spread overrides
OVERRIDE_SUSPENSION_DAYS = 90              # one calendar quarter, enforced in code

# ---------------------------------------------------------------- accountability
DARTBOARD_BASE_RATE = 0.398       # P(random pick beats matched-window QQQ) on this universe
TRACK_RECORD_WINDOW = 20          # last N decisions injected into Phase 2
KILL_SATELLITE_IF_SPREAD_BELOW_PP = -1.0   # vs mechanical baseline, after ...
KILL_EVAL_TRADING_DAYS = 125               # ~6 months of forward, post-cutoff data
KILL_MIN_OVERRIDE_HITRATE = 0.45
EXPAND_SATELLITE_IF_HITRATE_ABOVE = 0.398
BOARD_RECONVENE_IF_CORE_TRAILS_QQQ_PP = 3.0   # over 12 months

# ---------------------------------------------------------------- backtest acceptance
# core.py --backtest walks the archived v4.1 window bar-by-bar through
# compute_regime + build_plan with costs charged. These are the pass/fail bars.
BACKTEST_WINDOW = "2026-04-21:2026-07-10"   # the 46-run v4.1 window
V4_REALIZED_RETURN_PCT = 1.78               # what v4.1 actually made over it
BACKTEST_MIN_SESSIONS = 55                  # the window is 56 sessions long
BACKTEST_MIN_TREND_SESSIONS = 45            # honest walk: March's monthly close was
                                            # BELOW the SMA, so the mechanical core is
                                            # DEFENSIVE for the April sessions. It is
                                            # not TREND for all 56 and never was.
BACKTEST_MIN_RETURN_PCT = 8.0               # >> V4_REALIZED_RETURN_PCT, < buy&hold QQQ
                                            # (the April de-risk is the insurance premium)

# ---------------------------------------------------------------- data hygiene
DROP_PARTIAL_BAR = True           # today's in-progress bar is NOT a close
AUTO_ADJUST = True
MAX_DATA_AGE_HOURS = 96
FRESH_CATALYST_MAX_AGE_HOURS = 72   # satellite entry catalyst; monthly cadence
                                    # + 24h ≈ zero entries ≈ the kill trigger's
                                    # n>=10 sample never accrues. Overrides stay 24h.
VETO_MAX_AGE_DAYS = 7               # Phase 1 veto evidence
URL_CHECK_TIMEOUT_S = 5             # HEAD-resolvability check in validate.py
SHOCK_MOVE_PCT = 0.07               # single-session move on a held name that is an
                                    # event trigger; below it, single-day noise is
                                    # by design not an event.

# ---------------------------------------------------------------- costs (KIS)
FX_SPREAD_PCT = 0.001             # 0.1% preferential KRW<->USD
COMMISSION_PCT = 0.0007
CGT_ALLOWANCE_KRW = 2_500_000     # 250만원/yr — buy-and-hold usually never touches it

# ---------------------------------------------------------------- paths
SKILL_DIR = os.path.expanduser("~/.claude/skills/us-stock-advisor")
# US_ADVISOR_STATE lets tests/backtests redirect state to a scratch dir.
STATE_DIR = os.environ.get("US_ADVISOR_STATE", os.path.join(SKILL_DIR, "state"))
RECS_JSONL = os.path.join(STATE_DIR, "recommendations.jsonl")
SCORECARD_CSV = os.path.join(STATE_DIR, "scorecard.csv")
BASELINE_JSON = os.path.join(STATE_DIR, "baseline_plan.json")
LAST_RUN_JSON = os.path.join(STATE_DIR, "last_run.json")   # structured continuity. NOT prose.

# universe the satellite may draw from (script-enforced; the LLM may not add tickers)
SATELLITE_UNIVERSE = [
    "NVDA", "GOOGL", "MSFT", "META", "AMZN", "AAPL", "AMD", "TSM", "AVGO",
    "MU", "PLTR", "SOXX", "SMH",
]
# NOTE: no commodity/energy sleeve. v4's Agent 2 universe (FCX/XLE/XOM/CVX/SCCO)
# produced 43% of rejections and ~zero net return minus costs. Deleted.
