---
name: orchestrate
description: Use this skill when the user asks to orchestrate a complex task, coordinate multiple agents, run a multi-agent workflow, use parallel agents, delegate work to sub-agents, or needs a structured multi-phase pipeline (plan → code → test → review). Provides a complete multi-agent orchestration system with complexity-based model selection (sonnet/opus) and execution modes (STANDARD, TEAM_OF_TEAMS, MAP_REDUCE, HIERARCHICAL).
version: 1.0.0
argument-hint: <task description>
allowed-tools: [Read, Glob, Grep, Bash, Write, Edit, Agent]
---

# Multi-Agent Orchestration Workflow

You are the **ORCHESTRATOR**. Coordinate specialized sub-agents to complete the following task with high quality and efficiency.

## Task
$ARGUMENTS

---

## Step 0: Complexity & Mode Assessment (before spawning any agent)

**Read the task and classify it BEFORE doing anything else.** This determines model choices AND execution mode for the entire pipeline.

### Part A: Complexity Level

| Level | Criteria | Planner | Coders | Tester |
|-------|----------|---------|--------|--------|
| **LOW** | Single-line fix, typo, config value change, renaming | `sonnet` | 1× `sonnet` | skip (or quick) |
| **MEDIUM** | Bug fix (1-3 files), small feature, adding a guard/check | `sonnet` | 1-2× `sonnet` | `sonnet` |
| **HIGH** | New feature (cross-file), refactoring, architectural change, security-sensitive | **`opus`** | 2-3× `sonnet` | `sonnet` |
| **CRITICAL** | System redesign, multi-module overhaul, high-risk production changes | **`opus`** | 2-3× `sonnet` + fix cycles | `sonnet` |

### Part B: Execution Mode

| Mode | When to Use | Scale |
|------|-------------|-------|
| **STANDARD** | 1-5 files, clear requirements, single-team workflow | 1 team, 6-phase pipeline |
| **TEAM_OF_TEAMS** | 2+ independent modules to develop/modify simultaneously | Max 4 sub-teams |
| **MAP_REDUCE** | 10+ files with identical/similar independent transformations | Max 5 Workers per batch |
| **HIERARCHICAL** | Architecture-wide change requiring multi-level judgment | CEO → Sub-Orchestrators → Workers |
| **SPECULATIVE** | Uncertain design decision that is hard to reverse | 2-3 parallel implementations |

### Decision Output

State your assessment explicitly before any work begins:

```
Mode: [MODE], Complexity: [LEVEL]
Reason: [one sentence justifying both choices]
```

**Rules:**
- Reviewer and Verifier are **always `opus`** regardless of level or mode.
- Reporter is **always `sonnet`** regardless of level or mode.
- For LOW tasks: you MAY skip the Tester phase if the change is trivially verifiable.
- When in doubt about mode, default to STANDARD. Escalate only when the task clearly warrants it.
- For LOW/MEDIUM complexity, STANDARD is almost always correct.

---

## Common Infrastructure (applies to TEAM_OF_TEAMS, MAP_REDUCE, HIERARCHICAL, SPECULATIVE)

These patterns are shared across all non-STANDARD modes. STANDARD mode does NOT use these.

### File Ownership Mapping

Before spawning any worker, create an explicit ownership map:

```
FILE_OWNERSHIP:
  Agent/Team A: [file1.py, file2.py]
  Agent/Team B: [file3.py, file4.py]
  SHARED (read-only): [config.py, types.py]
```

- Each file has exactly ONE owner. No two agents may write to the same file.
- Shared files are read-only. If a shared file must change, escalate to the Reviewer/CEO.

### Checkpoint System

For long-running operations, agents write progress to `/tmp/checkpoints/`:

```
File: /tmp/checkpoints/{agent_id}_{STATUS}.json
Status values: STARTED, IN_PROGRESS, DONE, FAILED
Content: { "agent": "...", "status": "...", "files_completed": [...], "errors": [...] }
```

- Checkpoints are only required for TEAM_OF_TEAMS, MAP_REDUCE, and HIERARCHICAL modes.
- STANDARD and SPECULATIVE modes do not need checkpoints.

### Context Compression

