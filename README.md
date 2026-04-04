# claude-commands

Personal Claude Code skills — shared online, installed manually on each machine.

## Skills

| Skill | Trigger | Description |
|-------|---------|-------------|
| [`/orchestrate`](#orchestrate) | Auto or manual | Multi-agent orchestration pipeline |
| [`/repo-context`](#repo-context) | Manual | Read CLAUDE.md + README to orient in any repo |
| [`/git-pull-push`](#git-pull-push) | Manual | Pull → commit → push in one shot; auto-resolves conflicts with up to 5 Opus agents |
| [`/md-img-resize`](#md-img-resize) | Manual | Auto-resize markdown image widths based on actual image dimensions |
| [`/skill-publish`](#skill-publish) | Manual | Publish a ~/.claude/skills skill to the claude-commands repo, update README, and push |
| [`/weekly-review`](#weekly-review) | Manual | Automates PARAZETTEL vault weekly review note generation with multi-agent data collection, synthesis, and validation |

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

# git-pull-push
mkdir -p ~/.claude/skills/git-pull-push
cp skills/git-pull-push/SKILL.md ~/.claude/skills/git-pull-push/SKILL.md

# md-img-resize
mkdir -p ~/.claude/skills/md-img-resize
cp skills/md-img-resize/SKILL.md ~/.claude/skills/md-img-resize/SKILL.md

# skill-publish
mkdir -p ~/.claude/skills/skill-publish
cp skills/skill-publish/SKILL.md ~/.claude/skills/skill-publish/SKILL.md

# weekly-review
mkdir -p ~/.claude/skills/weekly-review
cp skills/weekly-review/SKILL.md ~/.claude/skills/weekly-review/SKILL.md
```

Restart Claude Code — skills will be active.

## Update (all skills)

```bash
cd claude-commands
git pull

cp skills/orchestrate/SKILL.md ~/.claude/skills/orchestrate/SKILL.md
cp skills/repo-context/SKILL.md ~/.claude/skills/repo-context/SKILL.md
cp skills/git-pull-push/SKILL.md ~/.claude/skills/git-pull-push/SKILL.md
cp skills/md-img-resize/SKILL.md ~/.claude/skills/md-img-resize/SKILL.md
cp skills/skill-publish/SKILL.md ~/.claude/skills/skill-publish/SKILL.md
cp skills/weekly-review/SKILL.md ~/.claude/skills/weekly-review/SKILL.md
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

## git-pull-push

Pull → commit → push를 한 번에 처리. 충돌 발생 시 Opus 에이전트를 최대 5명 동원해서 자동 resolve.

```
/git-pull-push
```

수행 순서: `git status` 확인 → 변경사항 있으면 커밋 → `git pull --rebase` → `git push`

충돌 해결 우선순위:
1. **두 내용 공존** — 서로 다른 내용이면 둘 다 살림
2. **로컬 우선** — 공존 불가 시 로컬 커밋 내용 채택

---

## md-img-resize

Auto-resizes `<img>` tag widths in markdown files based on actual image dimensions. Handles both `![](path)` and `<img src="...">` patterns.

```
/md-img-resize path/to/file.md
/md-img-resize                  ← uses file mentioned in conversation
```

Width formula: `max(380, min(int(ratio × 420), 860))` where `ratio = width / height`

| ratio range | typical subject | applied width |
|-------------|----------------|---------------|
| ≥ 2.0 | wide figures, tables | ~860px |
| 1.5–2.0 | landscape screenshots | ~700px |
| 1.0–1.5 | square-ish images | ~500px |
| < 1.0 | portrait / paper scans | ~380px |

Requires Pillow: `pip install Pillow`

---

## skill-publish

Publishes a `~/.claude/skills/<name>` skill to this repo. Reads `description` and `argument-hint` from SKILL.md frontmatter, then automatically updates README (table, Install, Update, Uninstall, description section) and commits + pushes.

```
/skill-publish <skill-name>
```

---

## weekly-review

PARAZETTEL 연구 vault의 주간 리뷰를 자동 생성. Sonnet 6개로 데이터 병렬 수집 → Opus로 종합 작성 → Opus로 검수하는 3-phase 파이프라인.

```
/weekly-review (optional) specific week like "2026-W15" — defaults to current week
```

수집 대상: 데일리 노트, 미팅 노트, inbox/root 노트, review-due 노트, git log, 프로젝트/영역 상태. 최종 보고 시 참고한 전체 파일 목록 출력.

---

## Uninstall

```bash
rm -rf ~/.claude/skills/orchestrate
rm -rf ~/.claude/skills/repo-context
rm -rf ~/.claude/skills/git-pull-push
rm -rf ~/.claude/skills/md-img-resize
rm -rf ~/.claude/skills/skill-publish
rm -rf ~/.claude/skills/weekly-review
```

## License

MIT
