---
name: us-stock-advisor
description: 미국 주식 코어-새틀라이트 어드바이저. 결정론적 파이썬 코어(core.py)가 레짐·목표비중·정수 주 단위 주문(QQQ+QQQM 합산 코어)을 전부 계산하고, LLM은 (a) 근거 기반 veto 추출, (b) 비용을 지불하고 로깅되는 override 채널, (c) 실전 자금 없는 섀도우(페이퍼) 새틀라이트 종목 지명만 담당. 매월(+입금/이벤트 시) 실행, 누적 성과(TWR, 입출금 제외)를 QQQ와 대조해 자기 채점. KIS API/실거래 없이 리서치·판단만.
version: 5.1.2
argument-hint: "<portfolio.json 경로 또는 현금(USD)+보유종목(ticker,수량,평단가)> [--deposit KRW입금액] [--withdraw 출금액] [--weekly] [--event <사유>]"
allowed-tools: [Read, Write, Bash, Agent, WebSearch, WebFetch]
---

# US Stock AI Advisor v5.1 — Mechanical Core, LLM on Parole

**One-line thesis.** The default state is fully invested in the index. Every
deviation from the index — **including cash** — is a logged, scored, expiring bet
that must pay for itself or lose the right to be made.

v4.1 lost ~10pp of a 12.7pp rally over 46 near-daily runs. It did not lose that
to the market. It lost it to a governance failure in which "no" routed capital to
0%-yield cash 57% of the time. v5 makes that routing **impossible by construction,
not by prompt**: the deterministic core has no cash field to route to, and the
validator that enforces it is Python, not a judge.