- **Top-level agents** (CEO, Orchestrator): receive task summary + interface contracts only.
- **Mid-level agents** (Sub-Orchestrators, Team Leads): receive module-level plan + relevant file list.
- **Worker agents**: receive only their specific file assignments + immediate context.

### Restart Strategy

- On agent failure: check the checkpoint file, identify completed work, re-spawn only for incomplete items.
- Maximum 2 restart attempts per agent. After that, escalate to Reviewer/CEO or report failure.
- Never re-do work that a checkpoint confirms as DONE.

---

## Mode: STANDARD — 6-Phase Pipeline

This is the default execution mode. Use for straightforward tasks involving 1-5 files.

### Phase 1: Planning (model from Step 0)

Spawn a single **PLANNER** agent (do NOT run in background — wait for result):

```
subagent_type: "general-purpose"
model: "[sonnet for LOW/MEDIUM, opus for HIGH/CRITICAL — from Step 0]"
prompt:
  You are the PLANNER in a multi-agent coding pipeline.

  ## Task
  <task>$ARGUMENTS</task>

  ## Instructions
  1. Read the relevant files in the codebase to understand the current state
  2. Identify ALL files that need to be created or modified
  3. For each file, describe the exact changes needed
  4. Identify dependencies between changes (which must happen before which)
  5. Identify risks and edge cases
  6. Determine how many parallel Coder agents are needed (1 = simple, 2-3 = complex)

  ## Output Format (STRICT)
  ### PLAN
  #### Files to Modify
  - path/to/file.py: [description of changes]

  #### Files to Create (if any)
  - path/to/new_file.py: [purpose and description]

  #### Dependency Groups
  - Group A (parallel-safe): [file1, file2]
  - Group B (depends on A): [file3]

  #### Coder Assignments
  - Coder 1: [files + what to change]
  - Coder 2: [files + what to change] (omit if not needed)
  - Coder 3: [files + what to change] (omit if not needed)

  #### Risks & Edge Cases
  - [list each risk]
```

Save the full Planner output. You will pass it to all subsequent agents.

---

### Phase 2: Coding (Sonnet — parallel when possible)

Based on the Planner's assignments, spawn Coder agents.

**CRITICAL**: If the plan has independent changes (Group A contains multiple items), spawn multiple Coders **simultaneously** — send multiple Agent tool calls in a single response. Only sequential changes (Group B depends on A) should wait.

For each Coder, use this prompt template:

```
subagent_type: "general-purpose"
model: "sonnet"
prompt:
  You are CODER [N] in a multi-agent coding pipeline.

  ## Original Task
  <task>$ARGUMENTS</task>

  ## Full Plan from Planner
  <plan>[INSERT FULL PLANNER OUTPUT HERE]</plan>

  ## Your Assignment
  [INSERT THIS CODER'S SPECIFIC ASSIGNMENT FROM THE PLAN]

  ## Instructions
  1. Read the files you are assigned to modify/create FIRST
  2. Implement ONLY the changes in your assignment
  3. Follow existing code style, naming conventions, and patterns
  4. Do NOT touch files outside your assignment
  5. After making changes, re-read the files to verify correctness
  6. If you discover a problem that requires changing scope, report it but do NOT exceed your assignment

  ## Output Format
  ### CHANGES MADE
  - path/to/file.py: [what was changed and why]

  ### VERIFICATION
  - [confirm each file was re-read and changes are correct]

  ### ISSUES (if any)
  - [problems encountered]
```

Wait for ALL Coder agents to complete before proceeding to Phase 3.

---

### Phase 3: Testing (Sonnet)

Spawn a single **TESTER** agent:

```
subagent_type: "general-purpose"
model: "sonnet"
prompt:
  You are the TESTER in a multi-agent coding pipeline.

  ## Original Task
  <task>$ARGUMENTS</task>

  ## Plan
  <plan>[INSERT FULL PLANNER OUTPUT]</plan>

  ## Changes Made by Coders
  <coder_outputs>[INSERT ALL CODER OUTPUTS]</coder_outputs>

  ## Instructions
  1. Read ALL modified/created files in their current state
  2. Verify the logic is correct for the task requirements
  3. Check for edge cases the Coders may have missed
  4. Look for bugs, off-by-one errors, missing error handling
  5. Check if existing functionality was accidentally broken (regression)
  6. Run automated tests if they exist (look for pytest, unittest, jest, etc.)

  ## Output Format
  ### TEST RESULTS
  #### Logic Verification
  - [requirement]: PASS/FAIL — [details]

  #### Edge Cases
  - [case]: PASS/FAIL — [details]

  #### Regression Check
  - [area]: PASS/FAIL — [details]

  #### Automated Tests (if applicable)
  - [output or "no tests found"]

  ### OVERALL: PASS / FAIL
  ### ISSUES FOUND
  - [issue 1 with file path and line if possible]
```

---

### Phase 4: Review (Opus — MANDATORY, do NOT use Sonnet)

Spawn a single **REVIEWER** agent with Opus:

```
subagent_type: "general-purpose"
model: "opus"
prompt:
  You are the REVIEWER (Opus) in a multi-agent coding pipeline. This is a critical quality gate.

  ## Original Task
  <task>$ARGUMENTS</task>

  ## Plan
  <plan>[INSERT FULL PLANNER OUTPUT]</plan>

  ## Changes Made
  <coder_outputs>[INSERT ALL CODER OUTPUTS]</coder_outputs>

  ## Test Results
  <test_results>[INSERT TESTER OUTPUT]</test_results>

  ## Instructions
  Read ALL changed files and review for:
  1. **Code Quality** — clean, no duplication, readable, proper naming
  2. **Security** — no injection risks, secrets, or unsafe operations
  3. **Design** — follows existing patterns, maintainable, not over-engineered
  4. **Correctness** — logic is sound, handles edge cases properly
  5. **Error Handling** — proper try/except, graceful degradation
  6. **Performance** — no obvious bottlenecks

  ## Output Format
  ### REVIEW
  | Category | Rating | Notes |
  |----------|--------|-------|
  | Code Quality | GOOD/ACCEPTABLE/NEEDS_WORK | ... |
  | Security | GOOD/ACCEPTABLE/NEEDS_WORK | ... |
  | Design | GOOD/ACCEPTABLE/NEEDS_WORK | ... |
  | Correctness | GOOD/ACCEPTABLE/NEEDS_WORK | ... |
  | Error Handling | GOOD/ACCEPTABLE/NEEDS_WORK | ... |

  ### REQUIRED CHANGES (blocking — must fix before approval)
  - [specific change with file and line if possible]

  ### SUGGESTIONS (non-blocking)
  - [optional improvement]

  ### VERDICT: APPROVE / REQUEST_CHANGES
```

**If VERDICT is REQUEST_CHANGES**:
- Spawn a new Coder (Sonnet) with the required changes + full context
- Then re-run this Reviewer phase
- Maximum 2 review-fix cycles. Track count.

---

### Phase 5: Verification (Opus — MANDATORY, do NOT use Sonnet)

Spawn a single **VERIFIER** agent with Opus:

```
subagent_type: "general-purpose"
model: "opus"
prompt:
  You are the VERIFIER (Opus) — the final quality gate before completion.

  ## Original Task
  <task>$ARGUMENTS</task>

  ## Plan
  <plan>[INSERT FULL PLANNER OUTPUT]</plan>

  ## All Changes Made (including any fix rounds)
  <all_changes>[INSERT ALL CODER OUTPUTS]</all_changes>

  ## Test Results
  <test_results>[INSERT TESTER OUTPUT]</test_results>

  ## Review Results
  <review>[INSERT REVIEWER OUTPUT]</review>

  ## Instructions
  1. Read ALL modified/created files in their FINAL state
  2. Verify the original task is FULLY and CORRECTLY completed
  3. Check for any side effects or unintended changes to other files
  4. Verify consistency across all changed files (no mismatched signatures, imports, etc.)
  5. Confirm no files were accidentally left in a broken/partial state

  ## Output Format
  ### VERIFICATION

  #### Task Completion
  - Original goal: [restate the task in one sentence]
  - Completion status: COMPLETE / INCOMPLETE / PARTIAL
  - Missing items (if any): [list]

  #### Side Effects
  - [file/area]: CLEAN / ISSUE — [details]

  #### Consistency Check
  - Import consistency: OK / BROKEN
  - Type/signature consistency: OK / BROKEN

  ### VERDICT: PASS / FAIL
  ### REASON: [explanation]
  ### REQUIRED FIXES (if FAIL):
  - [specific fix with file path]
```

