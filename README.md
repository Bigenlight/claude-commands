# claude-commands

Personal Claude Code skills — shared online, installed manually on each machine.

## Skills

| Skill | Trigger | Description |
|-------|---------|-------------|
| [`/orchestrate`](#orchestrate) | Auto or manual | Multi-agent orchestration pipeline |
| [`/repo-context`](#repo-context) | Manual | Read CLAUDE.md + README to orient in any repo |

---

## Install (all skills)

```bash
git clone git@github.com:Bigenlight/claude-commands.git
cd claude-commands

# orchestrate
mkdir -p ~/.claude/skills/orchestrate
cp skills/orchestrate/SKILL.md ~/.claude/skills/orchestrate/SKILL.md

# repo-context
mkdir -p ~/.claude/skills/repo-context
cp skills/repo-context/SKILL.md ~/.claude/skills/repo-context/SKILL.md
```

Restart Claude Code — skills will be active.

## Update (all skills)

```bash
cd claude-commands
git pull

cp skills/orchestrate/SKILL.md ~/.claude/skills/orchestrate/SKILL.md
cp skills/repo-context/SKILL.md ~/.claude/skills/repo-context/SKILL.md
```

---

## orchestrate

Multi-agent orchestration for complex tasks. Auto-triggers when describing multi-agent work, or invoke directly:

```
/orchestrate fix the login bug
/orchestrate add pagination to the /users API endpoint
/orchestrate refactor the database layer to use connection pooling
```

Auto-classifies complexity (LOW / MEDIUM / HIGH / CRITICAL) and selects an execution mode:

| Mode | When |
|------|------|
| **STANDARD** | Default — 6-phase pipeline (Planner → Coder → Tester → Reviewer → Verifier → Reporter) |
| **TEAM_OF_TEAMS** | 2+ independent modules — up to 4 parallel sub-teams |
| **MAP_REDUCE** | 10+ files needing similar changes — up to 5 workers per batch |
| **HIERARCHICAL** | Architecture-wide changes — CEO → Sub-Orchestrators → Workers |

Model selection is automatic: Sonnet for coding, Opus for planning (HIGH+), reviews, and verification.

---

## repo-context

Reads `CLAUDE.md` and `README.md` in the current repo, checks recent git log, and gives a quick orientation summary. Useful at the start of a new conversation.

```
/repo-context
```

Outputs: repo purpose / folder structure / recent activity / key conventions / suggested entry point.

---

## Uninstall

```bash
rm -rf ~/.claude/skills/orchestrate
rm -rf ~/.claude/skills/repo-context
```

## License

MIT
