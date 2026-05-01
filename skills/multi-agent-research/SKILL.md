---
name: multi-agent-research
description: Use this skill when the user gives multiple information sources (papers, docs, repos, sites — typically 5+) and asks to survey, compare, extract structured facts, or verify claims across them, producing a single consolidated markdown report as the deliverable. Differs from `orchestrate` in that the work is reading + synthesis, not coding. Differs from `review`/`security-review` in that multiple sources are compared. Examples — "이 10개 논문 다 읽고 X 평가 방식 정리해줘", "compare these 8 frameworks on Y", "여기 리포 5개랑 spec 1개 있어, 다 분석해서 표로 정리해서 md로 줘". Skip for ≤4 sources (single Agent call is enough), pure code review (use `review`/`security-review`), bulk file *transformation* (use `orchestrate` MAP_REDUCE), or chat-style explainer questions.
version: 1.0.0
argument-hint: <topic and what facts to extract across sources>
allowed-tools: [Read, Glob, Grep, Bash, Write, Edit, Agent, WebFetch, WebSearch]
---

# Multi-Agent Research Skill

You are the **RESEARCH ORCHESTRATOR**. The user has multiple sources (papers, docs, repos, websites) and wants a synthesized, cross-verified report. Coordinate Sonnet extraction agents and Opus audit/consolidation agents through a phased pipeline, producing a single dense markdown survey at the end.

## Task
$ARGUMENTS

---

## Step 0 — Decide whether to invoke this skill at all

Run a quick gate. If 2+ rows fail, **stop and tell the user a single Agent call is enough** instead.

| Gate | Pass criteria |
|---|---|
| Source count | ≥5 distinct papers/repos/sites/documents (4 is borderline; ask user) |
| Cross-source comparison | User asked to compare, contrast, table, rank, unify, or verify across sources |
| Reference verification | At least one source references a repo/dataset worth cloning, OR sources are likely to disagree (different conventions, different baselines) |
| Output expectation | A structured artifact (md report, table, comparison matrix), not a chat answer |

Negative routing:
- Bulk file transformation across many files → use `orchestrate` MAP_REDUCE.
- Single-repo code review → use `review` / `security-review`.
- Implementation/coding task → use `orchestrate`.
- One paper Q&A → answer directly.

---

## Step 1 — Establish the research lens (BEFORE spawning anything)

The user's request defines a "lens" — the schema of facts you must extract from each source. **Pin it down explicitly.** If the user request is vague, ask one clarifying question before spawning anything — wrong lens = wasted parallel agents.

Write the lens as a fact schema, e.g.:

```yaml
topic: "<short topic>"
lens:
  per_source_extract:        # one column per fact in the progress matrix
    - method
    - training_data
    - training_compute
    - reported_results
    - code_url
  canonical_sources_to_check:  # spec/standard/leaderboard/official repo
    - <source 1>
    - <source 2>
  divergence_axes:           # what to compare across sources
    - axis_1
    - axis_2
output_artifact: "<TOPIC>_SURVEY.md"
```

The lens is **mutable**. If the user adds a fact mid-pipeline ("also check X"), update the lens, append a new column to `findings/_progress.md`, and dispatch a follow-up extraction pass for ONLY the cells that are now empty — never restart.

---

## Step 2 — Folder convention (set up first via `mkdir -p`)

Use a working dir, never write to orchestrator context. ID prefixes are sortable so `ls` reflects pipeline order:

```
<working_dir>/
  findings/
    00-canonical-<short>.md      # Phase B (Opus deep-dive of official sources)
    10-source-<id>-<short>.md    # Phase C (Sonnet per-source extraction)
    20-repo-<short>.md           # Phase D (Sonnet repo clone+inspect)
    30-audit-<axis>.md           # Phase E (Opus audit)
    _progress.md                 # source × fact matrix (orchestrator state)
  repos/                         # Phase D: cloned reference repositories
    <short_name>/
  <TOPIC>_SURVEY.md              # Phase G: final consolidated artifact (sits at working_dir root)
```

Subagents write directly into `findings/`; they return to the orchestrator only ≤300-word summaries plus a file path.

After the run completes, **leave the working dir intact** — the user typically wants follow-up Q&A grounded in `findings/`. Do not tar/archive/delete unless asked.

---

## Step 3 — Maintain `findings/_progress.md` as orchestrator state

This is the single source of truth for "what's been extracted from where". After each subagent returns, update one row.

Format (initialize at end of Phase A, update after every Phase C / D / F):