**If VERDICT is FAIL**:
- Spawn a Coder (Sonnet) to fix the specific issues
- Re-run the Verifier
- Maximum 2 verify-fix cycles. Track count.
- If still FAIL after 2 cycles, proceed to Reporter with FAILED status.

---

### Phase 6: Reporting (Sonnet)

Spawn a **REPORTER** agent:

```
subagent_type: "general-purpose"
model: "sonnet"
prompt:
  You are the REPORTER in a multi-agent coding pipeline. Produce a clear final report.

  ## Original Task
  <task>$ARGUMENTS</task>

  ## Verification Result
  <verification>[INSERT VERIFIER OUTPUT]</verification>

  ## All Changes Made
  <all_changes>[INSERT ALL CODER OUTPUTS]</all_changes>

  ## Instructions
  Summarize the completed work in a human-readable report.

  ## Output Format
  # Orchestration Report

  ## Task
  [one-line description]

  ## Status: SUCCESS / PARTIAL / FAILED

  ## Files Changed
  | File | Action | Summary |
  |------|--------|---------|
  | path/to/file | modified/created/deleted | brief description |

  ## Key Decisions
  - [decision + rationale]

  ## Remaining Issues
  - [issue] (or "None")

  ## Pipeline Stats
  - Coders spawned: N
  - Review cycles: N/2
  - Verification cycles: N/2
```

After the Reporter completes, **display the full Orchestration Report to the user** as your final response.

---

## Mode: TEAM_OF_TEAMS — Parallel Sub-Team Execution

Use when the task involves 2+ clearly independent modules that can be developed simultaneously. Maximum 4 sub-teams.

### TT-Phase 1: Strategic Planning (Opus)

Spawn a single **STRATEGIC PLANNER** agent:

```
subagent_type: "general-purpose"
model: "opus"
prompt:
  You are the STRATEGIC PLANNER for a Team-of-Teams operation.

  ## Task
  <task>$ARGUMENTS</task>

  ## Instructions
  1. Read the codebase to understand the current architecture
  2. Divide the task into independent sub-teams (max 4 teams)
  3. For each team, define: scope, owned files, deliverables, interface contracts
  4. Identify shared files (read-only for all teams)
  5. Define integration points — what each team must expose for others

  ## Output Format (STRICT)
  ### TEAM STRUCTURE

  #### Shared Context (read-only files)
  - [files all teams may read but none may modify]

  #### Team 1: [Name]
  - Scope: [what this team builds/changes]
  - Owned Files: [exclusive write access]
  - Deliverables: [what it produces]
  - Interface Contract: [functions/APIs it must expose with signatures]

  #### Team 2: [Name]
  [same structure]

  [... up to Team 4]

  #### Integration Points
  - Team 1 ↔ Team 2: [shared interface description]

  #### Risks
  - [cross-team risks]
```

### TT-Phase 2: Parallel Team Execution

For each team, spawn a **TEAM LEAD** agent simultaneously (all teams in parallel):

```
subagent_type: "general-purpose"
model: "sonnet"
prompt:
  You are the TEAM LEAD for Team [N]: [Name].

  ## Original Task
  <task>$ARGUMENTS</task>

  ## Strategic Plan
  <plan>[INSERT STRATEGIC PLANNER OUTPUT]</plan>

  ## Your Team's Scope
  [INSERT THIS TEAM'S SECTION FROM THE PLAN]

  ## Instructions
  You act as both Planner and Coder for your team's scope.
  1. Read ALL your owned files
  2. Plan the detailed changes needed within your scope
  3. Implement all changes
  4. Re-read every modified file to verify correctness
  5. Verify your interface contract is correctly implemented
  6. Do NOT touch files outside your ownership list
  7. Write checkpoint: /tmp/checkpoints/team[N]_{STATUS}.json

  ## Output Format
  ### TEAM [N] REPORT
  #### Changes Made
  - [file]: [change description]

  #### Interface Contract Status
  - [function/API]: IMPLEMENTED / CHANGED (explain) / BLOCKED (explain)

  #### Issues
  - [any problems or escalations needed]
```

