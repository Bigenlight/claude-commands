---
name: us-stock-advisor
description: 미국 주식 코어-새틀라이트 어드바이저. 결정론적 파이썬 코어(core.py)가 레짐·목표비중·주문을 전부 계산하고, LLM은 (a) 근거 기반 veto 추출과 (b) 비용을 지불하고 로깅되는 override 채널만 담당. 매월(+입금/이벤트 시) 실행, 누적 성과를 QQQ와 대조해 자기 채점. KIS API/실거래 없이 리서치·판단만.
version: 5.0.0
argument-hint: "<portfolio.json 경로 또는 현금(USD)+보유종목(ticker,수량,평단가)> [--deposit KRW입금액] [--weekly] [--event <사유>]"
allowed-tools: [Read, Write, Bash, Agent, WebSearch, WebFetch]
---

# US Stock AI Advisor v5 — Mechanical Core, LLM on Parole

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
| **Any day `cash_pct` > `config.CASH_MAX_PCT`** | full pipeline (idle cash is the deleted defect; it may not sit unlogged until month-end) |
| **Event: T−3 to a held/candidate name's earnings** | Phase 0 + Phase 3 only |
| **Weekly** | satellite stop-check ONLY (`--weekly`) — see §Weekly |
| **Any other day** | **nothing. Do not run this skill.** |

Both event triggers are computed by `core.py` and printed in `baseline_plan.json`
under `events` (`shock_moves`, `cash_over_max`) — they are detected in code, not
noticed by a human.

46 daily runs were 46 chances to find a reason to say no, plus a compounding
per-run confidence tax on every position already held. The thesis horizon is
multi-week; the decision cadence must not be shorter than the thesis.

**Retire the v4 daily orchestrator.** Any cron/orchestrator entry that invokes
this skill daily must be deleted or repointed to the monthly trigger before v5's
first run. A daily caller silently reinstates deleted cadence.

---

## Pipeline at a glance

```
Phase 0  core.py        DETERMINISTIC   → baseline_plan.json  (PRE-APPROVED ORDERS)
Phase 1  veto scan      LLM  (sonnet, stock-research-readonly, ×1–2)  → vetoes[]
Phase 2  execute/override LLM (opus, ×1)                              → proposal.json
Phase 3  validate.py    DETERMINISTIC   → PASS (proposal) | FAIL (baseline enforced)
Phase 4  report.py + LLM prose → recommendations.jsonl + Korean Slack report
```

Money-touching numbers are produced **only** in Phase 0 and checked **only** in
Phase 3. Both are Python. The LLM narrates, vetoes, and may file an override — and
that is the entire extent of its authority.

---

## Phase 0 — `core.py` · **DETERMINISTIC** · the authority

Run: `python3 scripts/core.py --portfolio <portfolio.json> --out state/baseline_plan.json`

**Inputs**
- yfinance daily OHLC, **bar-complete only** (today's partial bar is dropped;
  `auto_adjust` on) — see `config.DROP_PARTIAL_BAR`, `config.AUTO_ADJUST`.