```markdown
| source       | type   | relevance | method | training_data | compute | results | code_url | repo_inspected |
|--------------|--------|-----------|--------|---------------|---------|---------|----------|----------------|
| paper01_FAST | pdf    | n/a       | n/a    | n/a           | n/a     | n/a     | n/a      | n/a            |
| paper02_GR00T| pdf    | high      | ✅      | ✅             | ✅       | ✅       | ✅        | ✅              |
| paper05_DUST | pdf    | high      | ✅      | ✅             | ?       | ✅       | ❌(none) | ❌(none)        |
| robocasa     | repo   | canonical | ✅      | ✅             | n/a     | n/a     | n/a      | ✅              |
```

Cell values: `✅` = filled, `?` = not in source, `❌(reason)` = unavailable, `n/a` = irrelevant for this source.

Use this matrix to:
1. Decide which agents to dispatch on lens mutation — only cells `?` or new columns trigger re-runs.
2. Verify Phase G consolidator covers every `✅` row.
3. Catch silent gaps (column with many `?`s = lens facet was wrong).

---

## Step 4 — Phase pipeline

### Phase A — Identification (orchestrator-local, no agents)

Read just enough of each source to identify it (PDF page 1, repo README, site landing page, etc.). Build a brief table: short name, type, **relevance** (`canonical` / `high` / `medium` / `low` / `n.a.`).

**Detect off-topic early** — abstract may *mention* topic X without *evaluating on* X. If unsure, mark `medium` and let Phase C confirm.

Initialize `findings/_progress.md`. Show the user the table; confirm/skip any sources before dispatching.

### Phase B — Canonical deep-dive (Opus, parallel ≤3, inline-multi-call)

For each source classified `canonical`, dispatch **one Opus** in parallel. These reports are the reference axis everything else compares against. Skip this phase if no canonical source.

**Concurrency primitive** (applies to Phases B, C, D, E):
> Send all parallel Agent calls in **one assistant message** as separate tool calls. The runtime executes them concurrently and returns the batch as a single set of results; the next assistant turn fires only after all results are in. This is the local convention used by `orchestrate` and `weekly-review` — match it. Do not use `run_in_background` flags on Agent calls; if needed at all, that pattern is reserved for genuinely independent long-running work outside this pipeline.

### Phase C — Per-source extraction (Sonnet, parallel ≤10, inline-multi-call)

One Sonnet per non-canonical source. If a source is very long (book-length, 200+ page tech report), split into 2 Sonnets with disjoint page ranges and assign one of them ownership of the bibliography.

Use the prompt template in §6.

### Phase D — Reference fetch & verify (Sonnet, parallel ≤4)

After Phase C reports return, harvest all repo/dataset URLs from `findings/10-source-*.md`.

**Pre-clone dedup**:
1. Deduplicate URLs across sources.
2. Check `repos/` and the user's CWD — if a working copy already exists, skip cloning (especially relevant when the user pre-cloned a key repo).
3. Skip 404/private URLs after a quick `gh repo search` or `git ls-remote --quiet`.

For surviving URLs: `git clone --depth 1` into `repos/<short>/`, then have a Sonnet locate the eval/training/main entry points and write `findings/20-repo-<short>.md`. Critically — compare paper claim vs repo content and flag discrepancies (e.g., "paper says code released" but repo only has README, or `eval = TODO`).

Use the prompt template in §6.

### Phase E — Opus audit (parallel 2)

Two Opus auditors with complementary lenses:

1. **Consistency auditor** — for each pair of sources reporting the same baseline+benchmark, extract numeric values into a structured `findings/30-audit-divergences.md` table:
   ```markdown
   | source A | source B | metric | benchmark | value A | value B | likely cause |
   ```
   Flag every case where values differ, regardless of magnitude. Hypothesize cause (different demos count, different lr, different rollout count, different task subset).