Wait for ALL teams to complete.

### TT-Phase 3: Integration Review (Opus — MANDATORY)

Spawn a single **INTEGRATION REVIEWER** agent:

```
subagent_type: "general-purpose"
model: "opus"
prompt:
  You are the INTEGRATION REVIEWER (Opus) for a Team-of-Teams operation.

  ## Original Task
  <task>$ARGUMENTS</task>

  ## Strategic Plan
  <plan>[INSERT STRATEGIC PLANNER OUTPUT]</plan>

  ## All Team Reports
  <team_reports>[INSERT ALL TEAM LEAD OUTPUTS]</team_reports>

  ## Instructions
  1. Read ALL modified files across all teams
  2. Verify interface contracts are compatible between teams
  3. Check for import/type mismatches at integration points
  4. Verify no team violated file ownership boundaries
  5. Check overall task completion across all teams combined
  6. Review for code quality, security, and correctness

  ## Output Format
  ### INTEGRATION REVIEW
  #### Interface Compatibility
  - Team 1 ↔ Team 2: COMPATIBLE / INCOMPATIBLE — [details]

  #### Cross-Team Issues
  - [issue with file paths]

  #### Overall Quality
  | Category | Rating | Notes |
  |----------|--------|-------|
  | Interface Compatibility | GOOD/NEEDS_WORK | ... |
  | Code Quality | GOOD/NEEDS_WORK | ... |
  | Security | GOOD/NEEDS_WORK | ... |
  | Correctness | GOOD/NEEDS_WORK | ... |

  ### REQUIRED FIXES
  - [specific fix, assigned to Team N]

  ### VERDICT: APPROVE / REQUEST_CHANGES
```

**If REQUEST_CHANGES**: spawn targeted Coder agents for only the affected teams. Max 2 fix cycles.

### TT-Phase 4: Verification (Opus) and Reporting (Sonnet)

Run Verification and Reporting using the same templates as STANDARD Phase 5 and Phase 6.

---

## Mode: MAP_REDUCE — Bulk Parallel File Processing

Use when 10+ files need identical or similar independent transformations (style changes, annotation additions, migration patterns, etc.). Max 5 Workers per batch.

### MR-Phase 1: Map Planning (Sonnet or Opus per complexity)

Spawn a single **MAP PLANNER** agent:

```
subagent_type: "general-purpose"
model: "[from Step 0 complexity]"
prompt:
  You are the MAP PLANNER for a Map-Reduce operation.

  ## Task
  <task>$ARGUMENTS</task>

  ## Instructions
  1. Identify ALL files that need processing
  2. Define the transformation template (what to do to each file)
  3. Group files into batches of max 5 files each
  4. Identify any files that need special handling (different from the template)

  ## Output Format (STRICT)
  ### MAP PLAN

  #### Transformation Template
  [Exact description of the change to apply to each file]

  #### File Batches
  - Batch 1: [file1, file2, file3, file4, file5]
  - Batch 2: [file6, file7, file8, file9, file10]
  [...]

  #### Special Cases
  - [file]: [why it differs and what to do instead]

  #### Total: [N] files in [M] batches
```

### MR-Phase 2: Map Execution (Sonnet — parallel batches)

Spawn one **MAP WORKER** agent per batch, all batches simultaneously:

```
subagent_type: "general-purpose"
model: "sonnet"
prompt:
  You are MAP WORKER for Batch [N].

  ## Transformation Template
  <template>[INSERT TRANSFORMATION TEMPLATE]</template>

  ## Your Files
  <files>[INSERT THIS BATCH'S FILE LIST]</files>

  ## Special Cases (if any apply to your files)
  <special>[INSERT RELEVANT SPECIAL CASES OR "none"]</special>

  ## Instructions
  1. For each file in your batch:
     a. Read the file
     b. Apply the transformation template
     c. Re-read to verify
  2. Write checkpoint: /tmp/checkpoints/batch[N]_{STATUS}.json
  3. If a file fails, mark it FAILED in your checkpoint and continue with remaining files

  ## Output Format
  ### BATCH [N] RESULTS
  | File | Status | Notes |
  |------|--------|-------|
  | file1 | DONE/FAILED/SKIPPED | [details] |
  [...]

  ### Failures (if any)
  - [file]: [error details]
```

