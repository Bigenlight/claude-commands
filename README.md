# claude-commands

Shared custom slash commands for [Claude Code](https://docs.anthropic.com/en/docs/claude-code). Clone this repo on any machine to get the same commands everywhere.

<!-- 여러 PC에서 동일한 커스텀 명령어를 공유하기 위한 저장소입니다. -->

## What's Included

| Command | Description |
|---------|-------------|
| `orchestrate` | Multi-Agent Orchestration Workflow — coordinates 6 specialized sub-agents (Planner, Coder, Tester, Reviewer, Verifier, Reporter) to complete complex coding tasks |

### Orchestrate: Execution Modes

The orchestrate command auto-classifies task complexity (LOW / MEDIUM / HIGH / CRITICAL) and selects the best execution mode:

| Mode | When | Scale |
|------|------|-------|
| **STANDARD** | Default for most tasks (1-5 files) | 6-phase pipeline |
| **TEAM_OF_TEAMS** | 2+ independent modules | Up to 4 parallel sub-teams |
| **MAP_REDUCE** | 10+ files needing similar changes | Up to 5 workers per batch |
| **HIERARCHICAL** | Architecture-wide changes | CEO → Sub-Orchestrators → Workers |
| **SPECULATIVE** | Uncertain design decisions | 2-3 competing implementations |

Model selection is automatic: Sonnet for coding tasks, Opus for planning (HIGH+), reviews, and verification.

## Installation

### Quick Install (Recommended)

```bash
git clone git@github.com:Bigenlight/claude-commands.git
cd claude-commands
bash install.sh
```

<!-- install.sh가 없으면 아래 수동 설치를 따르세요. -->

### Manual Install

Copy the command files into your global Claude commands directory:

**Windows (Git Bash / WSL):**
```bash
mkdir -p ~/.claude/commands
cp commands/*.md ~/.claude/commands/
```

**macOS / Linux:**
```bash
mkdir -p ~/.claude/commands
cp commands/*.md ~/.claude/commands/
```

That's it. Claude Code picks up `.md` files in `~/.claude/commands/` automatically — no restart needed.

## Usage

Inside any Claude Code session, type:

```
/orchestrate fix the login bug
```

```
/orchestrate add pagination to the /users API endpoint
```

```
/orchestrate refactor the database layer to use connection pooling
```

The orchestrator will:
1. Assess complexity and choose an execution mode
2. Spawn a Planner agent to analyze the codebase and create a plan
3. Spawn Coder agent(s) to implement changes (in parallel when possible)
4. Run a Tester agent to verify correctness
5. Run an Opus Reviewer for code quality review
6. Run an Opus Verifier for final validation
7. Generate a summary report

## Setting Up on Another PC

<!-- 다른 PC에서 설치하는 방법 -->

**Option A: Clone and install**
```bash
git clone git@github.com:Bigenlight/claude-commands.git
cd claude-commands
bash install.sh
```

**Option B: Manual copy**
```bash
git clone git@github.com:Bigenlight/claude-commands.git
mkdir -p ~/.claude/commands
cp claude-commands/commands/*.md ~/.claude/commands/
```

To pull updates later:
```bash
cd claude-commands
git pull
bash install.sh   # or manually cp commands/*.md ~/.claude/commands/
```

## Adding New Commands

1. Create a new `.md` file in the `commands/` directory:
   ```bash
   # 파일명이 곧 슬래시 명령어 이름이 됩니다 (e.g., review.md → /review)
   touch commands/my-command.md
   ```

2. Write your command prompt in the file. Use `$ARGUMENTS` as a placeholder for user input:
   ```markdown
   # My Command

   You are a specialized agent. Complete the following task:

   $ARGUMENTS
   ```

3. Install it:
   ```bash
   cp commands/my-command.md ~/.claude/commands/
   ```

4. Use it in Claude Code:
   ```
   /my-command do something useful
   ```

## Uninstall

### Quick Uninstall

```bash
cd claude-commands
bash uninstall.sh
```

### Manual Uninstall

Remove specific commands:
```bash
rm ~/.claude/commands/orchestrate.md
```

Remove all commands installed from this repo:
```bash
# commands/ 폴더에 있는 파일명과 동일한 파일만 삭제합니다
cd claude-commands
for f in commands/*.md; do rm -f ~/.claude/commands/$(basename "$f"); done
```

Or remove everything:
```bash
rm -rf ~/.claude/commands/
```

## License

MIT