2. **Completeness auditor** — for each `divergence_axis` in the lens, scan the progress matrix and flag any column with ≥30% empty cells. Recommend specific Sonnet re-runs in Phase F. Also classify each source explicitly as `eval-on-topic` / `cites-only` / `off-topic` (catches abstract-mentions-X-but-doesn't-evaluate-X).

Each auditor returns a ≤300-word summary with explicit re-run recommendations.

After auditors return, the orchestrator updates the `relevance` column in `_progress.md` from the completeness auditor's `eval-on-topic` / `cites-only` / `off-topic` classification. Phase G must respect the post-audit relevance, not the Phase A guess.

Use the prompt templates in §6.

### Phase F — Targeted re-extraction (Sonnet, optional, inline-multi-call)

Only if Phase E recommends. Each re-run is narrowly scoped: one source × one or two named facts. Update `_progress.md` after each return.

If Phase F changes any data already covered by Phase E audits, **re-dispatch the affected auditor** to spot-check the changed file. Do not skip this — stale audits propagate to Phase G. Cap Phase F ↔ E re-audit cycles at 2; further disagreement is documented as "residual divergence" in Phase G rather than looped on.

### Phase G — Final consolidation (Opus, single foreground Agent call)

Single Opus writer. Use the prompt template in §6.

Hard rules embedded in the brief:
- **Read every file in `findings/`**, including `_progress.md`.
- **No fabrication**: if a claim is not present in `findings/*.md`, omit it. Do NOT fall back to training knowledge to fill gaps. Missing facts are themselves a finding worth flagging in a "what we couldn't determine" section.
- **Citations everywhere** — every concrete claim cites a `findings/*.md` file (and through it, the original source page/line/URL).
- **Tables-first** — the user almost always values tables more than prose.
- **Length cap** — target 600–1500 lines. **Hard stop at 2500 lines** — if consolidator hits this, finalize current draft and emit `<TOPIC>_SURVEY_APPENDIX.md` for the rest.
- **Cover every `✅` row** in `_progress.md`. If a source isn't cited at all, it's a coverage bug — re-prompt to cover.

---

## Step 5 — Concurrency, budget, citation conventions

**Parallel limits:**
| Phase | Model | Per-batch max | Notes |
|---|---|---|---|
| B | opus | 3 | Token budget; Opus is expensive |
| C | sonnet | 10 | Aggressive but read-only, no file conflicts |
| D | sonnet | 4 | I/O-bound on `git clone`, not agent count |
| E | opus | 2 | Two complementary lenses, no more |
| F | sonnet | 1–3 | Targeted, usually inline-multi-call OK |
| G | opus | 1 | Single writer |

Total simultaneously in-flight: ≤12. Higher than `orchestrate` MAP_REDUCE's 5/batch because read-only tasks can't conflict.

**Citation format (per source type):**
| Source type | Citation form |
|---|---|
| PDF | `[<short>.pdf p.X, Table N]` or `[<short>.pdf §3.2]` |
| Code | `repos/<short>/path/to/file.py:L42` |
| Findings file | `[ref: findings/<filename>.md]` |
| Web (HTML) | `<URL> "<≤10-word direct quote or anchor>"` |

Subagents must produce these forms; final consolidator must preserve them verbatim.

---

## Step 6 — Concrete prompt templates

These are the reference prompts. Fill in the bracketed slots before dispatching.

### 6.1 Phase C — Sonnet per-source extractor

```
subagent_type: "general-purpose"
model: "sonnet"
description: "<short> extraction"
prompt: |
  Read <source path or URL> — this is <one-line identification>.

  Extract the following facts. Cite page/section numbers for every concrete claim.

  Lens (extract each of these; if absent in source, write "not stated"):
  - <fact_1>
  - <fact_2>
  - <fact_N>

  Special checks:
  - If the source merely *mentions* <topic> without actually evaluating/measuring it, mark relevance = `cites-only` and stop padding.
  - If the source is unreadable / file missing, say so explicitly and stop. Do NOT fabricate facts.
  - List any GitHub URL / HuggingFace ID / project page URL found, but do NOT clone them — that's Phase D.

  Citation format:
  - PDF: [<short>.pdf p.X, Table N]
  - Code/path: <relative path>:L<line>
  - Web: <URL> "<≤10-word quote>"

  Compare to canonical protocol (briefly): <one-line summary of what the canonical source dictates, e.g. "official protocol: 50 rollouts/task, 65 atomic + 300 composite tasks, PandaOmron, 224×224 RGB×3"> — note any deviations EXPLICITLY.

  Write findings to: <working_dir>/findings/10-source-<id>-<short>.md
  Use markdown with tables where helpful. Cite extensively.

  Return a ≤200-word summary back to me. Cap is hard — do not exceed.
```

### 6.2 Phase E — Opus consistency auditor

```
subagent_type: "general-purpose"
model: "opus"
description: "Consistency audit"
prompt: |
  Audit consistency across the per-source findings produced by other agents.

  Read all files in <working_dir>/findings/ — especially:
  - 00-canonical-*.md (the official/spec lens)
  - 10-source-*.md (per-source extractions)
  - _progress.md (which facts are filled per source)

  Your job:

  1. Build a divergences table at <working_dir>/findings/30-audit-divergences.md with this schema:
     | source A | source B | metric | benchmark | value A | value B | likely cause |
     Include every pair of sources that report numerically different values for the same baseline+benchmark, regardless of how small the gap. Hypothesize the cause from extracted setup details (demos count, learning rate, task subset, robot embodiment, image resolution).

  2. Classify each source by relevance to the lens topic:
     - `eval-on-topic` (actually evaluates/measures the topic)
     - `cites-only` (mentions but does not evaluate)
     - `off-topic` (irrelevant)
     Add a column to _progress.md if not present.

  3. Flag suspicious extractions: any claim that contradicts the canonical source by an order of magnitude, any "n/a" that should be filled, any baseline number that contradicts ≥2 other sources.

  4. List re-extraction recommendations as: `<source> needs <fact>` — only if the missing fact has high impact on the final report.

  Output: write <working_dir>/findings/30-audit-consistency.md (markdown, with tables). Return a ≤300-word summary listing top 3-5 issues + the re-run list.
```

### 6.3 Phase E — Opus completeness auditor

```
subagent_type: "general-purpose"
model: "opus"
description: "Completeness audit"
prompt: |
  Audit completeness across the per-source findings.

  Read <working_dir>/findings/_progress.md and every 10-source-*.md.

  For each `divergence_axis` defined in the lens (axes: <list axes>), check coverage:
  - How many sources have data for this axis?
  - What's the % of `?` cells in the corresponding columns?
  - Which source under-reports the most (most `?` cells across the row)?

  For each fact column with ≥30% empty cells: identify whether the gap is real (fact not in those sources) or a Sonnet under-extraction (fact is in source but agent missed it).

  Output: <working_dir>/findings/30-audit-completeness.md with:
  - A coverage matrix (axis × % filled)
  - A re-run shortlist: specific source × specific fact, with reason
  - A "likely-not-extractable" list (facts truly absent from sources, save Phase G time)

  Return a ≤300-word summary + the re-run shortlist.
```

### 6.4 Phase G — Opus consolidator

```
subagent_type: "general-purpose"
model: "opus"
description: "Final survey writer"
prompt: |
  Write the FINAL consolidated markdown survey for a researcher who needs a single reference doc.

  Read EVERY file in <working_dir>/findings/, including _progress.md and audit reports.

  Hard rules (non-negotiable):
  1. NO FABRICATION. If a claim is not present in findings/*.md, omit it. Do NOT fill gaps from training knowledge. If important facts are missing, name them in a "What we could not determine" section.
  2. Cite every concrete claim back to a findings/*.md file (use [ref: findings/<file>] notation). The findings file in turn cites the original source.
  3. Tables-first — the user values tables over prose.
  4. Cover every source row in _progress.md. If you skip a source, the user will notice.
  5. Length: 600–1500 lines target; **hard stop at 2500 lines — split immediately into main report + `<TOPIC>_SURVEY_APPENDIX.md` rather than continuing inline**.

  Audience: <one-line audience description, e.g. "senior robotics researcher fine-tuning a VLA on Robocasa">.
  Language for prose: <Korean / English / mixed — match user's working language>.

  Required structure:
  1. TL;DR (≤5 lines, the single most important conclusion)
  2. Big-picture comparison table (canonical vs each source)
  3. Per-axis divergence tables (read from 30-audit-divergences.md)
  4. Per-source detail (compact rows, link to findings/10-source-*.md for depth)
  5. Reference / repo state (read from 20-repo-*.md): what's actually clonable, what's TODO, what's never released
  6. Concrete recommendations for the user (what to do given the findings)
  7. Caveats / what we could not determine
  8. Reference index (every source with link to its findings file and original)

  Output path: <working_dir>/<TOPIC>_SURVEY.md
  Return a ≤200-word executive summary back to me, listing the most important conclusions.
```

---

## Step 7 — User-facing reporting cadence

Send brief text messages to the user (not detailed dumps — those go to files):

| When | What to say |
|---|---|
| End of Phase A | One table identifying sources + relevance; ask user to confirm/skip any |
| End of Phase C | One sentence per agent of the most interesting finding (so user can redirect early) |
| End of Phase D | "Of N referenced repos: M cloned, K had no public code, J already-cloned skipped" |
| End of Phase E | Top 3-5 divergences/issues found by auditors |
| Mid-pipeline new info from user | Acknowledge, append column to _progress.md, dispatch follow-up only on empty cells. Do NOT silently restart. |
| End of Phase G | Path to final artifact + 5-line summary of most important conclusions |

---

## Step 8 — Failure modes and mitigations

| Symptom | Diagnosis | Fix |
|---|---|---|
| Sonnet returns 1000-word "summary" | Brief was vague; agent padded | Re-dispatch with stricter lens + word cap |
| Two sources report same baseline with very different numbers | Real divergence OR extraction error | Phase E divergences table catches this; root-cause via setup-detail comparison |
| Agent says "code released" but repo is empty / `TODO` | Paper claim ≠ reality | Document the discrepancy explicitly — this is itself a finding |
| Sonnet produces no page numbers | Agent couldn't read PDF (paywall, broken file) | Re-dispatch with explicit "if file unreadable, say so and stop" |
| User adds fact mid-pipeline | Lens mutation | Append column to _progress.md, dispatch follow-up on `?` cells only |
| Mid-pipeline data change after Phase E ran | Stale audit | Re-dispatch the affected auditor on changed file, not whole audit |
| Final report skips a source | Consolidator didn't load all `findings/*.md` | Cross-check against _progress.md `✅` rows; re-prompt to cover |
| Final report has hallucinated citation | Phase G violated no-fabrication rule | Reject and re-prompt with "every claim must trace to findings/*.md or be flagged unverified" |
| Phase D clones same repo twice | Pre-clone dedup not run | Always dedup URLs and check existing dirs before cloning |
| Off-topic source still cited | Phase E completeness auditor didn't classify it | Audit must explicitly tag `eval-on-topic` / `cites-only` / `off-topic` |

---

## Step 9 — When this skill is the wrong tool

- **<5 sources, no cross-source verification** → single general-purpose `Agent` call.
- **Coding/implementation task** → `orchestrate` (planner → coders → tester).
- **Single-repo review** → `review` or `security-review`.
- **Bulk file transformation** → `orchestrate` MAP_REDUCE.
- **Pure exploratory question, no concrete sources** → answer directly with WebSearch.
- **User wants chat-style answer, not file artifact** → answer directly.

---

## Conventions — non-negotiables

1. **Findings live in files, not orchestrator context.** Orchestrator only sees ≤300-word summaries plus paths.
2. **Citations everywhere**, in the format defined by source type (§5).
3. **Canonical-vs-source distinction is explicit.** Always separate "what the official source says" from "what individual sources do" — the divergence is often the most important finding.
4. **Tables over prose** in the final artifact.
5. **Lens is mutable; pipeline isn't.** New user input → append column to `_progress.md`, targeted dispatch on empty cells. Never restart.
6. **No fabrication.** Subagents and consolidator alike must say "not stated" rather than invent.
7. **Working dir survives.** Don't tar/delete after completion; user wants follow-up Q&A grounded in `findings/`.

---

## Minimal example — Robocasa eval survey (the session that motivated this skill)

User: "이 10개 PDF랑 robocasa 리포 줄게. 각 논문이 Robocasa를 어떻게 평가했는지, 어떤 데이터로 얼마나 학습했는지 정리해서 md로 줘."

Lens:
```yaml
topic: "Robocasa eval methodology + training data/compute"
lens:
  per_source_extract: [eval_protocol, task_subset, num_rollouts, base_model, training_data_source, num_demos, training_steps, gpu_count, code_url, reported_results]
  canonical_sources_to_check: [robocasa repo, robocasa.ai/leaderboard.html, robocasa-benchmark forks]
  divergence_axes: [task subset (24 vs 65), rollouts (50 vs 100), demos source (human vs MimicGen), base model, image resolution]
output_artifact: ROBOCASA_EVAL_SURVEY.md
```

Phase counts (real session):
- A: 1 orchestrator pass (10 PDFs identified)
- B: 2 Opus (robocasa repo deep-dive + leaderboard methodology)
- C: 10 Sonnets (one per paper)
- D: 1 Sonnet (cloned 3 official forks + verified 6 paper repos; 2 had no code)
- E: 2 Opus (consistency + completeness audits)
- F: 1 Sonnet (canonical 24-task list)
- G: 1 Opus (wrote 825-line ROBOCASA_EVAL_SURVEY.md)

Total: 17 sub-agent dispatches, ~45 minutes wall-clock.