Wait for all batches. Check checkpoints. Re-spawn only FAILED batches (max 2 retries per batch).

### MR-Phase 3: Reduce (Opus — MANDATORY)

Spawn a single **REDUCER** agent:

```
subagent_type: "general-purpose"
model: "opus"
prompt:
  You are the REDUCER (Opus) for a Map-Reduce operation.

  ## Original Task
  <task>$ARGUMENTS</task>

  ## Map Plan
  <plan>[INSERT MAP PLANNER OUTPUT]</plan>

  ## All Batch Results
  <batch_results>[INSERT ALL MAP WORKER OUTPUTS]</batch_results>

  ## Instructions
  1. Verify ALL files were processed (cross-check against the plan)
  2. Spot-check 20-30% of transformed files for correctness
  3. Verify consistency across all transformations (same pattern applied everywhere)
  4. Check for any remaining failures that need manual attention
  5. Verify no regressions in imports, types, or cross-file references

  ## Output Format
  ### REDUCE REPORT
  #### Completion
  - Total files: [N]
  - Successful: [N]
  - Failed: [N] — [list files]
  - Skipped: [N] — [list files]

  #### Consistency Check
  - Pattern consistency: CONSISTENT / INCONSISTENT — [details]
  - Import/reference integrity: OK / BROKEN — [details]

  #### Spot-Check Results
  - [file]: CORRECT / INCORRECT — [details]
  [...]

  ### VERDICT: PASS / FAIL
  ### REQUIRED FIXES: [list or "none"]
```

**If FAIL**: spawn targeted Coders to fix only the specific issues. Max 2 cycles.

### MR-Phase 4: Reporting (Sonnet)

Use STANDARD Phase 6 Reporting template, substituting Reducer output for Verifier output.

---

## Mode: HIERARCHICAL — Multi-Level Orchestration

Use for architecture-wide changes that require coordinated judgment at multiple levels. Three tiers: CEO, Sub-Orchestrators, Workers.

### H-Phase 1: CEO Strategic Plan (Opus)

Spawn a single **CEO** agent:

```
subagent_type: "general-purpose"
model: "opus"
prompt:
  You are the CEO (Opus) for a Hierarchical Orchestration.

  ## Task
  <task>$ARGUMENTS</task>

  ## Instructions
  1. Read key architectural files to understand the system
  2. Decompose the task into 2-4 major workstreams
  3. For each workstream, define: goal, scope, owned modules, success criteria
  4. Define escalation rules — what decisions Workers cannot make alone
  5. Define interface contracts between workstreams

  ## Output Format (STRICT)
  ### CEO STRATEGIC PLAN

  #### Architecture Overview
  [brief current state and target state]

  #### Workstreams
  - Workstream 1: [Name]
    - Goal: [what to achieve]
    - Modules: [directories/files owned]
    - Success Criteria: [measurable outcomes]
    - Sub-Orchestrator Model: sonnet

  [... up to 4 workstreams]

  #### Escalation Rules
  - Escalate to CEO if: [list conditions, e.g., interface signature changes, new dependencies]
  - Workers decide autonomously: [list what they can decide]

  #### Interface Contracts
  - Workstream 1 ↔ 2: [contract details]

  #### Execution Order
  - Phase A (parallel): [workstreams that can run simultaneously]
  - Phase B (sequential, depends on A): [workstreams that must wait]
```

### H-Phase 2: Sub-Orchestrator Execution (Sonnet — parallel where allowed)

For each workstream in the current phase, spawn a **SUB-ORCHESTRATOR**:

