# claude-commands

Multi-agent orchestration skill for [Claude Code](https://docs.anthropic.com/en/docs/claude-code).

## Install

```bash
git clone git@github.com:Bigenlight/claude-commands.git
mkdir -p ~/.claude/skills/orchestrate
cp claude-commands/skills/orchestrate/SKILL.md ~/.claude/skills/orchestrate/SKILL.md
```

Restart Claude Code — the `orchestrate` skill will be active.

## Update

```bash
cd claude-commands
git pull
cp skills/orchestrate/SKILL.md ~/.claude/skills/orchestrate/SKILL.md
```

## Usage

Claude auto-triggers the skill when you describe a complex, multi-agent task. You can also invoke it directly:

```
/orchestrate fix the login bug
/orchestrate add pagination to the /users API endpoint
/orchestrate refactor the database layer to use connection pooling
```

## What it does

Auto-classifies task complexity (LOW / MEDIUM / HIGH / CRITICAL) and selects an execution mode:

| Mode | When |
|------|------|
| **STANDARD** | Default — 6-phase pipeline (Planner → Coder → Tester → Reviewer → Verifier → Reporter) |
| **TEAM_OF_TEAMS** | 2+ independent modules — up to 4 parallel sub-teams |
| **MAP_REDUCE** | 10+ files needing similar changes — up to 5 workers per batch |
| **HIERARCHICAL** | Architecture-wide changes — CEO → Sub-Orchestrators → Workers |

Model selection is automatic: Sonnet for coding, Opus for planning (HIGH+), reviews, and verification.

## Uninstall

```bash
rm -rf ~/.claude/skills/orchestrate
```

## License

MIT