- Account cash + positions (from the user's argument or the KIS balance script).
- `config.py`.
- `state/last_run.json` — **structured JSON only**, for regime-flip hysteresis.

**Decides (and nothing else in this skill may re-decide):**
- **Regime.** One term: QQQ **monthly close** vs its long-horizon SMA
  (length = `config.REGIME_SMA_MONTHS`, evaluated per `config.REGIME_EVAL`). `TREND` above, `DEFENSIVE`
  below. That is the whole model. It has 100+ years of out-of-sample evidence
  behind it (Faber) and it is not tuned on the last quarter.
- **Targets.** `config.TARGETS[regime]` → core % / satellite budget %.
  **Unused satellite budget auto-routes to the core.** Cash target is zero.
- **Orders.** Emitted only when |actual − target| exceeds
  `config.REBALANCE_DRIFT_BAND_PCT`. Fractional shares. Below
  `config.MIN_ORDER_USD` → no order.
- **Satellite stops** (ATR-based, satellite names only) and `days_to_earnings`
  per held/candidate name (from `Ticker.get_earnings_dates()`, a date — not a
  headline).

**Emits** `baseline_plan.json` with `"status": "PRE_APPROVED"`. This is not a
suggestion. It is today's order list, already approved, before any LLM has read
anything.

**The core carries no stop.** Deliberately. A daily SMA stop sold QQQ at 711.44 on
7/08 two days before it closed at 725.51. Left-tail gap risk on the core is
accepted beta, disclosed in the report, and insurable only by not being an equity
investor.

**Forbidden:** nothing. Phase 0 is the authority. If `core.py` exits non-zero,
**the pipeline halts** — no approved plan means there is nothing for the LLM to
execute, and "the script failed so I'll decide myself" is exactly the failure mode
this version exists to delete.
A halt is never silent: run `python3 scripts/report.py --halt "<reason>"`, which
appends a `{"type":"pipeline_halt"}` line to `recommendations.jsonl`; send the
*failure* (not a recommendation) to Slack, and re-arm the trigger for the next
session. "Nothing happened" being invisible was 84% of v4's runs.

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

**Inputs:** held tickers + any satellite candidates from `config.SATELLITE_UNIVERSE`.

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
3. **Satellite proposal** — within `config.SATELLITE_MAX_PCT` /
   `config.SATELLITE_MAX_NAMES`, from `config.SATELLITE_UNIVERSE` only. Each add
   needs a dated catalyst fresher than `config.FRESH_CATALYST_MAX_AGE_HOURS` with a
   fetchable URL, `rr >= config.SATELLITE_MIN_RR` (satellite-only; the core is never
   R/R-gated), and no earnings inside `config.EARNINGS_BLACKOUT_SESSIONS`.
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
- Inventing sentiment floats, conviction floats, or any number not produced by a
  script.
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
  "final_allocation": {"QQQ": 90.0, "NVDA": 10.0},
  "satellite": [{"ticker":"...","action":"BUY|HOLD|SELL","size_pct":0,
                 "rr":0,"catalyst_url":"https://...","catalyst_timestamp":"ISO8601",
                 "stop":0,"target":0}],
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

---

## The OVERRIDE channel — the burden-of-proof inversion, literally

Doubt now has a default, and the default is the index. The only way to deviate:

1. **Direction-limited.** An override may reduce equity by at most
   `config.OVERRIDE_MAX_EQUITY_REDUCTION_PCT` below the regime target. It may never
   raise satellite above budget, never lever above target, and never touch the SMA
   regime. **In DEFENSIVE the channel is re-risking ONLY** — the LLM may never
   deepen a de-risk. (That is how v4 turned a lagging fear label into
   buy-high/refuse-low.)
2. **Required fields, all code-validated:** `catalyst_description`, `catalyst_url`
   (must resolve — HEAD-checked by `validate.py`; content verified by the LLM phases
   via WebFetch), `catalyst_timestamp` (fresher than
   `config.OVERRIDE_CATALYST_MAX_AGE_HOURS`), `expected_cost_if_wrong_pct`,
   `qqq_forward_20d_if_i_am_wrong`. Missing, late, unfetchable, or future-dated
   catalyst ⇒ hard FAIL ⇒ **the baseline executes unmodified**.
3. **Auto-expiry** after `config.OVERRIDE_EXPIRY_TRADING_DAYS`. The baseline
   reasserts itself unless a *new* dated catalyst re-files. An override is a bet
   with a clock, not a new policy.
4. **Scored.** Every override is a line in `recommendations.jsonl`, graded at +5/+20d
   against the plan it overrode. The running override P&L prints in every report.
5. **Escalating cost.** After `config.OVERRIDE_SUSPEND_AFTER_N_BAD` consecutive
   overrides with negative spread vs baseline, `validate.py` **suspends override
   privileges** for `config.OVERRIDE_SUSPENSION_DAYS`. A counter in code, not a
   sentence in a prompt. During suspension the only legal output is
   CONFIRM_BASELINE.

There is no other lever. There is no "adjust", no "trim for prudence", no
"NO_VIABLE_ALTERNATIVE", no "reduce until clarity". Those are cash by another name
and cash is not a decision this skill can make.

---

## Phase 3 — `validate.py` · **DETERMINISTIC** · the gate

Run: `python3 scripts/validate.py --baseline state/baseline_plan.json --proposal state/proposal.json`

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
- every allocated ticker is **priceable by Phase 0** (core, a held name, or otherwise
  quoted); an unpriceable ticker whose order would silently become cash is a FAIL
- the **derived orders are re-checked against the intended equity** — a plan that is
  fully invested on paper but lands in cash on execution FAILs; gating the % while
  trusting the derivation is the hole this closes
- a satellite name whose allocation weight is new or increased must be a declared
  satellite BUY (it may not escape the satellite gates by living only in the allocation)
- equity ≥ regime target − active-override allowance; and ≤ target (no levering up)
- implied cash ≤ regime cash sleeve + operational float + allowance
  (**the >60%-cash trigger and its 40–60% dead zone are deleted**)
- satellite ≤ budget, ≤ max names, universe-restricted, force-closed in DEFENSIVE
- satellite BUY: R/R ≥ min, catalyst URL + fresh timestamp, earnings blackout clear
- single-**name** cap; **broad ETFs are uncapped, forever** (`config.BROAD_ETFS`)
- override schema complete, catalyst fresh + fetchable, direction legal,
  privileges not suspended
- `data_age_hours >= 0` (a negative age means a partial bar leaked → **hard abort**;
  in v4 this silently made the staleness check pass on exactly the worst runs)
- CONFIRM_BASELINE must equal the baseline (no silent edits under a confirmation)

**Verdict semantics:** `PASS` → the proposal is the final plan. `FAIL` → **the
pre-approved baseline executes unmodified.** FAIL never produces cash, never
produces inaction, and is never a veto. **The LLM cannot argue with this file.**

**FORBIDDEN:** Phase 3 has no LLM. Do not summarize it, re-run it "with judgment",
or ask a model whether it agrees.

---

## Phase 4 — Korean report + logging · **script numbers, LLM prose**

Run:
```
python3 scripts/report.py --log --baseline state/baseline_plan.json --final state/final_plan.json [--deposit USD]
python3 scripts/report.py --header
```

`report.py` appends **exactly one `portfolio_mark` line per run — including no-ops
— plus one line per order / override / satellite trade / veto** to
`state/recommendations.jsonl` (in the skill directory, **never** under
`reports/`). It then computes the mandatory header block.

**The Korean report MUST open with the header, in the first five lines:**

```
📊 누적 수익률: X.XX%   |   QQQ 벤치마크: Y.YY%   |   vs QQQ: ±Z.ZZ%p
⚙️ 기계적 베이스라인: B.BB%   |   LLM 레이어 기여: ±L.LL%p   (음수면 LLM이 돈을 잃고 있다는 뜻)
🎯 적중률 vs QQQ: H%  (다트판 기준선 39.8%)
```

Those numbers come from `score_recs.py`. **The LLM may not restate, round,
re-derive, or soften them.** A skill that never grades itself is how we got here.

Report body: regime + why (the SMA line), the executed orders, satellite state,
vetoes with URLs, and — whenever a satellite position exists — the **satellite
honesty clause**, verbatim in substance:

> "새틀라이트의 기대 알파는 0이며 거래비용 차감 후에는 음수입니다. 유지하는 이유는
> 수익이 아니라 검증 데이터입니다. 지난 N주 satellite vs QQQ: X%."

Also disclose, every run: cash %, that the core carries **no stop** (overnight gap
risk is accepted beta), and any active override with its expiry date.

**Slack send happens here, in the top-level agent thread, and nowhere else.** No
subagent has, or will ever be given, Slack tools.

**Ledger integrity.** `recommendations.jsonl` is an append-only sha256 hash chain
(each line carries `prev_hash` = sha256 of the prior line), **anchored** by a tip
hash + line count persisted to `state/last_run.json` every run. Phase 2 runs in the
top-level thread that holds Bash+Write, so a suspended or adversarial LLM could
otherwise rewrite the ledger to clear its own suspension. The anchor is what makes
deletion, truncation, and a fully re-linked forgery detectable — not just a naive
mid-file edit: if the ledger is empty/shorter than the anchored length, or its tip
hash ≠ the anchor, `validate.py` treats override privileges as **suspended
(fail-safe)** and the report header prints `원장_변조_감지: true`.
**Scope, stated honestly:** this is tamper-*evident*, not unforgeable. An adversary
who rewrites *both* the ledger *and* the `last_run.json` anchor in the same breath is
not caught — both files sit in the same writable `state/` dir. Closing that requires
an HMAC key (or append-only store) outside the writable state directory; it is a
known P2 hardening, deliberately not built, because the realistic threat here is an
LLM that edits its own scorecard, not a determined attacker with shell access — and
the single-file edit, which is what an LLM actually does, now fails closed.

**Continuity:** the next run receives `state/last_run.json` — structured positions,
regime, targets, active override. **Never report prose.** Do not read
`reports/advisor/*.md` into any prompt. That archive-as-few-shot loop is what made
v4.1's merged fixes dead letter for seven consecutive runs.

**FORBIDDEN (Phase 4):** producing any number not emitted by a script; omitting the
header; omitting a losing trade or a failed override from the log; writing the log
anywhere under `reports/`.

---

## Bear-market behavior — mechanical, with a whipsaw guard

- **Trigger:** QQQ **monthly close** below the regime SMA (`config.REGIME_SMA_MONTHS`) ⇒ `DEFENSIVE`.
- **Action:** core de-risks to `config.TARGETS["DEFENSIVE"]`; remainder to the
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
- No new entries on a weekly run. No regime evaluation on a weekly run.

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

`score_recs.py` (nightly cron) grades every logged decision at +1/+5/+20d against a
**matched QQQ window**, and maintains two cumulative curves: **actual vs 100% QQQ
from day 0**, and **actual vs the un-overridden mechanical baseline**. The second is
the LLM layer's isolated P&L.

Pre-committed, in `config.py`, so that no future run can argue its way out:

- **Kill the satellite + override channel** if, after `config.KILL_EVAL_TRADING_DAYS`
  of forward, post-cutoff data, the LLM-layer spread vs the mechanical baseline is
  below `config.KILL_SATELLITE_IF_SPREAD_BELOW_PP`, or override hit-rate vs baseline
  is below `config.KILL_MIN_OVERRIDE_HITRATE` on a sufficient sample. Result: v5
  degrades to `core.py` plus a monthly LLM-written report with **zero decision
  authority**. That end state is a success, not a failure — it was entered on
  evidence.
- **Expand the satellite** only if its picks beat the matched-window benchmark more
  often than `config.EXPAND_SATELLITE_IF_HITRATE_ABOVE` with positive mean excess
  over a full evaluation window.
- **Reconvene the board** if the mechanical core itself trails buy-and-hold QQQ by
  more than `config.BOARD_RECONVENE_IF_CORE_TRAILS_QQQ_PP` over 12 months (trend
  whipsaw exceeding its insurance value), or after the first DEFENSIVE episode ends.

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
| decision log | `~/.claude/skills/us-stock-advisor/state/recommendations.jsonl` |
| structured continuity | `~/.claude/skills/us-stock-advisor/state/last_run.json` |
| **read-only researcher** | `~/.claude/agents/stock-research-readonly.md` |

Nightly cron: `0 14 * * 1-5  python3 ~/.claude/skills/us-stock-advisor/scripts/score_recs.py --score`

## Acceptance tests (run on EVERY edit to this file)

1. `grep -nE 'subagent_type\s*[=:]\s*"?general-purpose' SKILL.md` → no hits (an
   actual invocation; the two prose mentions — the "never" instruction and the
   DELETE-list entry — are intended and do not count).
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
6. Every run appends exactly one `portfolio_mark`, no-ops included; a failed run
   appends a `pipeline_halt`.
7. No gating threshold value appears in this file.
8. Every symbol in `config.py` is referenced by at least one script — verified by
   grep in CI. A constant no script reads is v4's double-bookkeeping bug relocated
   into Python.