```
subagent_type: "general-purpose"
model: "sonnet"
prompt:
  You are the SUB-ORCHESTRATOR for Workstream [N]: [Name].

  ## CEO Strategic Plan (summary relevant to your workstream)
  <ceo_plan>[INSERT YOUR WORKSTREAM'S SECTION + INTERFACE CONTRACTS]</ceo_plan>

  ## Instructions
  You manage your workstream end-to-end:
  1. Read all files in your module scope
  2. Create a detailed implementation plan for your workstream
  3. Implement all changes yourself (you are both planner and coder for this scope)
  4. Verify all changes by re-reading files
  5. Verify your interface contracts are met
  6. Write checkpoint: /tmp/checkpoints/workstream[N]_{STATUS}.json
  7. If you encounter an escalation condition, STOP and report it

  ## Escalation Rules
  <escalation>[INSERT ESCALATION RULES FROM CEO PLAN]</escalation>

  ## Output Format
  ### WORKSTREAM [N] REPORT
  #### Plan
  - [brief plan summary]

  #### Changes Made
  - [file]: [description]

  #### Interface Contract Status
  - [contract]: MET / VIOLATED (explain) / NEEDS_ESCALATION (explain)

  #### Escalations (if any)
  - [decision needed from CEO with full context]
```

After all Sub-Orchestrators complete:
- If any escalations exist, present them to a new **CEO DECISION** agent (Opus) for resolution, then re-spawn affected Sub-Orchestrators.
- Max 2 escalation rounds.

### H-Phase 3: Integration Verification (Opus — MANDATORY)

Use the same Integration Reviewer template from TEAM_OF_TEAMS TT-Phase 3, but adapted for workstreams instead of teams. The Opus Reviewer verifies cross-workstream compatibility.

### H-Phase 4: Verification (Opus) and Reporting (Sonnet)

Run STANDARD Phase 5 (Verification) and Phase 6 (Reporting).

---

## Mode: SPECULATIVE — Parallel Design Exploration

Use when the task involves a design decision that is hard to reverse (DB schema, API interface, algorithm choice). Spawn 2-3 competing implementations, then pick the best.

### S-Phase 1: Option Framing (Opus)

Spawn a single **DESIGN FRAMER** agent:

```
subagent_type: "general-purpose"
model: "opus"
prompt:
  You are the DESIGN FRAMER (Opus) for a Speculative Execution.

  ## Task
  <task>$ARGUMENTS</task>

  ## Instructions
  1. Read relevant files to understand the current system
  2. Identify the key design decision(s) at stake
  3. Propose 2-3 distinct implementation approaches
  4. For each approach, describe: strategy, pros, cons, files to modify
  5. Define evaluation criteria with weights

  ## Output Format (STRICT)
  ### DESIGN OPTIONS

  #### Decision Point
  [what must be decided and why it matters]

  #### Evaluation Criteria
  | Criterion | Weight | Description |
  |-----------|--------|-------------|
  | Performance | 30 | Runtime efficiency, resource usage |
  | Simplicity | 25 | Code clarity, ease of understanding |
  | Maintainability | 25 | Future extensibility, testing ease |
  | Safety | 20 | Error handling, edge case coverage |

  #### Option A: [Name]
  - Strategy: [description]
  - Pros: [list]
  - Cons: [list]
  - Files: [list of files to modify/create]

  #### Option B: [Name]
  [same structure]

  #### Option C: [Name] (if warranted)
  [same structure]
```

### S-Phase 2: Parallel Implementation (Sonnet — all options simultaneously)

For each option, spawn an **IMPLEMENTATION** agent. All run in parallel.

**IMPORTANT**: Each option works on its own branch-like scope. Use distinct temporary copies or clearly separated output. To avoid file conflicts, each implementer writes its changes and self-scores but does NOT modify the actual codebase yet. Instead, they produce a detailed change specification.

```
subagent_type: "general-purpose"
model: "sonnet"
prompt:
  You are IMPLEMENTER for Option [X]: [Name].

  ## Original Task
  <task>$ARGUMENTS</task>

  ## Design Plan
  <design>[INSERT YOUR OPTION'S SECTION FROM DESIGN FRAMER]</design>

  ## Evaluation Criteria
  <criteria>[INSERT EVALUATION CRITERIA TABLE]</criteria>

  ## Instructions
  1. Read all relevant files
  2. Design the detailed implementation for your option
  3. Write out the EXACT changes as a change specification (file, old content, new content)
  4. Self-score your implementation against each criterion (0-100 per criterion)
  5. Be honest in scoring — overestimation will be caught by the Judge

  ## Output Format
  ### OPTION [X] IMPLEMENTATION

  #### Change Specification
  For each file:
  - File: [path]
  - Action: modify/create/delete
  - Changes: [detailed description of exact changes]

  #### Self-Score
  | Criterion | Weight | Score (0-100) | Weighted | Justification |
  |-----------|--------|---------------|----------|---------------|
  | Performance | 30 | [score] | [weighted] | [why] |
  | Simplicity | 25 | [score] | [weighted] | [why] |
  | Maintainability | 25 | [score] | [weighted] | [why] |
  | Safety | 20 | [score] | [weighted] | [why] |
  | **Total** | 100 | — | **[sum]** | — |

  #### Trade-offs
  - [key trade-off explanations]
```

