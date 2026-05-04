---
name: skill-publish
description: 새로 만든 스킬이나 수정한 스킬을 ~/.claude/skills/ git repo (Bigenlight/claude-commands)에 add+commit+push까지 자동 처리. README의 스킬 표 + Install 섹션도 자동 갱신. v2 — 평탄 구조 (~/.claude/skills 자체가 git repo) 기준.
tools: Bash, Read, Edit, Write, Glob
---

# skill-publish

> **v2 (2026-05-04)**: ~/.claude/skills/ 자체가 git repo가 된 평탄 구조에 맞춰 재작성. 더 이상 두 디렉토리 사이 복사 단계 없음. in-place git workflow.

## When to invoke
- "/skill-publish <skill-name>" — 새 스킬 등록 또는 기존 스킬 업데이트
- "스킬 publish 해줘", "이 스킬 git에 올려줘"

## 동작 분기 (자동 감지)

`<skill-name>` 인자로 받음. 처리 순서:

1. `~/.claude/skills/<skill-name>/SKILL.md` 존재 확인. 없으면 STOP하고 사용자에게 보고
2. `~/.claude/skills/README.md`에서 해당 스킬이 표에 이미 있는지 grep → **있으면 update mode, 없으면 new mode**
3. 모드에 따라 다음 분기

## new mode — 새 스킬 등록

### Step A: 기본 정보 수집
- `<skill-name>/SKILL.md`의 frontmatter `description`을 1줄 추출
- 사용자에게 README 표에 들어갈 한 줄 설명 확인 (간결히 1문장)

### Step B: README.md 5군데 갱신
`~/.claude/skills/README.md`를 Edit으로 다음 5군데 갱신:
1. **스킬 표** (Markdown 테이블) — 알파벳 순서로 row 추가
2. **Install 섹션** — 만약 install이 일괄 clone이면 변경 불필요
3. **개별 스킬 설명 섹션** — `## skill-name` 헤더 + 설명 추가
4. **Update 섹션** — 변경 불필요 (일괄 git pull)
5. **Uninstall 섹션** — `rm -rf ~/.claude/skills/<skill-name>` 한 줄 추가

> 실제 README 구조는 Step 1에서 Read한 README 보고 판단. 5군데 모두 적용 안 될 수 있음 — 표 + 설명 섹션만 필수.

### Step C: git add + commit + push
```bash
cd ~/.claude/skills
git add <skill-name>/ README.md
git status
git diff --cached --stat
git commit -m "feat: add <skill-name> skill

<frontmatter description 한 줄>

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
"
git push origin main
```

## update mode — 기존 스킬 갱신

### Step A: 변경 사항 확인
```bash
cd ~/.claude/skills
git diff <skill-name>/
```
- 변경 없으면 STOP하고 사용자에게 보고
- 변경 있으면 사용자에게 commit message 확인

### Step B: README 갱신 (필요 시만)
- description 변경되었으면 표 row + 설명 섹션 동기화
- 단순 내부 수정이면 README 건드리지 않음

### Step C: git add + commit + push
```bash
cd ~/.claude/skills
git add <skill-name>/
git status
# README도 변경되었으면 같이 add
git commit -m "update: <skill-name> — <변경 요약>

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
"
git push origin main
```

## 안전 점검 (양쪽 모드 공통)

publish 전 다음 확인:
1. `git status` — 다른 미관리 파일 없는지 (있으면 사용자에게 보고)
2. `git fetch origin && git log origin/main..HEAD --oneline` — origin과 동기화 됐는지. behind면 `git pull --rebase` 안내
3. `.gitignore` 영향받는 파일 없는지 (`.venv/`, `__pycache__` 자동 제외 확인)
4. SSH 키 또는 HTTPS 인증 가능한지 (push 실패 시 사용자에게 명확히 보고)

## 실패 모드

- **SKILL.md 미존재**: "디렉토리 또는 SKILL.md가 없습니다" 보고 후 STOP
- **git push 실패** (인증/network): 에러 그대로 보고. force push 절대 시도 X
- **README 갱신 충돌**: 5군데 중 일부 패턴 매칭 실패 시 사용자에게 어느 부분 수동 갱신 필요한지 안내
- **기존 origin이 평탄 구조 아님 (legacy)**: README에 `claude-commands/skills/<name>/` 패턴 나오면 마이그레이션 미완료 PC. 사용자에게 README 업데이트 권장

## v1 → v2 변경 요약 (changelog)

- 복사 단계 (`cp`/`Write`) 모두 제거 — `~/.claude/skills/` 자체가 git repo
- 작업 디렉토리 단일화 (`~/.claude/skills/` 안에서만)
- `README.md` 위치도 같은 repo 안 (`~/.claude/skills/README.md`)
- 기존 v1의 `~/claude-commands` 경로 hard-code 10곳 모두 제거
- 마이그레이션 후 첫 PC에서 sync 시: 그냥 `git clone git@github.com:Bigenlight/claude-commands.git ~/.claude/skills`