**No live trading. No order execution. Research + judgment only.** The output is
a Slack report the human acts on (or doesn't).

**What v5.1 changed, in one paragraph.** The broker cannot buy fractional shares,
so v5.0's fractional orders were not executable and its "cash over max" trigger
fired forever on a residual nobody could deploy. v5.1 plans **whole shares** over
**one combined core sleeve** (`config.CORE_TICKERS`: QQQ and QQQM track the same
index, so QQQM is the small lot), reports the cash that no whole share can absorb as
an **unavoidable residual** instead of a trigger, moves real-money satellite entries
behind a switch (`config.SATELLITE_REAL_MONEY_ENABLED`, off) with a **shadow (paper)
track** that must earn graduation first, scores performance as a **time-weighted
return** (deposits and withdrawals are not P&L), refuses a **duplicate same-date
run** (exit 4), and makes the ledger **append-only for every writer, including the
nightly scorer**.

**What v5.1.1 (the red-team fix round) changed.** `validate.py` **HALTs** (exit 2) on
a baseline that is not this run's plan; DEFENSIVE overrides get **no** downward
allowance whatever their `direction` label says; allocation-only satellite cuts,
unfunded or unexecuted BUYs and weekly overrides FAIL; per-ticker price freshness in
`core.py`; failed overrides are logged (`override_rejected`); ledger tamper is
**sticky** for `config.OVERRIDE_SUSPENSION_DAYS`; declared cash flows are
**reconciled** against the ledger and the kill / board triggers read the
**conservative** return; a price outage reads `"unknown"`, never `false`.

**What v5.1.2 (the re-review fix round) changed.** A declared flow is verified only
when the ledger **corroborates** it — absolutely, relative to the declared amount, and
within a ledger-wide cumulative deviation cap (§Phase 4) — and the conservative return
never lets an unverified or undeclared flow help. The freshness HALT runs before
anything that can raise. An OVERRIDE that changes nothing FAILs
(`OVERRIDE_NO_EFFECT`), and a no-effect grade never resets the suspension streak.
Benign crashes are recoverable instead of reading as tamper: an interrupted commit is
completed by the next writer, a torn final line has an explicit, logged repair
(`report.py --repair-torn-tail`), and a single tamper detection no longer extends its
own window. `--log` refuses a HALT plan and a re-log that repeats the day's flow
(unless `--additional-flow`). `core.py` warns when it cannot de-risk a small book, and
a core share cheaper than `config.MIN_ORDER_USD` can no longer fire `cash_over_max`
with no order. The last gating numbers outside `config.py` moved into it.

**Language policy (kept from v4).** All internal reasoning, agent I/O, and JSON in
English. **Only the final Slack report (Phase 4) is Korean.**

**Numbers policy (new, load-bearing).** `scripts/config.py` is the single source of
truth for every threshold, cap, weight, and horizon. **This file contains no gating
numbers.** If you find yourself about to write a threshold into this document,
that is the v4 double-bookkeeping bug (config said R/R 1.8, SKILL said 2.0, the
reports used 2.0) reappearing. Put it in `config.py` and cite the symbol.

---

## Cadence — DAILY IS DELETED

| Trigger | What runs |
|---|---|
| **1st trading day of the month** | full pipeline (Phase 0→4) |
| **KRW deposit lands** | full pipeline (lump-sum in; do not stage entries) |
| **Event: QQQ *monthly* close crosses the SMA** | full pipeline (regime flip) |
| **Event: held name moves > `config.SHOCK_MOVE_PCT` in one session** | full pipeline |
| **Any day `events.cash_over_max` is true** — *deployable* excess cash: cash above the regime's target cash exceeds `config.CASH_MAX_PCT` of the book **and** it reaches the deployable threshold (one whole core share priced with `config.WHOLE_SHARE_PRICE_BUFFER_PCT`, never less than `config.MIN_ORDER_USD`; see Phase 0) | full pipeline (idle cash is the deleted defect; it may not sit unlogged until month-end) |
| **Event: T−3 to a held/candidate name's earnings** | Phase 0 + Phase 3 only |
| **Weekly** | satellite stop-check ONLY (`--weekly`) — see §Weekly |
| **Any other day** | **nothing. Do not run this skill.** |

Both event triggers are computed by `core.py` and printed in `baseline_plan.json`
under `events` (`shock_moves`, `cash_over_max`) — they are detected in code, not
noticed by a human. `events.cash_over_max_unavoidable` (excess above the float, but
no whole core share fits) is **informational and is not a run trigger**: re-running
cannot deploy cash that buys nothing, and in v5.0 exactly that residual re-fired the
trigger on every run. In DEFENSIVE the regime's cash sleeve is the target, not an
excess, so it never fires the trigger either.

46 daily runs were 46 chances to find a reason to say no, plus a compounding
per-run confidence tax on every position already held. The thesis horizon is
multi-week; the decision cadence must not be shorter than the thesis.

**Retire the v4 daily orchestrator.** Any cron/orchestrator entry that invokes
this skill daily must be deleted or repointed to the monthly trigger before v5's
first run. A daily caller silently reinstates deleted cadence.

---

## Pipeline at a glance

```
Phase 0  core.py        DETERMINISTIC   → baseline_plan.json  (PRE-APPROVED WHOLE-SHARE ORDERS
                                           + execution block + satellite levels)
Phase 1  veto scan      LLM  (sonnet, stock-research-readonly, ×1–2)  → vetoes[]
Phase 2  execute/override LLM (opus, ×1)                              → proposal.json (+ shadow_picks)
Phase 3  validate.py    DETERMINISTIC   → PASS (proposal) | FAIL (baseline enforced)
                                           shadow picks gated AFTER the verdict
Phase 4  report.py + LLM prose → recommendations.jsonl + Korean Slack report
```

Money-touching numbers are produced **only** in Phase 0 and checked **only** in
Phase 3. Both are Python. The LLM narrates, vetoes, may file an override, and may
name shadow tickers with a dated catalyst — and that is the entire extent of its
authority. Even a shadow pick's stop, target and R/R are Phase 0's numbers.

### Operating procedure — the exact command sequence

Run from `~/.claude/skills/us-stock-advisor` (live runs use the default state dir;
never set `US_ADVISOR_STATE` on a live run). Every step's exit code is read; nothing
is skipped.

`state/portfolio.json` is written by the top-level agent from the user's argument
(or the KIS balance script) before Phase 0. Its schema is exactly:
`{"cash_usd": <float>, "positions": [{"ticker": "QQQ", "shares": <float>, "cost_basis": <float, optional>}]}`
— whole shares held, USD cash after FX. Derive `cash_usd` from the user's stated
total (KRW → USD at the day's rate) minus the marked value of the positions.

A forced same-date re-log (`--force-relog --reason`) supersedes that day's earlier
run *including its shadow picks* — they are dropped from the shadow sample. Do not
re-log a day whose shadow picks should stand.

```
# Phase 0 — the approved plan (add --weekly on a weekly stop-check)
python3 scripts/core.py --portfolio state/portfolio.json --out state/baseline_plan.json
#   exit 0 -> continue.  exit 2 (bad input) / 3 (data failure) -> HALT (below).

# Phase 2 input — the scorecard (read-only for the LLM; may append override_score lines)
python3 scripts/score_recs.py --track-record

# Phase 1 — Task(subagent_type="stock-research-readonly", ...) -> vetoes[]
# Phase 2 — top-level thread writes state/proposal.json (schema in §Phase 2)

# Phase 3 — the gate. validate.py writes NO file: it prints ONE JSON object on
# stdout. Save that stdout verbatim as state/final_plan.json.
python3 scripts/validate.py --baseline state/baseline_plan.json \
    --proposal state/proposal.json > state/final_plan.json
#   exit 0 PASS -> log.   exit 1 FAIL (baseline enforced) -> log it the same way.
#   exit 2 HALT -> do NOT --log; run report.py --halt (below).

# Phase 4 — log, header, deliver
python3 scripts/report.py --log --baseline state/baseline_plan.json \
    --final state/final_plan.json [--deposit USD] [--withdraw USD]
#   exit 0 -> continue.  exit 4 duplicate date, or a re-log repeating the day's flow
#   (see §Phase 4).  exit 2 refused, nothing appended (malformed input, or a HALT
#   final plan: fix it, do not hand-edit the ledger).  exit 5 torn final ledger line /
#   unreadable ledger, nothing appended -> §Ledger recovery.  exit 6 state write
#   failed (e.g. read-only state/) -> fix permissions and re-run; see §Ledger recovery.
python3 scripts/report.py --header       # -> the numbers for the Korean header
# SEND the Korean report as a Slack DM (§Slack delivery). Not optional.

# HALT path (core.py non-zero, or validate.py exit 2):
python3 scripts/report.py --halt "<reason: exit code + first stderr line / violations[0]>" \
    --stage phase0          # or --stage phase3 for a validate.py HALT
#   exit 5/6 -> the halt could not be logged (§Ledger recovery); say so in the DM.
# then SEND the failure (not a recommendation) as the Slack DM, by the same route.
```

`report.py --log --final` accepts either the whole validate stdout (it unwraps
`final_plan` and reads `final_plan.rejected_override`) or the bare `final_plan`
object; saving stdout verbatim is the one documented form. Do not hand-assemble or
edit `final_plan.json` — every script-owned key in it is validate's.

**Where the shadow picks come from — one path, no shortcuts.** Phase 0 writes
`satellite.levels` into `baseline_plan.json` (one entry per universe name it could
price freshly) → Phase 2 names up to `config.SHADOW_MAX_PICKS_PER_RUN` of **those
tickers** (ticker, `direction`, `thesis_en`, `catalyst_url`, `catalyst_timestamp` —
no numbers) in `proposal.json` under `shadow_picks` → `validate.py` gates and
enriches them from the levels into `final_plan.shadow_picks` (rejections in
`shadow.rejected`) → `report.py --log` appends one `shadow_pick` line each. A ticker
without a `satellite.levels` entry is rejected (`SHADOW_NO_LEVELS`); an empty
`levels` (fetch failed, see `levels_error`) or a weekly run means no picks this run.

---

## Phase 0 — `core.py` · **DETERMINISTIC** · the authority

Run: `python3 scripts/core.py --portfolio <portfolio.json> --out state/baseline_plan.json`

**Inputs**
- yfinance daily OHLC, **bar-complete only** (today's partial bar is dropped;
  `auto_adjust` on) — see `config.DROP_PARTIAL_BAR`, `config.AUTO_ADJUST` — for
  every `config.CORE_TICKERS` name and every held name.
- **Per-ticker freshness.** A ticker is priced only if its **own** last valid bar is
  the frame's last bar — a name whose feed stopped days ago is not priced at its old
  close just because the frame as a whole is fresh (`data_age_hours` alone would read
  fresh). Consequences: a stale or missing `config.CORE_TICKER` → exit 3; a stale or
  missing **held** name → exit 3 (every % and order would be wrong); a stale or
  unpriced **unheld** QQQM → a line in `execution.warnings` and a QQQ-only plan; a
  stale universe name → no `satellite.levels` entry (so no shadow pick on it). The
  frame itself older than `config.MAX_DATA_AGE_HOURS`, or a negative age, → exit 3.
- A **separate, non-fatal** download of `config.SATELLITE_UNIVERSE` OHLC (+ earnings
  dates) for the satellite levels. It can never delay or kill the core plan.
- Account cash + positions (from the user's argument or the KIS balance script).
- `config.py`.
- `state/last_run.json` — **structured JSON only**, for regime-flip hysteresis.

**Decides (and nothing else in this skill may re-decide):**
- **Regime.** One term: QQQ **monthly close** vs its long-horizon SMA
  (length = `config.REGIME_SMA_MONTHS`, evaluated per `config.REGIME_EVAL`). `TREND` above, `DEFENSIVE`
  below. That is the whole model. It has 100+ years of out-of-sample evidence
  behind it (Faber) and it is not tuned on the last quarter. It reads
  `config.CORE_TICKER` closes only; QQQM never touches it.
- **Targets.** `config.TARGETS[regime]` → core % / satellite budget %. The core is
  **one sleeve summed over `config.CORE_TICKERS`**: core value, weight and drift are
  the QQQ+QQQM total, and anything held outside that set is satellite.
  **Unused satellite budget auto-routes to the core.** Cash target is zero.
  `targets.target_allocation` is written collapsed (`{"CORE": …}` plus held
  satellite names).
- **Orders — whole shares.** `config.FRACTIONAL_SHARES` is off: every order is an
  integer share count at `limit_ref_price` (the last complete close). A core order is
  emitted only when |core − target| exceeds `config.REBALANCE_DRIFT_BAND_PCT`, and one
  planner (`plan_core_orders`) sizes it for the baseline and for the validator alike:
  - **BUY:** the combination of QQQ/QQQM shares that deploys the most USD, where each
    share must fit in *spendable* cash (cash + this plan's SELL proceeds) at price ×
    (1 + `config.WHOLE_SHARE_PRICE_BUFFER_PCT`) — the buffer covers commission, FX
    and an overnight gap, so the emitted order is executable at the next open — and
    the cost may overshoot the core target by at most
    `config.CONFIRM_ALLOCATION_TOLERANCE_PP` of the book. Ties within
    `config.MIN_ORDER_USD` → fewer lines, then `config.CORE_TICKERS` order. It never
    spends more than spendable cash.
  - **SELL** (DEFENSIVE de-risk, or a derived override): the whole-share combination
    whose proceeds are nearest the needed de-risk, never more shares than held —
    **among the candidates the next run would not buy back**: the oversell (proceeds
    beyond the need) must stay below the deployable threshold (else `cash_over_max`
    fires next run) and may not exceed the drift band while a core share fits it
    (else the band re-buys). Either would be a two-run QQQ → QQQM conversion, so the
    **undersell is preferred among the candidates that survive**. Stated plainly:
    **a DEFENSIVE sell lands wherever the nearest surviving whole-share candidate
    puts it — that can be above the DEFENSIVE target or below it** (the current
    live book, 2 QQQ + $392, would sell 1 QQQ and land near 40% equity against a
    50% target; a random-book sweep found about 2% of books more than 5pp below).
    With one core lot a large slice of the book, the only sells that hit the target
    exactly are ones the next run would undo. Report the post-trade `execution.post_trade.equity_pct`, not the
    target, as what the book holds.
  - **The deployable threshold** (`deploy_threshold_usd`) is the cheapest *emittable*
    core line priced with `config.WHOLE_SHARE_PRICE_BUFFER_PCT`: one whole share, or —
    for a share cheaper than `config.MIN_ORDER_USD` — the fewest whole shares whose line
    reaches it (a smaller line is not emitted, so it is not deployable). The BUY and
    SELL searches likewise never pick a combination containing a sub-minimum line, so
    `cash_over_max` cannot fire on a plan that places no order.
    `events.cash_over_max`, `execution.residual.deployable` and the SELL search use
    this same number. The residual also counts the buffer this plan reserves on its
    own BUYs as spent, so a whole-share BUY never reads as leaving deployable cash
    behind.
  - **No conversion, ever.** All core lines in a plan share one direction; there is
    no path that sells QQQ to buy QQQM or back. Which core ticker ends up held is
    path-dependent, and that is accepted.
  - A line under `config.MIN_ORDER_USD` (or of zero shares) is not emitted. SELLs are
    listed before BUYs. Satellite exits (stop breach, DEFENSIVE force-close) sell the
    **entire** held quantity, legacy fractional lots included.
  - Flipping `config.FRACTIONAL_SHARES` back on restores v5.0's single fractional core
    line — a one-symbol change, never an LLM decision.
- **The unavoidable residual.** Whatever cash no whole core share can absorb after
  the plan is reported in `execution.residual` (`unavoidable: true` + a reason) —
  not traded, not a trigger, not a choice. `config.CASH_MAX_PCT` stays the *paper*
  float the validator checks; the residual never widens it.
- **Satellite stops** (ATR-based, satellite names only) and `days_to_earnings`
  per held/candidate name (from `Ticker.get_earnings_dates()`, a date — not a
  headline).
- **Satellite levels** for every universe name, before any LLM has picked anything:
  `satellite.levels[T] = {price, atr_14, stop, target, rr, rsi, days_to_earnings,
  atr_source}` with stop = price − `config.SATELLITE_STOP_ATR_MULT` × ATR14 and
  target = price + `config.SHADOW_TARGET_ATR_MULT` × ATR14. **R/R is therefore a
  constant bracket ratio by construction — a definition, not a forecast** — and
  nobody may present it as the latter. Targets are never derived from support /
  resistance (that would be DELETE-list item 4 wearing a formula). `atr_source` is
  `ohlc` live and `close_proxy` in `--prices-csv` mode. If the universe fetch fails,
  `levels` is empty, `levels_error` says why, and every shadow pick is rejected
  (`SHADOW_NO_LEVELS`) — the core plan is unaffected.

**Emits** `baseline_plan.json` (schema `baseline_plan/v5.1`) with
`"status": "PRE_APPROVED"`. This is not a suggestion. It is today's order list,
already approved, before any LLM has read anything. Alongside the orders:
- `prices` — every ticker Phase 0 priced (this is what makes QQQM allocatable while
  unheld);
- `execution` — what the whole-share orders actually produce: `whole_shares`,
  `share_price_buffer_pct`, `min_core_lot_usd`, `core_drift_pp_before/after`,
  `post_trade` (positions, `core_pct`, `satellite_pct`, `equity_pct`, `cash_usd`,
  `cash_pct`), `post_trade_allocation`, `residual` (`cash_usd`, `excess_cash_usd/pct`,
  `deployable`, `unavoidable`, `reason`) and `warnings` — which, besides stale /
  unpriced unheld core tickers, names the case where the post-trade core drift still
  exceeds `config.REBALANCE_DRIFT_BAND_PCT` and **no** whole-share core order can be
  placed (e.g. DEFENSIVE on a one-share book: half a share cannot be sold). Report that
  warning; never describe such a book as de-risked;
- `satellite.levels`, `satellite.real_money_enabled`, `satellite.levels_error`;
- `events` — `shock_moves`, `cash_over_max`, `cash_over_max_unavoidable`.

`limit_ref_price` is the prior close. A gap larger than the buffer can still make
the last share unaffordable at the open; the report tells the human to buy **up to**
the stated whole shares that cash allows, never a fraction and never on margin.

**The core carries no stop.** Deliberately. A daily SMA stop sold QQQ at 711.44 on
7/08 two days before it closed at 725.51. Left-tail gap risk on the core is
accepted beta, disclosed in the report, and insurable only by not being an equity
investor.

**Forbidden:** nothing. Phase 0 is the authority. If `core.py` exits non-zero,
**the pipeline halts** — no approved plan means there is nothing for the LLM to
execute, and "the script failed so I'll decide myself" is exactly the failure mode
this version exists to delete.
The same halt applies when Phase 3 refuses the baseline (`validate.py` exit 2, see
§Phase 3 — HALT).
A halt is never silent: run `python3 scripts/report.py --halt "<reason>"`, which
appends a `{"type":"pipeline_halt"}` line to `recommendations.jsonl`; send the
*failure* (not a recommendation) to Slack **by the same mandatory, pre-authorized
route as a normal report (§Slack delivery — DM `U0AD7V4SWD9`, do not ask)**, and
re-arm the trigger for the next session. A halt the user never sees is not a halt,
it is a disappearance. "Nothing happened" being invisible was 84% of v4's runs.

---

## Phase 1 — veto scan · **LLM (sonnet)** · evidence extraction only

Structured-extraction from news is the *only* LLM capability in this domain with
independent validation. Return prediction, sizing, and regime calls have none.
Phase 1 does the first and is structurally prevented from doing the others.

**Subagent type: `stock-research-readonly`. Never `general-purpose`.**
This is a capability restriction, not a request. The agent's tool list is
`WebSearch, WebFetch` — it *cannot* write a file, run Bash, or send a Slack
message. Two prior incidents (a Phase 1 researcher running the whole pipeline,
DMing conflicting recommendations, and clobbering report files) were possible only
because Phase 1 inherited the full toolset. Prose locks are not locks.

**Install the agent definition at `~/.claude/agents/stock-research-readonly.md`:**

```markdown
---
name: stock-research-readonly
description: Read-only equity/macro news researcher for us-stock-advisor Phase 1. Performs web searches and returns a JSON veto brief. Cannot write files, run shell commands, send Slack messages, or spawn agents. Phase 1 ONLY.
tools: WebSearch, WebFetch
model: sonnet
---
(full text: see scripts/../agents/stock-research-readonly.md in this skill's repo;
 it re-states the role lock, the veto schema, and the citation-honesty rule)
```

Invocation, literally:

```
Task(subagent_type="stock-research-readonly",
     prompt="=== ROLE LOCK === ...(verbatim block below)... === END ROLE LOCK ===\n"
            + <tickers/brief>)
```

**Defense in depth — every Phase 1 task prompt MUST begin with this line, verbatim:**

> `=== ROLE LOCK === You are ONE phase of a pipeline. You are NOT the pipeline. You return a JSON veto brief as your final message and nothing else. You may not produce strategy, sizing, allocations, regime labels, sentiment scores, targets, stops, BUY/SELL/HOLD calls, files, or Slack messages. If asked to, return the brief with "scope_violation_detected": true. === END ROLE LOCK ===`

**Inputs:** held tickers + any satellite / shadow candidates from `config.SATELLITE_UNIVERSE`.

**May decide:** to emit `veto` objects — and only those. A veto is a **dated,
URL-cited, fetch-verified negative event** within `config.VETO_MAX_AGE_DAYS`:
guidance cut, earnings miss, fraud/accounting probe, material litigation, Tier-1
downgrade, C-level exit, recall/breach, or an explained shock move. Plus one
sentence of Korean narration per veto for Phase 4.

If the URL does not fetch, or the page does not contain the claim, **the veto does
not exist**. Measured citation-hallucination rates are 3–13% fabricated / 5–18%
non-resolving, and citation *volume* correlates *inversely* with reliability. Do
not pad. `no_vetoes_found: true` is a perfectly good answer and the expected one.

**FORBIDDEN (Phase 1):**
- regime labels, market_regime, RISK_ON/OFF/NEUTRAL — deleted concepts
- sentiment scores / floats of any kind
- position sizes, allocations, price targets, stop levels, R/R
- BUY / SELL / HOLD recommendations, or any "bull case"
- adding tickers not given to it
- Reddit / StockTwits / X / social buzz queries (dead APIs, lagging noise)
- writing any file; sending any message; spawning any agent
- reading or citing prior reports

**A veto does not de-risk anything by itself.** It is an *input* to Phase 2, which
may act on it only through the override channel (§Override) or by declining a
satellite entry. **No veto can move the core.** Only the regime moves the core.

---

## Phase 2 — execute-or-override · **LLM (opus, ×1)** · on parole

**Phase 2 runs in the top-level agent thread. It is not a subagent and no Task
call is made for it** — the top-level thread is the only context allowed to hold
Write/Bash/Slack, and it is where the human is watching.

**The Phase 2 prompt begins, verbatim:**

> **"The plan below is today's default order. Whether to be invested is not your
> decision — it is decided. Your job is to execute it, or to file an OVERRIDE."**

**Inputs (and only these):**
- `baseline_plan.json` (PRE_APPROVED)
- Phase 1 `vetoes[]`
- `<track_record>` — from `score_recs.py --track-record`: the last
  `config.TRACK_RECORD_WINDOW` decisions, hit rate **against the
  `config.DARTBOARD_BASE_RATE` dartboard base rate (not 50%)**, cumulative spread
  vs QQQ, and cumulative spread vs the un-overridden mechanical baseline (the LLM
  layer's isolated P&L — that number is *your* scorecard).
- `state/last_run.json` — structured positions/regime **only**.

**May decide, in this order of expectation:**
1. **CONFIRM_BASELINE.** The default. The expected output. Confirming costs nothing
   and requires no justification, because the baseline is already approved.
2. **OVERRIDE** — see §Override. Costly, logged, auto-expiring.
3. **Shadow picks (paper satellite)** — see §Shadow track below. Up to
   `config.SHADOW_MAX_PICKS_PER_RUN` names from `config.SATELLITE_UNIVERSE`, each with
   a one- or two-sentence thesis and a dated, fetch-verified catalyst URL. **No
   numbers.** No money moves. This is how the satellite earns the right to exist.
4. **Real-money satellite proposal — DISABLED.** While
   `config.SATELLITE_REAL_MONEY_ENABLED` is off, any `satellite[]` BUY, and any
   non-core weight above what is already held, is a hard FAIL
   (`SATELLITE_NOT_GRADUATED`) under either decision. Holding or reducing a name
   already held is still possible — but a reduction beyond what the baseline itself
   sells must be **declared** as a `satellite[]` SELL (so the min-hold churn guard
   runs on it), else `SATELLITE_SELL_NOT_DECLARED`. After graduation (a human edit to `config.py`,
   never the pipeline) the v5 rules apply unchanged: within `config.SATELLITE_MAX_PCT`
   / `config.SATELLITE_MAX_NAMES`, universe only, a dated catalyst fresher than
   `config.FRESH_CATALYST_MAX_AGE_HOURS` with a fetchable URL, the **script's** R/R
   from `baseline.satellite.levels` ≥ `config.SATELLITE_MIN_RR` (satellite-only; the
   core is never R/R-gated), and no earnings inside `config.EARNINGS_BLACKOUT_SESSIONS`.
   RSI above `config.SATELLITE_RSI_HALVE_ABOVE` **halves the size** — it is a sizing
   input and never a veto. ("Overbought, don't chase" rejected 43 names in v4 that
   then returned +9.05%, beating QQQ by 6.67pp. It was a systematic short on the
   system's own best ideas.)

**FORBIDDEN (Phase 2):**
- **Choosing cash.** There is no cash field in the output schema. Unallocated
  satellite budget auto-routes to the core. A proposal containing `cash`,
  `CASH`, `USD`, `KRW`, or `MMF` in its allocation is a hard FAIL in Phase 3.
  Cash *proxies* (SGOV/BIL/SHV or any ticker outside `config.SATELLITE_UNIVERSE` ∪
  `config.BROAD_ETFS`) are the same violation wearing a ticker, and FAIL the same way.
- Emitting orders, share counts, or USD amounts. Orders are recomputed by
  `validate.py` from the validated `final_allocation`. A proposal containing an
  `orders` field is a schema FAIL.
- Touching the core allocation except through the override channel.
- Touching the regime. Ever. The SMA owns it.
- Filing an OVERRIDE on a `--weekly` run (`OVERRIDE_IN_WEEKLY`): a weekly run is a
  stop-check, and an override would derive a full rebalance outside the cadence.
- Inventing sentiment floats, conviction floats, or any number not produced by a
  script. **This includes stop, target, R/R, entry price, ATR, size, shares and USD**
  — for a real satellite entry they are read from `baseline.satellite.levels` (an
  LLM-supplied value that differs is `LLM_INVENTED_NUMBER`; omit them), and a shadow
  pick carrying any of them is rejected (`LLM_NUMBER_IN_SHADOW`).
- Choosing, or trying to steer, the QQQ/QQQM split. It is ignored (see below).
- Putting a shadow-picked ticker into `final_allocation` (`SHADOW_IN_ALLOCATION`).
- Adding tickers outside `config.SATELLITE_UNIVERSE`.
- Reading, citing, or imitating prior report **prose**. (The v4 report archive
  became the effective prompt and carried abolished rules forward for 7 runs —
  the model invented 25%-cap violations that v4.1 had already deleted.)
- Reasoning of the form "the market feels extended", "let's wait for a pullback",
  "patience over activity", "when in doubt", "preserve capital first". Every one of
  these was measured, in this account, as a value-destroying rejection rationale.
  They are not cautious. They are a −10pp position.

**Output schema (`proposal.json`):**
```json
{
  "decision": "CONFIRM_BASELINE | OVERRIDE",
  "final_allocation": {"QQQ": 100.0},
  "satellite": [{"ticker":"...","action":"HOLD|SELL","size_pct":0,
                 "catalyst_url":"https://...","catalyst_timestamp":"ISO8601"}],
  "shadow_picks": [{"ticker":"...","direction":"LONG","thesis_en":"<= 2 sentences",
                    "catalyst_url":"https://...","catalyst_timestamp":"ISO8601"}],
  "override": null,
  "vetoes": [],
  "rationale_en": "<= 6 sentences. No hedging vocabulary."
}
```
(The numbers in that skeleton are shape, not thresholds.)

`final_allocation` is the allocation **after the plan executes** — i.e. the target
weights, not today's drifted holdings. It is required (a proposal without it cannot
be gated and is a schema FAIL), it is the *only* thing that defines satellite
exposure (the `satellite[]` list is narration; the allocation is the position), and
`validate.py` derives the order list from it. There is no `orders` field.

**The core line.** Write real tickers. Weights on any `config.CORE_TICKERS` key are
**summed into one core weight and the split is ignored**: `{"QQQ": 100.0}`,
`{"QQQM": 100.0}` and the baseline's post-trade split are the same input. The LLM
therefore cannot steer the split or force a conversion. A literal `"CORE"` key is not
a ticker and FAILs as off-universe. For `CONFIRM_BASELINE`, copy the baseline:
`{"QQQ": targets.target_allocation.CORE}` plus any held satellite name at its target
weight. A CONFIRM whose collapsed allocation matches neither
`targets.target_allocation` nor `execution.post_trade_allocation` within
`config.CONFIRM_ALLOCATION_TOLERANCE_PP` per line is `CONFIRM_MISMATCH` — the
baseline still executes, but the run is logged as a FAIL. A CONFIRM that **does**
match one of those two references is the baseline, so it is not failed by an
allocation-shape check the baseline itself could not pass (an untrimmable held
satellite share just over budget or over the single-name cap, an all-cash book too
small to buy anything so the post-trade allocation is empty): the empty-allocation
check, `SATELLITE_OVER_BUDGET_IN_ALLOCATION`, the allocation-side
`SATELLITE_IN_DEFENSIVE` / `STOP_BREACH_IN_ALLOCATION` and `SINGLE_NAME_OVER_CAP` are
skipped for it. `SATELLITE_NOT_GRADUATED` and `SHADOW_IN_ALLOCATION` still run.

### Shadow track — the satellite's probation

The LLM names **tickers and catalysts**; `core.py` owns every level. Each accepted
pick is enriched from `baseline.satellite.levels` (`entry_ref_price`, `atr_14`,
`stop`, `target`, `rr`, `rsi`, `days_to_earnings`, `horizon_days`), logged as a
`shadow_pick` line with `real_money: false`, and graded by `score_recs.py` at
+1/+5/+`config.SHADOW_HORIZON_DAYS` sessions against a matched-window QQQ (entry =
first close on/after the pick date, no look-ahead), plus a close-based bracket
outcome (`TARGET` / `STOP` / `OPEN`).

**Matured vs unmatured.** A pick is *matured* only once `config.SHADOW_HORIZON_DAYS`
sessions have elapsed after its entry; only matured picks enter the hit rate, the
mean excess and the bracket tally (`n_graded`). An unmatured pick is counted in
`n_unmatured` (header `섀도우_미성숙_건수`), and its bracket reads `PENDING` — never
`OPEN`, never a miss. `섀도우_표본수` is graded/total, so a young track reads e.g.
"0/3" with three unmatured: that is "no evidence yet", not "zero hits". On a price
outage the shadow block reports `price_data_ok: false` and `n_unmatured` null.

Rules, all code-enforced by `validate.py` — the same entry rules as real money, so
the evidence transfers:
- `ticker` in `config.SATELLITE_UNIVERSE` (a core ticker is not a pick: rejected as
  `TICKER_NOT_IN_UNIVERSE`, and it never trips `SHADOW_IN_ALLOCATION`); `direction` is
  `LONG`; `thesis_en` present; at most `config.SHADOW_MAX_PICKS_PER_RUN` accepted per
  run.
- `catalyst_url` is http(s) and `catalyst_timestamp` is fresher than
  `config.FRESH_CATALYST_MAX_AGE_HOURS`. `validate.py` makes **no** network call for
  shadow picks, so the URL's content must be WebFetch-verified by the LLM phases, as
  for a veto. An unverified catalyst is not a catalyst.
- Not in DEFENSIVE, not on a `--weekly` run, not inside
  `config.EARNINGS_BLACKOUT_SESSIONS` of earnings, not a name held with real money, and
  not a ticker picked within `config.SHADOW_REPICK_COOLDOWN_DAYS` (overlapping windows
  would inflate the sample).
- Rejection codes: `SHADOW_SCHEMA`, `SHADOW_OVER_LIMIT`, `TICKER_NOT_IN_UNIVERSE`,
  `SHADOW_NO_LEVELS`, `SHADOW_DIRECTION`, `SHADOW_NO_CATALYST_URL`,
  `SHADOW_CATALYST_STALE`, `SHADOW_NO_THESIS`, `LLM_NUMBER_IN_SHADOW`,
  `SHADOW_IN_DEFENSIVE`, `SHADOW_WEEKLY`, `EARNINGS_BLACKOUT`, `SHADOW_ALREADY_OPEN`,
  `SHADOW_HELD_REAL`. A bad pick is dropped **individually**; it never FAILs the
  proposal.

**Graduation is reported, never applied.** `score_recs.py` prints `graduation_ok`
(header `실전_승격_가능`) only when **all three** hold: at least
`config.SHADOW_GRADUATION_MIN_PICKS` picks graded at the full horizon; a one-sided
exact binomial p-value of the hit count against `config.DARTBOARD_BASE_RATE` of at
most `config.SHADOW_GRADUATION_MAX_PVALUE`; and mean excess at the
`config.SHADOW_HORIZON_DAYS` horizon above
`config.SHADOW_GRADUATION_MIN_MEAN_EXCESS_PP`. Even then, turning on
`config.SATELLITE_REAL_MONEY_ENABLED` is a human edit to `config.py`. At a monthly
cadence with the per-run cap and the cooldown, that sample takes many months to
accrue. That is the honest timeline. Do not loosen the thresholds to speed it up.

---

## The OVERRIDE channel — the burden-of-proof inversion, literally

Doubt now has a default, and the default is the index. The only way to deviate:

1. **Direction-limited.** An override may reduce equity by at most
   `config.OVERRIDE_MAX_EQUITY_REDUCTION_PCT` below the regime target. It may never
   raise satellite above budget, never lever above target, and never touch the SMA
   regime. **In DEFENSIVE the channel is re-risking ONLY** — the LLM may never
   deepen a de-risk. (That is how v4 turned a lagging fear label into
   buy-high/refuse-low.) **`direction` is a self-declared label, not a permission**:
   `direction: "re_risk"` is required in DEFENSIVE (anything else, or a missing one,
   is `OVERRIDE_FORBIDDEN`) and is what lets the paper ceiling rise, but the label
   buys no downward room. In DEFENSIVE the de-risk allowance is **never** granted
   downward — the paper floor, the cash ceiling and the execution floor carry none —
   and the realised whole-share equity is checked against the baseline's realised
   equity on the numbers: below it is `OVERRIDE_DEEPENS_DEFENSIVE`, whatever the
   label says.
   **Weekly runs take no override** (`OVERRIDE_IN_WEEKLY`).
2. **Required fields, all code-validated:** `catalyst_description`, `catalyst_url`
   (must resolve — HEAD-checked by `validate.py`; content verified by the LLM phases
   via WebFetch), `catalyst_timestamp` (fresher than
   `config.OVERRIDE_CATALYST_MAX_AGE_HOURS`), `expected_cost_if_wrong_pct`,
   `qqq_forward_20d_if_i_am_wrong`. Missing, late, unfetchable, or future-dated
   catalyst ⇒ hard FAIL ⇒ **the baseline executes unmodified**.
3. **Auto-expiry** after `config.OVERRIDE_EXPIRY_TRADING_DAYS`. The baseline
   reasserts itself unless a *new* dated catalyst re-files. An override is a bet
   with a clock, not a new policy.
4. **Scored.** Every executed override is an `override` line in
   `recommendations.jsonl` carrying both post-trade equity weights
   (`baseline_equity_pct`, `realised_equity_pct`). Once
   `config.OVERRIDE_EXPIRY_TRADING_DAYS` sessions have passed, the scorer grades it:
   **spread (pp) = (realised − baseline equity %) / 100 × QQQ's forward return over
   those sessions** — a de-risk before a fall scores positive, before a rally
   negative. It is **appended** as an `override_score` line (the override line is
   never rewritten). A legacy core override without the two weights is marked
   `graded: false` (spread null, counted in `오버라이드_미채점_건수`) rather than scored
   zero; a legacy satellite-ticker override keeps the old ticker-vs-QQQ excess. The
   override hit rate (`오버라이드_적중률`) is hits / (hits + misses): a spread of exactly
   zero is neither. The running override P&L prints in every report.
   **A failed override is logged too.** When an OVERRIDE FAILs, `validate.py` puts
   `final_plan.rejected_override` (`override`, `proposed_allocation`, `violations`)
   in its output and `report.py --log` appends one `override_rejected` line
   automatically — never omitted, never scored as executed, never counted toward
   suspension (header `오버라이드_거부_건수`). Nothing for the LLM to do but not hide it.
5. **Escalating cost.** After `config.OVERRIDE_SUSPEND_AFTER_N_BAD` consecutive
   overrides with negative spread vs baseline, `validate.py` **suspends override
   privileges** for `config.OVERRIDE_SUSPENSION_DAYS`. A counter in code, not a
   sentence in a prompt. The streak counts **one override per run date** (a re-logged
   override, even with different weights, is one decision), and a spread of exactly
   zero (no effect) neither extends nor resets it — a no-effect override cannot buy
   its way out of the streak (and since v5.1.2 it FAILs anyway, `OVERRIDE_NO_EFFECT`).
   Grades are matched to their override by the override line's own `prev_hash`
   (`ref_prev_hash` on the `override_score` line), never by timestamp alone. During suspension the only legal output is
   CONFIRM_BASELINE.
6. **Executed in whole shares, and judged on what executes.** `validate.py` derives
   the override's orders with the same whole-share planner as the baseline. Its
   **realised** equity may sit at most the allowance (+
   `config.EXECUTION_DIVERGENCE_TOLERANCE_PP`) below what the baseline itself
   realises (`execution.post_trade.equity_pct`), else `OVERRIDE_EXECUTION_BELOW_FLOOR`;
   in DEFENSIVE the allowance is zero and `OVERRIDE_DEEPENS_DEFENSIVE` allows no
   tolerance either. Lot rounding is never a free extra de-risk. The derived orders
   must also be fundable (`EXECUTION_NEGATIVE_CASH`: non-core BUYs are capped by cash
   plus sell proceeds) and must execute what the proposal declares
   (`SATELLITE_BUY_NOT_EXECUTED`). **Stated honestly: at this account
   size one core lot is a larger slice of the book than the allowance, so most
   de-risk overrides are infeasible** — a partial trim either buys no share at all
   (and lands below the floor) or cannot be expressed. That is intended, not a bug to
   route around; the channel becomes usable as the account grows. Before filing, check
   the whole-share result against the baseline's `execution` block.

There is no other lever. There is no "adjust", no "trim for prudence", no
"NO_VIABLE_ALTERNATIVE", no "reduce until clarity". Those are cash by another name
and cash is not a decision this skill can make.

---

## Phase 3 — `validate.py` · **DETERMINISTIC** · the gate

Run: `python3 scripts/validate.py --baseline state/baseline_plan.json --proposal state/proposal.json > state/final_plan.json`
(`--recs` / `--last-run` default to the live ledger and anchor). The script writes
no file itself; stdout is the whole output (see §Operating procedure).

**HALT — exit 2, before any check runs** — before the ledger is even read, and again
on the validator's exception path, so an unreadable ledger can never turn a stale
baseline into executable orders. The baseline must be *this run's* plan. If
`baseline_plan.json` cannot be read, or its `generated_utc` is missing / not
ISO-8601 (`BASELINE_UNDATED`), in the future (`BASELINE_FROM_FUTURE`) or older than
`config.BASELINE_MAX_AGE_HOURS` (`BASELINE_STALE` — e.g. a plan left behind by a
`core.py` that exited non-zero), `validate.py` prints `"verdict": "FAIL"`,
`"halt": true`, a `HALT: …` violation, `final_plan.source: "HALT"` and
`final_plan.orders: []`, and exits **2**. **Nothing executes — neither the proposal
nor the baseline.** This is not a FAIL-with-baseline: do **not** run
`report.py --log` on it (that would log an empty plan as a no-op). Run
`report.py --halt "<the HALT violation>" --stage phase3`, send the *failure* to Slack
by the normal route, and re-run from Phase 0 if the session allows.

Replaces v4's two LLM judges (67% PASS_WITH_WARNINGS, 2.2% FAIL — theater; and the
single FAIL fired *against* the fix it was supposed to protect). A judge model can
be test-retest reliable and position-biased at the same time: *consistently wrong
is not the same as right.*

**Checks (all thresholds from `config.py`):**
- `decision` is exactly one of `CONFIRM_BASELINE` / `OVERRIDE` (any other value, or a
  missing one, is a schema FAIL — it must not skip the override protocol)
- schema contains no cash allocation of any kind, and no cash *proxy* ticker
- weights parse as finite numbers, are long-only (no negative/short weights), no single
  line item exceeds the book, and they sum to **at most** 100% (the shortfall is the
  cash residual; leverage is not a residual)
- every allocated ticker is **priceable by Phase 0** (`baseline.prices` — which
  includes QQQM while unheld — a held name, or an order's reference price); an
  unpriceable ticker whose order would silently become cash is a FAIL
- the core is **one line**: weights on `config.CORE_TICKERS` are collapsed before any
  check, so the split can neither be steered nor turned into a conversion
- the **derived orders are re-checked against the intended equity**, twice, because
  whole shares make the two numbers differ: `EXECUTION_DIVERGES` compares an
  unconstrained derivation with the intended equity (catches dropped / unpriceable
  lines — a plan that is fully invested on paper but lands in cash on execution
  FAILs; gating the % while trusting the derivation is the hole this closes), and
  `OVERRIDE_EXECUTION_BELOW_FLOOR` / `OVERRIDE_EXECUTION_ABOVE_CEILING` bound the
  **whole-share realised** equity against the baseline's realised equity (see
  §Override item 6)
- a satellite name whose allocation weight is new or increased must be a declared
  satellite BUY (it may not escape the satellite gates by living only in the allocation);
  symmetrically, a derived satellite SELL larger than the baseline's own SELL of that
  name must be a declared `satellite[]` SELL (`SATELLITE_SELL_NOT_DECLARED`), so the
  min-hold guard runs on allocation-side cuts too; and a declared satellite BUY that
  derives no order is `SATELLITE_BUY_NOT_EXECUTED`
- **allocation-side force-close and stops:** in DEFENSIVE any non-core weight above
  zero in `final_allocation` is `SATELLITE_IN_DEFENSIVE` (not only a `satellite[]`
  entry); a weight on a name whose ATR stop is breached is `STOP_BREACH_IN_ALLOCATION`
  — the baseline's stop-out and force-close cannot be cancelled through the allocation
- the derived book is funded: post-trade cash below zero is `EXECUTION_NEGATIVE_CASH`
- an `OVERRIDE` whose derived orders are exactly the baseline's (same tickers, sides
  and share counts) is `OVERRIDE_NO_EFFECT`: it changes nothing, so it is not an
  override (use `CONFIRM_BASELINE`); the baseline executes and the rejected override is
  logged like any other FAIL
- `OVERRIDE` on a weekly baseline is `OVERRIDE_IN_WEEKLY`; in DEFENSIVE a realised
  equity below the baseline's realised equity is `OVERRIDE_DEEPENS_DEFENSIVE`
  (§Override item 1)
- **real-money satellite switch:** while `config.SATELLITE_REAL_MONEY_ENABLED` is off,
  any satellite BUY or any non-core weight above what is held is
  `SATELLITE_NOT_GRADUATED`, under either decision
- **shadow isolation, both directions:** a shadow-picked ticker with real weight above
  what is held is `SHADOW_IN_ALLOCATION`; and after derivation, no order that would
  execute may BUY anything outside the core sleeve while the switch is off
  (`SHADOW_ISOLATION_BREACH`). Shadow picks themselves are validated only **after the
  verdict is fixed** and never touch `violations`, the orders, or the allocation:
  `verdict`, `violations`, `final_plan.orders` and `final_plan.final_allocation` are
  identical with and without `shadow_picks` (a metamorphic test enforces it; the one
  permitted difference is `SHADOW_IN_ALLOCATION` on a proposal that already FAILs)
- OVERRIDE, on paper: equity ≥ regime target − operational float − active-override
  allowance (**no allowance in DEFENSIVE**); and ≤ target (no levering up — in
  DEFENSIVE a valid `re_risk` override may rise by the allowance, capped at the TREND
  equity total)
- OVERRIDE, on paper: implied cash ≤ regime cash sleeve + operational float + allowance
  (again no allowance in DEFENSIVE)
  (**the >60%-cash trigger and its 40–60% dead zone are deleted**). This is the paper
  float `config.CASH_MAX_PCT`; the unavoidable whole-share residual never widens it
- satellite ≤ budget, ≤ max names, universe-restricted, force-closed in DEFENSIVE
- satellite BUY (only once graduated): the **script's** R/R from
  `baseline.satellite.levels` ≥ min (`SATELLITE_NO_SCRIPT_LEVELS` if absent), any
  LLM-written `rr` / `stop` / `target` equal to the script's (`LLM_INVENTED_NUMBER`),
  catalyst URL + fresh timestamp, earnings blackout clear
- single-**name** cap; **broad ETFs are uncapped, forever** (`config.BROAD_ETFS`)
- override schema complete, catalyst fresh + fetchable, direction legal,
  privileges not suspended
- `data_age_hours >= 0` and finite (a negative age means a partial bar leaked; NaN,
  infinity, a boolean or a non-number is a corrupt baseline — all `DATA_AGE_INVALID`,
  **hard abort**; in v4 a negative age silently made the staleness check pass on
  exactly the worst runs); an age over `config.MAX_DATA_AGE_HOURS` is `DATA_STALE`
- malformed shapes never crash the gate: a non-list `satellite` / `vetoes`, a
  non-object item in them, a non-object `override`, or any uncaught exception inside
  the validator is a listed `SCHEMA_VIOLATION` with the baseline enforced
- CONFIRM_BASELINE must equal the baseline (no silent edits under a confirmation):
  the collapsed allocation must match `targets.target_allocation` or
  `execution.post_trade_allocation` within `config.CONFIRM_ALLOCATION_TOLERANCE_PP`
  per line, else `CONFIRM_MISMATCH`. Because the orders are the baseline's verbatim,
  the paper floor / ceiling / cash-ceiling checks are skipped for a CONFIRM (a legacy
  baseline without an `execution` block falls back to them), and a **matched**
  CONFIRM also skips the allocation-shape checks listed in §Phase 2 (the core line)

**Output** (stdout JSON): `verdict`, `violations`, `shadow` (`accepted` — enriched
with script levels — and `rejected` with reason codes), `ledger_tamper`, and
`final_plan` carrying `orders`, `source` (`BASELINE` / `OVERRIDE_VALIDATED` /
`BASELINE_ENFORCED` / `HALT`), `execution`, the accepted `shadow_picks` and
`rejected_override` (null unless a FAILed OVERRIDE). On PASS every script-owned key
the LLM may have written is overwritten.

**Verdict semantics:** `PASS` (exit 0) → the proposal is the final plan. `FAIL`
(exit 1) → **the pre-approved baseline executes unmodified**, and it is logged like
any run. FAIL never produces cash, never produces inaction, and is never a veto.
`HALT` (exit 2) → nothing executes; halt path, not `--log`. **The LLM cannot argue
with this file.**

**FORBIDDEN:** Phase 3 has no LLM. Do not summarize it, re-run it "with judgment",
or ask a model whether it agrees.

---

## Phase 4 — Korean report + logging · **script numbers, LLM prose**

Run, in order — **all three steps, every run** (`state/final_plan.json` is
validate.py's stdout saved verbatim; after a validate HALT use the halt path instead):
```
1. python3 scripts/report.py --log --baseline state/baseline_plan.json --final state/final_plan.json [--deposit USD] [--withdraw USD]
2. python3 scripts/report.py --header
3. SEND the Korean report to Slack (see §Slack delivery below). The run is NOT
   complete until this step has executed.
```

`report.py` appends **exactly one `portfolio_mark` line per run-date (UTC) unless
forced — including no-ops — plus one line per order / override / satellite trade /
veto / shadow pick — and one `override_rejected` line when validate FAILed an
OVERRIDE** — to `state/recommendations.jsonl` (in the skill directory, **never**
under `reports/`). Every record is validated before anything is written; a malformed
input exits **2** with nothing appended and `last_run.json` untouched. It then
computes the mandatory header block. A no-op run logs a `noop` line whose reason is
the residual reason when there is one.

**Duplicate run → exit 4.** If the ledger already holds a `portfolio_mark` for
today's UTC date, `--log` appends **nothing**, leaves `last_run.json` untouched,
explains why on stderr, and exits **4**. A duplicate is **not a halt**: do not run
`--halt`, and **do not retry blindly** — the retry is refused identically. Decide:
if the earlier run already delivered today's plan, stop and say so in the reply; if
this run genuinely must supersede it (e.g. the portfolio input was wrong), re-run
with `--force-relog --reason "<why>"` — **`--force-relog` without a non-empty
`--reason` is refused (exit 2, nothing appended)**; the reason is stored on the mark
(`relog_reason`). The new mark carries `supersedes_same_date: true`, and scoring
counts only the **last** run of a date either way; the superseded run's order /
override / `override_rejected` lines stay listed in the header
(`중복_기록_제외_라인`), because they may have been executed. On a forced re-log,
`--deposit/--withdraw` are **in addition to** the day's earlier declarations (the mark
records the running `day_flow_total_usd`): **omit them to keep the earlier run's**. A
re-log whose `--deposit/--withdraw` equals a flow already declared that day (within the
near-duplicate tolerance: the larger of `config.ADJ_DUP_ABS_USD` and
`config.ADJ_DUP_REL` of the amount) is **refused — exit 4, nothing appended** — because
it is almost always the same transfer typed twice. Only if it genuinely is a second,
separate transfer, add **`--additional-flow`** (the user says so; the LLM never
assumes it). A HALT final plan (`final_plan.source: "HALT"`) is refused by `--log`
(exit 2): it is logged with `--halt`, never as a run.
The date is the UTC date, so a run at 08:59 KST and one at 09:01 KST are different
dates.

**External cash flows are not performance.** Pass a KRW deposit as `--deposit USD`
and money taken out as `--withdraw USD`; the mark records the signed
`external_flow_usd`. `report.py` also computes the flow the ledger itself implies
since the previous mark (`implied_flow_usd`: the change in cash not explained by
share-count changes, each valued at the average of the two marks' per-share prices);
a gap from the declared day total larger than `config.UNEXPLAINED_FLOW_WARN_PCT` of
the book is recorded as `unexplained_flow_usd` and surfaced in the header
(`미확인_입출금_의심`, which also lists marks where the ledger implies a flow nobody
declared) — a warning, not a block.

**Declared flows are reconciled, and verified only when corroborated.** At read time
every declared flow (a mark's `--deposit/--withdraw` day total, and every
`cash_flow_adjustment`) is checked against that implied flow; all declared flows
landing on the same mark are checked together (declared total D, implied flow I,
deviation D − I). Being *close* is not enough — a group is **verified only when all
three legs hold**, in ledger order:
1. **absolute:** the deviation is within `config.UNEXPLAINED_FLOW_WARN_PCT` of that
   mark's book;
2. **relative to the claim:** the deviation is within `config.FLOW_VERIFY_MAX_REL_DEV`
   of the declared amount — an implied flow near zero cannot verify a non-trivial
   declaration;
3. **cumulative:** the running sum of the deviations of every verified group so far,
   plus this one, stays within `config.FLOW_VERIFY_CUM_DEV_MAX_PCT` of that mark's book
   — many small deviations cannot add up to a large one.
Each entry in `입출금_내역` carries `verified`: `true` (the ledger corroborates it),
`false` (contradicted, not corroborated, over the cumulative cap, or unverifiable
because an adjacent mark lacks cash/positions — `note` names which), or `null` (no
effect on the TWR: received by the first mark, or no mark receives it). One fake
entry on a mark makes the whole group unverified, genuine flows on that mark included.
Three headline returns follow: `누적_수익률_pct` applies every declared flow;
`누적_수익률_검증입출금만_pct` drops the unverified ones; `누적_수익률_보수적_pct` is the
lowest of those and a **conservative curve** in which no unverified or undeclared flow
can help — an unverified group counts as the largest inflow any source claims (zero,
the declared total, or the ledger-implied flow), and an undeclared implied inflow
above the absolute tolerance counts as a deposit. The conservative figure is what the
kill trigger and the board-reconvene trigger read (`LLM_레이어_기여_보수적_pp`), so a
fake, a poisoned genuine deposit, or an undeclared deposit can never disarm them; it
may sit *below* the other two. The implied flow also books dividends, fees, FX and
interest inside the account as "flow" — reconciliation is evidence, not proof.

A past flow that was never declared is corrected by **appending**, never by editing
the ledger:
```
python3 scripts/report.py --adjust-flow --effective-date YYYY-MM-DD --flow-usd <signed USD> \
    [--flow-krw <memo>] [--basis ledger_implied|declared] --reason "<why>"
```
It attaches to the first mark on/after the effective date, and stderr prints its
reconciliation verdict. Refusals: exit 5 on tamper evidence (a broken chain / anchor,
**or** inside the sticky tamper window, **or** a torn final line), exit 4 on an identical **or near-identical**
repeat — same effective date, amount within the larger of `config.ADJ_DUP_ABS_USD`
and `config.ADJ_DUP_REL` of the amount, also against a flow
already declared on that date's mark — unless `--force-relog`; exit 2 on bad input,
no receiving mark, or an effective date **on or before the first mark** (a flow before
the first valuation cannot change the TWR, so there is nothing to correct). An
adjustment can never be removed, only offset by another one, and every one is counted
in the header (`입출금_보정_건수`) and listed with its verdict (`입출금_내역`), because a
fake flow would flatter the return. **Only the user supplies a flow amount**; the LLM
never invents one.

**The Korean report MUST open with the BOTTOM-LINE ACTION block, then the header.**

The very first thing in the report — line 1, above everything, before the
self-grade header — is the concrete action the human should take, stated as an
instruction, not a summary. It answers *"그래서 내가 지금 뭘 하면 돼?"* in one glance.
Keep it to 1–3 lines. Name the actual order(s) to place (or explicitly "오늘은
아무것도 하지 마세요"), and, when the run is gated on a pending event (deposit landing,
earnings, regime), name that next trigger. It restates the executed plan for the
human — it may not invent a decision the pipeline did not make (no new orders, no
regime call, no allocation the validator did not pass).

```
👉 오늘의 최종 행동: <구체적 지시 — 예: "QQQM 1주 매수 (정수 주만, 현금이 허용하는 만큼까지)" 또는 "NVDA 전량 매도, 대금은 코어(QQQ/QQQM)로" 또는 "아무것도 하지 마세요">
   다음 트리거: <있으면 — 예: "며칠 뒤 비상금 재입금 시 알려주면 QQQ/QQQM 정수 주 배분 계산">
```

Orders are named in **whole shares** exactly as the plan states them — never a
fraction, never a USD amount to "spend".

**Immediately below the action block, the mandatory self-grade header (extended in
v5.1 with the shadow line, in v5.1.1 with the integrity lines):**

```
📊 누적 수익률(TWR): X.XX%   |   QQQ 벤치마크: Y.YY%   |   vs QQQ: ±Z.ZZ%p
⚙️ 기계적 베이스라인: B.BB%   |   LLM 레이어 기여: ±L.LL%p   (음수면 LLM이 돈을 잃고 있다는 뜻)
🎯 적중률 vs QQQ: H%  (다트판 기준선 D%)
🧮 보수적 수익률: C.CC%  (미검증 입출금 N건 제외 · 킬 트리거/보드 재소집 기준)      ← X와 다를 때만
💸 입출금 내역: <입출금_내역 그대로: effective_date · amount_usd · verified · note>  | 없음
👻 섀도우 적중률 vs QQQ: S%  (표본 graded/total, 미성숙 U건, 평균 초과 E.EE%p, 실전 승격 가능: 예/아니오)
🛡️ 오버라이드: 적중률 O · 거부 R건 · 미채점 G건
🔒 원장 변조: 없음 | 감지 (최초 T1, 정지 해제 T2, 사유)   |   복구 이력: 없음 | K건 (removed_bytes · reason)
📡 가격 데이터: 정상 | 비정상   |   킬 트리거: 예/아니오/unknown   |   보드 재소집: 예/아니오/unknown
```

Source keys, one per slot, **all from `report.py --header` output, nothing else**:
X `누적_수익률_pct` · Y `QQQ_누적_pct` · Z `vs_QQQ_pp` · B `기계적_베이스라인_누적_pct` ·
L `LLM_레이어_기여_pp` · H `적중률_vs_QQQ` · D `다트판_기준선` · C
`누적_수익률_보수적_pct` · N `미검증_입출금_건수` · 입출금 `입출금_내역` · S
`섀도우_적중률_vs_QQQ` · graded/total `섀도우_표본수` · U `섀도우_미성숙_건수` · E
`섀도우_평균초과수익_20d_pp` · 승격 `실전_승격_가능` · O `오버라이드_적중률` · R
`오버라이드_거부_건수` · G `오버라이드_미채점_건수` · 변조 `원장_변조_감지` /
`원장_변조_최초감지` / `원장_변조_정지_해제` / `원장_변조_사유` (+ `원장_꼬리_손상`,
`원장_미완료_커밋_복구대기`) · 복구 `원장_복구_이력` · 가격 `가격_데이터_정상` ·
킬 `킬_트리거_발동` · 보드 `보드_재소집`. A null value prints as "없음" / "—", never as 0.
The 🧮 line is printed whenever C differs from X; when they are equal it may be
omitted (N is then still visible on the 💸 line as the `verified: false` entries).

**Three rules for these lines, because each was a way to mislead:**
- **The flow list is quoted as-is.** Print `입출금_내역` entry by entry exactly as the
  script emits it — including `verified: false` and its `note`. The LLM does not
  merge, net, drop, re-date, relabel or "explain away" an entry, and never states that
  a flow is verified when the script says it is not.
- **`"unknown"` is not `false`.** `킬_트리거_발동` and `보드_재소집` read `"unknown"`
  whenever `가격_데이터_정상` is false, for any reason — the benchmark series missing,
  a valuation-price lookup failing, or the scorecard failing to grade (nothing can be
  matured without it). Print "unknown" and say the trigger could not be evaluated this run; never
  report it as "not armed".
- **`가격_데이터_정상: false` is reported, not smoothed over.** When it is false, the
  benchmark-dependent numbers (QQQ, vs QQQ, hit rates, shadow grades) are missing or
  stale for this run — say so on the 📡 line and do not fill the gaps from memory or
  from a prior report.

Those header numbers come from `score_recs.py` / `report.py --header`. **The LLM may
not restate, round, re-derive, or soften them.** A skill that never grades itself is
how we got here. The action block sits *above* the header; it never replaces, hides,
or edits it — the self-grade stays fully visible on every run.

**The return is time-weighted** (`수익률_방식`): deposits and withdrawals are external
flows and never P&L, so a KRW deposit cannot flatter the curve and a withdrawal
cannot sink it. Directly under the header, print the script's `누적_손익_USD`,
`외부_입출금_순액_USD`, `입출금_보정_건수` and `중복_기록_제외_건수` (with the
`중복_기록_제외_라인` entries, if any), and any `미확인_입출금_의심` dates. All three
header curves (actual, QQQ, mechanical baseline) are flow-free and measured over the
same marks; the mechanical baseline compounds at the baseline's *executable* equity,
so whole-share cash drag is not booked as LLM loss.
**Dividend basis, stated honestly:** QQQ's curve is dividend-adjusted (total return)
while the mechanical baseline compounds on run-time closes (price return);
`배당_기준차_pp` is that gap, and the kill trigger compares against
`기계적_베이스라인_누적_TR상한_pct`, a total-return upper bound on the baseline (it can
only arm the trigger earlier). `기계적_베이스라인_누적_pct` and `LLM_레이어_기여_pp`
stay price-basis.

Report body: regime + why (the SMA line), the executed orders, satellite state,
vetoes with URLs, and — whenever a satellite position exists — the **satellite
honesty clause**, verbatim in substance:

> "새틀라이트의 기대 알파는 0이며 거래비용 차감 후에는 음수입니다. 유지하는 이유는
> 수익이 아니라 검증 데이터입니다. 지난 N주 satellite vs QQQ: X%."

Also disclose, every run: cash %, the **unavoidable residual** (`불가피_잔여현금_USD` /
`불가피_잔여현금_pct`: cash no whole core share can absorb — reported, not a
decision), that the core carries **no stop** (overnight gap risk is accepted beta),
any active override with its expiry date, and any shadow picks logged this run —
labelled as paper (실제 돈이 아닌 기록용), with the script's levels, never as advice.

### Slack delivery — MANDATORY, PRE-AUTHORIZED, DO NOT ASK

The Slack DM **is the deliverable**. A run that computes a plan and does not deliver
it has not run; "nothing happened" being invisible was 84% of v4's runs, and a report
that stops in the agent's own transcript is that same defect wearing a nicer outfit.

- **Destination: a Slack DM to the user (self-DM), `channel_id = U0AD7V4SWD9`.**
  There is **no channel, no webhook, and no config entry** for this — do not go
  looking for one, do not conclude it is missing, and **do not ask the user which
  channel to use.** (Every prior version's report was a DM. The absence of a channel
  ID in `config.py` is not a misconfiguration; it is the design.)
- **Tool:** the Slack MCP send-message tool, called from the top-level agent thread.
- **Authorization:** invoking this skill IS the authorization to send. This step is
  pre-approved, standing, and needs no per-run confirmation. Do not draft-and-ask.
  Do not send a draft tool call. Send it.
- **Slack send happens in the top-level agent thread and nowhere else.** No subagent
  has, or will ever be given, Slack tools.
- If the send fails (auth, network, tool unavailable), say so explicitly in the reply
  and hand the user the full report text verbatim so the run is still delivered.

*Why this is spelled out to the point of tedium:* v5's original Phase 4 mentioned
Slack eight times and never once said **where to send it, with what, or that no
permission was needed.** Every mention was a *prohibition* (which subagent may not
send, which thread must). A fresh agent correctly read that negative space, found no
destination, and stopped to ask — turning a mandatory delivery into a blocked run.
Constraints on a step are not the same as the step.

**Ledger integrity.** `recommendations.jsonl` is an append-only sha256 hash chain
(each line carries `prev_hash` = sha256 of the prior line), **anchored** by a tip
hash + line count persisted to `state/last_run.json` every run. Phase 2 runs in the
top-level thread that holds Bash+Write, so a suspended or adversarial LLM could
otherwise rewrite the ledger to clear its own suspension. The anchor is what makes
deletion, truncation, and a fully re-linked forgery detectable — not just a naive
mid-file edit: if the ledger is empty/shorter than the anchored length, or its tip
hash ≠ the anchor, `validate.py` treats override privileges as **suspended
(fail-safe)** and the report header prints `원장_변조_감지: true`.
**Fail closed on a missing anchor:** a non-empty ledger whose anchor is absent,
unparseable, or has a null / malformed `ledger_len` / `ledger_tip_hash` is tampered.
Only an absent-or-empty ledger with no anchor is a genuine first run. (Deleting
*both* files still looks like a first run — see Scope below.)
**Tamper is sticky.** The first writer that sees a fresh tamper stamps
`ledger_tamper_since` / `ledger_tamper_last` into the anchor **and** `ledger_tamper:
true` onto every line it appends, records the exact chain breaks it saw
(`ledger_tamper_accepted_breaks`, each pinned by the hash of the line before it), then
moves the anchor to the new tip. A later write that sees **the same** breaks is not new
evidence — it neither re-stamps nor extends the window, so a single detection ends
after `config.OVERRIDE_SUSPENSION_DAYS`; any *different* break, or an anchor mismatch,
is fresh evidence and stamps again. From then on
the ledger reads as tampered — overrides suspended, `--adjust-flow` refused (exit 5) —
until `config.OVERRIDE_SUSPENSION_DAYS` after the latest detection, using whichever
file still carries the evidence (stripping the anchor's stamp does not clear the
flagged lines). A normal `--log` after a tamper still logs the run (the mark carries
the flag, stderr warns) but can never restore privileges. The header reports
`원장_변조_최초감지`, `원장_변조_정지_해제` and `원장_변조_사유`.
**Scope, stated honestly:** this is tamper-*evident*, not unforgeable. An adversary
who rewrites *both* the ledger *and* the `last_run.json` anchor in the same breath is
not caught — both files sit in the same writable `state/` dir. Closing that requires
an HMAC key (or append-only store) outside the writable state directory; it is a
known P2 hardening, deliberately not built, because the realistic threat here is an
LLM that edits its own scorecard, not a determined attacker with shell access — and
the single-file edit, which is what an LLM actually does, now fails closed.

**Append-only means every writer, including the scorer.** No script rewrites a byte
of an existing line. v5.0's nightly scorer rewrote and re-linked the whole ledger to
fill in override spreads and then re-pointed the anchor — which would have laundered
any tamper every night once the cron was installed. That path is deleted: matured
spreads are appended as `override_score` lines and merged on read. Every writer —
`--log`, and every append outside it (a `pipeline_halt`, a `cash_flow_adjustment`, an
`override_score`) — goes through one path (`commit_locked`): check the chain
**before** appending, stamp a fresh tamper sticky, append everything in one write,
then write the anchor atomically. The anchor always moves to the new tip, and a
legitimate append can still never turn a broken ledger into a valid-looking one,
because the detection is recorded sticky in both files. Every writer holds the same
exclusive ledger lock from the check through the anchor write, so the nightly scorer
and an interactive run cannot fork the chain or leave a stale anchor. Readers see
an *effective* view: for each date only the last run counts, while halts,
adjustments and scores are never dropped. Nothing is ever deleted to get there.

**Crash safety.** Before every append, the writer records a **pending intent**
(`state/ledger_pending.json`: the anchored base tip/length, the new tip/length, and the
anchor it is about to write). If the process dies after the append but before the
anchor write, the next writer finds the ledger ending exactly at the intent's new tip
over the anchored base, **completes the commit** (writes that anchor) and carries on;
readers see `원장_미완료_커밋_복구대기: true` meanwhile, not tamper. A ledger that does
not match both the anchor and the intent is still tamper. If the intent itself cannot
be written (e.g. a read-only `state/`), nothing is appended (exit 6). Every reader
decodes the ledger with replacement characters, so a torn byte never crashes a
command; a corrupted line reads as not intact instead.

### Ledger recovery — the exact procedure for each benign fault

- **Torn final line** (power loss mid-append: the last line is not valid UTF-8 / JSON,
  usually unterminated). `--header` still works and shows `원장_변조_감지: true` with
  `원장_꼬리_손상` and a `TORN_TAIL` reason; validate treats the ledger as not intact
  (overrides suspended); **every writer refuses** (`--log` / `--halt` / `--adjust-flow`
  exit 5, `score_recs.py --score` exit 5, nothing appended) — appending after garbage
  would chain onto it. Fix, once, explicitly:
  ```
  python3 scripts/report.py --repair-torn-tail --reason "<what happened: e.g. power loss during --log>"
  ```
  It drops **only** that final line, saves its exact bytes to
  `state/ledger_torn_tail.<UTC>.bin`, and appends a `ledger_repair` line (bytes,
  sha256, copy name, reason) through the normal tamper-checked writer. It is the
  **single documented exception to append-only** and refuses (exit 2, nothing touched)
  when the final line is a complete JSON object. It never clears a real tamper: if the
  lines before the torn one are truncated, re-linked or edited, the repair's own write
  detects and stamps it sticky. Then re-run the step that failed (for `--log`, the same
  command; the torn run never landed). Every repair is listed in the header
  (`원장_복구_이력`) and must be reported on the 🔒 line. A run whose batch was torn
  after some of its lines landed still reads as tampered after the repair — report
  it; do not hand-edit.
- **Anchor write failed after the append** (crash, full disk): nothing to do — the next
  writer completes the commit from the pending intent. Note that the run DID land, so
  re-running the same `--log` exits 4 (duplicate); that exit is the confirmation, not a
  failure. `ledger_pending.json` stays until any writer runs (e.g. `--header` does not
  write; the nightly `--score` or the next `--log` clears it).
- **A flow declared on the wrong run** (e.g. `--deposit` passed one run before the cash
  actually appeared in the portfolio): the conservative return can drop by the full
  amount until corrected. Repair by appending two adjustments — the negative of the
  flow on the mistaken date and the flow again on the date the cash arrived — with
  `report.py --adjust-flow --effective-date <date> --flow-usd <±amount> --reason "..."`.
  Both should reconcile as verified.
- **Read-only / unwritable `state/`** (exit 6 on `--log` / `--halt`): nothing was
  appended; fix the permissions and re-run. The header still prints (the scorecard is
  simply not cached).
- **A tamper detection** (`원장_변조_감지: true` with a non-`TORN_TAIL` reason): there is
  no repair. The window ends on its own `config.OVERRIDE_SUSPENSION_DAYS` after the
  detection; until then only CONFIRM_BASELINE can PASS. Report it; never edit either
  file to "fix" it.

Line types: `portfolio_mark` (+ `external_flow_usd`, `withdraw_usd`,
`day_flow_total_usd`, `implied_flow_usd`, `last_bar`, `benchmark_px`,
`baseline_eq_pct`, `core_pct`, `execution_residual`, `unexplained_flow_usd`,
`supersedes_same_date`, `relog_reason`), `order` (+ `sleeve`, `whole_shares`), `noop`,
`override` (+ `baseline_equity_pct`, `realised_equity_pct`), `veto`, `pipeline_halt`,
new in v5.1 `shadow_pick`, `cash_flow_adjustment`, `override_score`, new in v5.1.1
`override_rejected`, and new in v5.1.2 `ledger_repair`. Every line carries `logged_utc` and `prev_hash`; a line appended
while a fresh tamper is detected also carries `ledger_tamper: true`.

**Continuity:** the next run receives `state/last_run.json` — structured positions,
regime, targets, active override, satellite stops, the ledger anchor, and (v5.1)
`core_tickers`, `prices` and `execution_residual`. **Never report prose.** Do not read
`reports/advisor/*.md` into any prompt. That archive-as-few-shot loop is what made
v4.1's merged fixes dead letter for seven consecutive runs.

**FORBIDDEN (Phase 4):** producing any number not emitted by a script; omitting the
bottom-line action block or the self-grade header, or letting the action block
replace/hide/edit the header; omitting a losing trade or a failed override from the log; writing the log
anywhere under `reports/`; editing, deleting or re-linking any existing ledger line
(a past error is corrected by appending); inventing an external flow amount;
paraphrasing, netting or dropping entries of `입출금_내역`; reporting an `"unknown"`
trigger or `가격_데이터_정상: false` as if it were a clean false; running `--log` on a
validate HALT; hand-editing `final_plan.json`;
blindly retrying a `--log` that exited 4; **ending the run without sending the Slack DM**; **asking
the user for a channel, for permission to send, or whether to send** (the destination
is fixed and the authorization is standing — see §Slack delivery).

---

## Bear-market behavior — mechanical, with a whipsaw guard

- **Trigger:** QQQ **monthly close** below the regime SMA (`config.REGIME_SMA_MONTHS`) ⇒ `DEFENSIVE`.
- **Action:** core de-risks toward `config.TARGETS["DEFENSIVE"]` (the nearest
  whole-share sale across QQQ/QQQM that the next run would not buy back — so the book
  lands **above or below** the DEFENSIVE equity target by up to one core lot, see
  Phase 0 SELL; no core BUY in the same plan. A cash-heavy DEFENSIVE book instead
  BUYS up toward the target); remainder to the
  KRW cash-equivalent (`config.DEFENSIVE_CASH_EQUIV`); **satellite force-closed**;
  override channel restricted to **re-risking only**.
- **Re-entry:** monthly close back above the SMA ⇒ back to `TREND` weights.
- **Why not 0% equity:** a full exit lost to buy-and-hold in six of eight bull years
  post-publication. A partial de-risk bounds the whipsaw cost while still roughly
  halving max drawdown. Expected outcome in a −20% year: **≈ −10 to −13% with 3–5
  trades.**
- **Whipsaw guard:**
  (a) **completed monthly closes only** — `core.py` drops the running month's
      partial bucket before the SMA sees it, so a deposit- or shock-day run
      evaluates the identical regime as the month-start run. The 7/08 incident
      (a daily stop selling the core two days before a new high) is impossible
      by construction;
  (b) the drift band suppresses micro-rebalances;
  (c) `config.MIN_MONTHS_BETWEEN_REGIME_FLIPS` — a minimum interval between flips.
- **Accepted cost, stated in the report:** v5 will lag a V-shaped recovery by up to
  a month. **That is the insurance premium.** It is not a bug and it is not to be
  "fixed" by adding a faster signal.

---

## Weekly satellite check (`--weekly`)

Phase 0 (stops + prices) → stop-check → Phase 4 logging **only**.

- `core.py --weekly` emits orders **only** on an ATR-stop breach — it compares
  today's price to the stop persisted in `state/last_run.json` by the *previous*
  run (a stop recomputed from today's close could never be breached). It does not
  evaluate the regime and does not rebalance drift.
- Exits for an elapsed min-hold with a dead catalyst, or for a Phase 1 veto, are
  proposed at the next **full** run and gated by `validate.py`
  (`SATELLITE_MIN_HOLD_VIOLATION` fires on any earlier LLM-initiated satellite SELL
  that has neither a stop breach nor a dated veto behind it).
- **Do not re-litigate the thesis.** A held name's conviction may **not** be
  decremented in the absence of a *new dated event*. The v4 per-run confidence tax
  ("if you cannot rebut the bear case, downgrade confidence") is deleted: it turned
  every run into a fresh adversarial trial of a position that had done nothing wrong.
- No new entries on a weekly run. No regime evaluation on a weekly run. No override
  on a weekly run (`OVERRIDE_IN_WEEKLY`; the only legal decision is CONFIRM_BASELINE).
  A weekly run emits no satellite levels and accepts no shadow picks (`SHADOW_WEEKLY`); a
  stop-breach sale's proceeds go to the core in whole shares, or stay as residual.

---

## What v5 DELETED (do not reintroduce under a new name)

1. **Daily cadence.**
2. **"When in doubt, reject." / "or is cash wiser?" / "Patience over activity" /
   "Capital preservation above all" / "Never lose money."** These are a measured
   loss-aversion payload (a risk-averse persona halves an LLM's effective risk
   tolerance; a trade-aversion cue collapses activity to ~0). "Patience over
   activity" was cited verbatim as a rejection rationale on a day the rejected name
   went on to beat the index.
3. **All sentiment gates, floors, caps, and floats.** The v4 cap was *below* its own
   BUY floor — arithmetically self-deadlocked. The 28 sentiment-rejected names then
   returned +3.85% (+1.85pp vs QQQ).
4. **The OVERBOUGHT / at-resistance / margin-of-safety veto.** The single most
   destructive clause in the rulebook. Demoted to a satellite *sizing* input.
5. **The R/R ≥ 2.0 hard gate.** A fictional threshold applied to LLM-invented
   targets. Now `config.SATELLITE_MIN_RR`, satellite-only, code-checked.
6. **`market_regime` as an LLM output, and all RISK_ON/NEUTRAL/RISK_OFF gating.**
   Anti-predictive: RISK_OFF was followed by a QQQ *gain* 6 of 9 times. It was
   5-day-lagged momentum in a macro costume, wired to the sizing dial.
7. **The commodity/energy research agent and its universe** (FCX/XLE/XOM/CVX/SCCO).
   ~Zero net return minus costs; 43% of all rejections.
8. **Social sentiment** (Reddit/StockTwits/X, `social_buzz`, `INSUFFICIENT_DATA`).
9. **Phase 3 and Phase 4 as LLM judges.**
10. **`subagent_type: "general-purpose"` anywhere in this skill.**
11. **Prior-report prose in context; the report archive as few-shot.**
12. **`NO_VIABLE_ALTERNATIVE`; the ">60% cash" trigger; the 40–60% dead zone.**
13. **config.py / SKILL.md double bookkeeping.**
14. **Cash as a choosable allocation.** Removed from every schema. This is the
    fatal-defect fix, and every other item on this list is downstream of it.

---

## Accountability & pre-committed restructuring triggers

`score_recs.py` (nightly cron, installed by `scripts/install_cron.sh`) grades every
*effective* logged decision (superseded same-date runs excluded) at +1/+5/+20d
against a **matched QQQ window**, and maintains two cumulative curves: **actual
(time-weighted, external flows removed) vs 100% QQQ from day 0**, and **actual vs the
un-overridden mechanical baseline**. The second is the LLM layer's isolated P&L. The
hit rate grades **satellite** orders only (a core BUY graded against the core is
excess zero by construction and would read as a miss forever); shadow picks are
graded separately and never mixed into it. The scorer only ever **appends**
(`override_score`); a night skipped by laptop sleep is harmless because every run is
a full, idempotent recompute. Satellite min-hold and round-trip history, and the
override list, include superseded same-date runs (an order that may have executed
still counts); a verbatim same-date repeat counts once.

**Torn / unreadable ledger → exit 5.** `score_recs.py --score` appends no
`override_score` line onto a torn final line; it exits 5 so the cron log shows it
(§Ledger recovery).

**Price outage → exit 3, nothing written.** If the benchmark price data cannot be
loaded, `score_recs.py --score` grades nothing, writes no scorecard (no all-empty
cache that would later read as real), appends nothing, and exits **3** — visible in
the cron log. `--track-record` / `--header` in the same condition report
`price_data_ok: false` (header `가격_데이터_정상`) and the maturity-gated triggers as
`"unknown"`; §Phase 4 says how to report both.

**Cron.** `bash scripts/install_cron.sh` is a **dry run by default**: it prints the
exact crontab line (absolute `python3`, `flock -n` on a lock in `state/`, output
appended to `state/score_cron.log`) and what would change, and touches nothing.
Every path in the line is POSIX-shell-quoted for cron's `/bin/sh`; a path containing
`%` (which cron turns into a newline) is refused. The line also **rotates the log**
before each run: a `score_cron.log` over `config.CRON_LOG_MAX_KIB` is moved to
`score_cron.log.1` (one generation kept), so the log stays bounded. The installer reads
that cap from `config.py` through `score_recs.py --cron-log-max-kib` and refuses to
print or install a line if it cannot.
`--install` is idempotent (keeps every other crontab line, replaces only the line
carrying the marker `# us-stock-advisor-score`); `--uninstall` removes that line. The
schedule lives in that script and nowhere else. Installing is a deliberate human /
top-level-agent action, never a test and never a subagent.

Pre-committed, in `config.py`, so that no future run can argue its way out:

- **Kill the satellite + override channel** if, after `config.KILL_EVAL_TRADING_DAYS`
  of forward, post-cutoff data, the LLM-layer spread vs the mechanical baseline is
  below `config.KILL_SATELLITE_IF_SPREAD_BELOW_PP` (header `킬_트리거_발동`; computed
  on the **conservative** return against the baseline's total-return bound —
  `LLM_레이어_기여_보수적_pp` — so neither an unverified flow nor the dividend basis
  can keep it from arming), or override hit-rate vs baseline is below
  `config.KILL_MIN_OVERRIDE_HITRATE` on a sufficient sample — at least
  `config.OVERRIDE_HITRATE_MIN_DECIDED` hit-or-miss grades (header
  `오버라이드_권한_정지`, a separate flag). Without benchmark prices the trigger reads
  `"unknown"`, never false. Result: v5
  degrades to `core.py` plus a monthly LLM-written report with **zero decision
  authority**. That end state is a success, not a failure — it was entered on
  evidence.
- **Expand the satellite** only if its picks beat the matched-window benchmark more
  often than `config.EXPAND_SATELLITE_IF_HITRATE_ABOVE` with positive mean excess
  over a full evaluation window.
- **Graduate the satellite to real money** only on the shadow evidence in §Shadow
  track (`config.SHADOW_GRADUATION_MIN_PICKS`, `config.SHADOW_GRADUATION_MAX_PVALUE`,
  `config.SHADOW_GRADUATION_MIN_MEAN_EXCESS_PP`, against
  `config.DARTBOARD_BASE_RATE`). The script reports it; a human flips
  `config.SATELLITE_REAL_MONEY_ENABLED`. Nothing in the pipeline does.
- **Reconvene the board** if the mechanical core itself trails buy-and-hold QQQ by
  more than `config.BOARD_RECONVENE_IF_CORE_TRAILS_QQQ_PP` over 12 months (trend
  whipsaw exceeding its insurance value), or after the first DEFENSIVE episode ends.
  What the script computes (`보드_재소집`) is narrower, stated as it is: the
  **conservative** actual return vs QQQ, cumulative since the first mark, armed only
  once the window is mature (`config.KILL_EVAL_TRADING_DAYS`) — `"unknown"` without
  benchmark prices. The 12-month framing and the post-DEFENSIVE reconvene are human
  checks; no script arms them.

**If it cannot be scored, it does not get to make calls.** v5 does not run at all
until `recommendations.jsonl` and `score_recs.py` are in place.

---

## Honest expectations (say this to the user, do not soften it)

- The core is **beta**, not alpha: ~6–7%/yr real, long-run, with real drawdowns.
- The trend overlay is **drawdown insurance**, not return enhancement (~0 CAGR
  effect; roughly halves max drawdown; costs a few trades a year and lags V-shaped
  recoveries).
- The satellite's honest expected alpha is **zero, and negative after costs.** It is
  kept small and sunset-claused because its product is *forward evidence*, not
  return. The one statistically real capability measured in v4 was **refusal**
  (69% of its rejects underperformed QQQ) — and refusal is only worth anything if
  the freed capital goes into the index, which is now the only place it can go.
- No independent study shows an LLM discretionary picker beating buy-and-hold net
  of costs. The broadest one (FINSABER, 100+ symbols, 20 years) finds LLM strategies
  are **too timid in bulls and too aggressive in bears** — precisely the v4 failure,
  reproduced independently. v5's design concedes that finding rather than arguing
  with it.
- **Lot rounding is real at this account size.** One core share is a large slice of
  the book, even in QQQM. The whole-share book therefore carries an unavoidable cash
  residual of up to one lot, the mechanical baseline realises less than its ideal
  target, a DEFENSIVE de-risk usually leaves more equity than the DEFENSIVE target
  (undersell beats a sell the next run re-buys), and most de-risk overrides are
  infeasible (§Override item 6). None of that
  is a defect to be argued around; it shrinks as the account grows.
- The shadow track will take many months to produce a verdict at monthly cadence.
  The honest prior (see the FINSABER point above) is that it never graduates.
- Tax/cost note (KIS, KRW-funded): buy-and-hold under the 250만원 양도세 allowance is
  ~zero CGT at this account size. The churn v5 deleted was the only thing that could
  create a tax bill or repeatedly burn the FX spread.

---

## Install map

| File | Path |
|---|---|
| this skill | `~/.claude/skills/us-stock-advisor/SKILL.md` |
| single source of truth | `~/.claude/skills/us-stock-advisor/scripts/config.py` |
| Phase 0 authority | `~/.claude/skills/us-stock-advisor/scripts/core.py` |
| price ground truth | `~/.claude/skills/us-stock-advisor/scripts/fetch_indicators.py` |
| Phase 3 gate | `~/.claude/skills/us-stock-advisor/scripts/validate.py` |
| accountability | `~/.claude/skills/us-stock-advisor/scripts/score_recs.py` |
| logging + header | `~/.claude/skills/us-stock-advisor/scripts/report.py` |
| nightly scorer cron installer (dry run by default) | `~/.claude/skills/us-stock-advisor/scripts/install_cron.sh` |
| offline test suite + acceptance runner | `~/.claude/skills/us-stock-advisor/tests/` (`run_acceptance.sh`) |
| decision log | `~/.claude/skills/us-stock-advisor/state/recommendations.jsonl` |
| structured continuity | `~/.claude/skills/us-stock-advisor/state/last_run.json` |
| **read-only researcher** | `~/.claude/agents/stock-research-readonly.md` |
| **report destination** | Slack **DM to the user**, `channel_id = U0AD7V4SWD9` (no channel, no webhook, no config entry — by design) |

Nightly cron: `bash ~/.claude/skills/us-stock-advisor/scripts/install_cron.sh` (dry
run; prints the line) → `--install` to apply. Do not hand-write a cron line here or
anywhere else; the schedule has one home.

## Acceptance tests (run on EVERY edit to this file)

One command runs the automated ones offline, in a throw-away state dir (it refuses
to start if state would resolve to the live `state/`, and fails if the live ledger or
anchor changed during the session):
`bash tests/run_acceptance.sh` (= `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/ -q`
plus the greps for 1, 7 and 9). Network tests are opt-in via
`US_ADVISOR_NETWORK_TESTS=1`.

1. `grep -nE 'subagent_type\s*[=:]\s*"?general-purpose' SKILL.md` → no hits (an
   actual invocation; the two prose mentions — the "never" instruction and the
   DELETE-list entry — are intended and do not count; the grep prints the DELETE-list
   line, and that is its only allowed hit).
2. A deliberately adversarial Phase 1 prompt cannot write a file, run Bash, or send
   Slack — verified by red-team run, not by reading the prose.
3. A Phase 2 proposal allocating to cash is rejected by `validate.py` and the
   baseline executes unmodified.
4. A proposal with a stale/missing/unfetchable override catalyst is rejected.
5. `core.py --backtest` walks the archived window bar-by-bar through
   `compute_regime` + `build_plan` with costs applied, and meets the acceptance bars
   in `config.BACKTEST_*` (`acceptance_regime` and `acceptance_return` both true;
   the return is far above what v4.1 realized). `core.py --backtest-fixture bear`
   flips DEFENSIVE, stops the satellite out on its ATR stop, force-closes the
   satellite at the flip when stops are disabled, and re-enters — `acceptance_bear`
   true.
   *Note, honestly:* the mechanical core did **not** sit in TREND for the whole
   archived window. March's monthly close was below the SMA, so it was DEFENSIVE for
   the April sessions and returned less than buy-and-hold QQQ over that window. That
   gap is the insurance premium, not a bug — and it is the number the backtest
   prints rather than the number the design would prefer.
   *v5.1:* the walk trades **whole shares** over both `config.CORE_TICKERS` (a BUY
   that does not fit in cash is skipped, never scaled into a fraction), still meets
   both bars, and ends holding integer share counts. Offline it runs on the cached
   history `tests/fixtures/history_6y.csv` via `--history-csv`.
6. Every run-date appends exactly one `portfolio_mark`, no-ops included (mark + one
   `noop`); a failed run appends a `pipeline_halt` and the anchor stays valid. A
   second `--log` on the same UTC date exits 4 and appends nothing (last_run.json
   byte-identical); `--force-relog` appends a superseding mark and scoring counts one
   run.
7. No gating threshold value appears in this file.
8. Every symbol in `config.py` is referenced by at least one script — verified by
   grep in CI (`tests/test_acceptance.py`; `SKILL_DIR` is exempt, it only builds
   `STATE_DIR`). A constant no script reads is v4's double-bookkeeping bug relocated
   into Python.
9. **Delivery is specified, not merely constrained.** `grep -n -i slack SKILL.md`
   must return, alongside the prohibitions, at least one line naming (a) the
   destination `U0AD7V4SWD9`, (b) the tool, and (c) the standing authorization. A
   skill that only ever says who may *not* send, and never says where to send, reads
   to a fresh agent as an unconfigured step — and it will stop and ask, which on a
   cron/headless run means the report is never delivered at all. Do not let the
   Slack mentions decay back into pure negative space.
10. **Whole-share dry run yields an executable integer order.** On the real book
    (QQQ 2 sh, cash 392.25, prices from a fixture) core → CONFIRM → validate PASS →
    `report.py --log` produces exactly `BUY QQQM 1`: integer shares, cost incl.
    `config.WHOLE_SHARE_PRICE_BUFFER_PCT` within cash, `execution.residual.unavoidable`
    true, and `events.cash_over_max` false on the post-trade book. Plus: no plan ever
    mixes core BUY and SELL lines (no conversion), never spends more than spendable
    cash, and a DEFENSIVE plan never mixes a core BUY with a core SELL (it sells an
    over-target book and buys up a cash-heavy one).
11. **Shadow isolation (metamorphic).** For CONFIRM, valid OVERRIDE and FAIL proposals
    alike, `verdict`, `violations`, `final_plan.orders` and
    `final_plan.final_allocation` are identical with and without `shadow_picks`; a
    shadow ticker in the allocation FAILs; a pick carrying any number is rejected.
12. **Append-only, every writer.** Across `report.py --log`, `--halt`,
    `--adjust-flow` and `score_recs.py --score`, the ledger before each step is a
    byte-identical prefix of the ledger after it, the anchor stays valid, and a
    tamper is never laundered: after one, the anchor moves to the new tip but the
    detection is stamped sticky (anchor + flagged line), `ledger_intact` stays false
    for `config.OVERRIDE_SUSPENSION_DAYS` and then lifts, and stripping the anchor's
    stamp does not clear it. A non-empty ledger with no usable anchor is tampered; an
    empty one with none is a first run.
13. **TWR ignores external flows.** A ledger with a deposit and a withdrawal (declared
    on the mark, or appended later as `cash_flow_adjustment`) yields the same
    `actual_cum_pct` as its flow-free twin. Same-date flows: a legacy mark (no
    `day_flow_total_usd`) that repeated a deposit on a forced re-log is counted once;
    a v5.1.1 re-log's `--deposit` adds to the day total, and a re-log that declares
    nothing keeps it; since v5.1.2 a re-log repeating a flow already declared that day
    is refused (exit 4) unless `--additional-flow`.
14. **The cron installer's default is harmless.** `install_cron.sh` with no argument
    only reads `crontab -l` (verified against a fake `crontab` on PATH) and changes
    nothing; the installed line quotes its paths and rotates the log.
15. **A stale baseline HALTs.** A `baseline_plan.json` older than
    `config.BASELINE_MAX_AGE_HOURS`, undated, or future-dated makes `validate.py`
    exit 2 with `final_plan.source` `HALT` and no orders; a fresh one does not.
16. **DEFENSIVE cannot be deepened by relabelling.** An override labelled `re_risk`
    whose whole-share execution lands below the baseline's realised equity FAILs
    (`OVERRIDE_DEEPENS_DEFENSIVE`); the allocation cannot keep a satellite in
    DEFENSIVE, cancel a stop breach, cut a held satellite without a declared SELL, or
    fund a BUY with cash it does not have; an override on a weekly baseline FAILs; and
    a CONFIRM that matches the baseline's own reference PASSes.
17. **Per-ticker freshness.** A stale held ticker is fatal (exit 3), a stale unheld
    QQQM gives a QQQ-only plan with a warning, a stale universe name gets no levels;
    no DEFENSIVE sell oversells into a next-run re-buy.
18. **Flows are reconciled, and the triggers read the conservative figure.** A declared
    flow the ledger contradicts is `verified: false` and cannot disarm the kill or
    board trigger; near-duplicate and on/before-first-mark adjustments are refused.
19. **Failed and legacy overrides are visible, not mis-scored.** A FAILed OVERRIDE
    appends one `override_rejected` line and never counts as executed; a core
    de-risk gets a real signed spread; a legacy core override is ungraded, not a miss.
20. **Price outage is visible.** With no benchmark prices, `score_recs.py --score`
    exits 3 and writes nothing, and the header reports `가격_데이터_정상: false` with
    `"unknown"` triggers (for any price failure, not only a missing series); an
    unmatured shadow pick is never tallied `OPEN`.
21. **Flows are corroborated, not merely close** (`tests/test_rereview_fixes.py`). On
    the live-ledger copy the historical deposit stays verified and the 07-23
    ledger-implied correction verifies; the re-review's one-fake-per-mark laundering
    cannot lift `누적_수익률_보수적_pct`; a near-zero implied flow cannot verify a
    declaration; many small over-declarations stop at the cumulative cap; poisoning a
    genuine deposit's group does not help; an undeclared implied inflow counts as a
    deposit in the conservative curve.
22. **Benign faults recover; tampers do not.** An anchor-write failure after an append
    is completed by the next writer (no tamper stamp); a torn final line (ASCII or a
    torn multi-byte character) never crashes a command, blocks every writer with
    exit 5, and is repaired only by `--repair-torn-tail` (exact bytes kept, a
    `ledger_repair` line appended); the repair does not clear a truncation, relink or
    mid-file edit; repeated writes after one detection do not extend its window.
23. **The gate refuses non-decisions.** A stale baseline HALTs even when the ledger is
    unreadable; an OVERRIDE equal to the baseline FAILs `OVERRIDE_NO_EFFECT`; a zero
    spread does not reset the streak and a re-logged override counts once; `--log`
    refuses a HALT plan (exit 2) and a re-log repeating the day's flow (exit 4)
    unless `--additional-flow`; a one-share DEFENSIVE book carries a drift warning; a
    core share cheaper than `config.MIN_ORDER_USD` never fires `cash_over_max` without
    an order; state files keep their permission bits.