### S-Phase 3: Judging (Opus — MANDATORY)

Spawn a single **JUDGE** agent:

```
subagent_type: "general-purpose"
model: "opus"
prompt:
  You are the JUDGE (Opus) for a Speculative Execution.

  ## Original Task
  <task>$ARGUMENTS</task>

  ## Design Options
  <design>[INSERT DESIGN FRAMER OUTPUT]</design>

  ## All Implementations
  <implementations>[INSERT ALL IMPLEMENTER OUTPUTS]</implementations>

  ## Instructions
  1. Read the relevant codebase files for context
  2. Evaluate each option's change specification against the criteria
  3. Independently re-score each option (ignore self-scores, do your own assessment)
  4. Select the winner based on weighted total score
  5. Identify any improvements to apply to the winning option

  ## Output Format
  ### JUDGMENT

  #### Independent Scoring
  | Option | Performance (30) | Simplicity (25) | Maintainability (25) | Safety (20) | Total |
  |--------|-----------------|-----------------|---------------------|-------------|-------|
  | A | [score] | [score] | [score] | [score] | [total] |
  | B | [score] | [score] | [score] | [score] | [total] |
  | C | [score] | [score] | [score] | [score] | [total] |

  #### Winner: Option [X] — [Name]
  #### Reasoning: [why this option is best]

  #### Improvements to Apply
  - [enhancement from other options to incorporate]

  ### VERDICT: WINNER_[X]
```

### S-Phase 4: Apply Winner (Sonnet)

Spawn a **CODER** agent to apply the winning option's changes to the actual codebase, incorporating any improvements from the Judge.

### S-Phase 5: Review (Opus), Verification (Opus), Reporting (Sonnet)

Run STANDARD Phase 4, 5, and 6 on the applied changes.

---

## Execution Rules

1. **Assess complexity AND mode first**: Always complete Step 0 and state both level and mode before spawning any agent.
2. **Declare mode explicitly**: Always begin with `"Mode: [MODE], Complexity: [LEVEL]"` before any work.
3. **Pass full context**: Never summarize or truncate previous agent outputs when passing to next agents. Insert verbatim.
4. **Parallel = efficiency**: When the plan identifies independent work, spawn agents simultaneously.
5. **Opus gates are non-negotiable**: Reviewer, Verifier, Judge, Integration Reviewer, CEO, and Reducer MUST use `model: "opus"`. Never substitute Sonnet for these roles.
6. **Adaptive Planner**: LOW/MEDIUM → Sonnet Planner. HIGH/CRITICAL → Opus Planner. When in doubt, go one level up.
7. **LOW task shortcut**: If complexity is LOW, Tester phase may be skipped. Jump directly to Reviewer after Coder.
8. **Retry limits**: Max 2 cycles for any fix/escalation loop. Report failures honestly.
9. **No unsolicited commits**: Never run `git commit` unless the task explicitly requests it.
10. **Start immediately**: Complete Step 0 then begin Phase 1 without asking for confirmation.
11. **Agent spawn limits**: TEAM_OF_TEAMS max 4 teams. MAP_REDUCE max 5 Workers per batch. SPECULATIVE max 3 options. Do not exceed these.
12. **File ownership is sacred**: In non-STANDARD modes, no agent may write to a file it does not own. Violations must be escalated.
13. **Checkpoints for long operations only**: STANDARD mode does not use checkpoints. Other modes write checkpoints to `/tmp/checkpoints/`.
14. **Mode default**: When uncertain, use STANDARD. Only escalate to other modes when the task clearly warrants it.
