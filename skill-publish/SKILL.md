---
name: skill-publish
description: 새로 만든 스킬이나 수정한 스킬을 ~/.claude/skills/ git repo (Bigenlight/claude-commands)에 add+commit+push까지 자동 처리. README의 스킬 표 + Install 섹션도 자동 갱신. v2 — 평탄 구조 (~/.claude/skills 자체가 git repo) 기준.
version: 2.0.0
argument-hint: <skill-name>
allowed-tools: [Bash, Read, Edit, Write, Grep, Glob]
---

# skill-publish

`~/.claude/skills/` **자체가 git repo다** (remote: `git@github.com:Bigenlight/claude-commands.git`, 기본 브랜치 `main`). 각 스킬은 `~/.claude/skills/<skill-name>/SKILL.md` 형태의 평탄(flat) 구조이고, `~/.claude/skills/README.md`가 전체 스킬 표 + Install/Update/Uninstall 섹션을 관리한다. 이 스킬은 그 repo 안에서 **in-place로** add → README 동기화 → commit → push까지 처리한다. 복사 단계는 없다.

> Windows에서 `~` = `C:\Users\<user>`. Git Bash 기준 `cd ~/.claude/skills`로 진입. 모든 git 명령은 이 디렉토리 안에서 실행.

## When to invoke

- `/skill-publish <skill-name>` — 새 스킬 등록 또는 기존 스킬 업데이트
- "스킬 publish 해줘", "이 스킬 git에 올려줘"
- `<skill-name>` 인자가 없으면: 이 대화에서 방금 만들거나 수정한 스킬이 명확하면 그걸 사용, 아니면 사용자에게 물어볼 것

---

## Step 0 — preflight (모든 모드 공통, 반드시 먼저)

```bash
cd ~/.claude/skills
git remote -v          # origin이 Bigenlight/claude-commands인지 확인
git status --short     # 대상 스킬 외 미관리/변경 파일 파악
git fetch origin
git rev-list --left-right --count origin/main...HEAD
```

- **`rev-list --left-right --count` 해석: 왼쪽 숫자 = behind (origin에만 있는 커밋), 오른쪽 숫자 = ahead (로컬에만 있는 커밋).**
    - behind > 0 → **push 전에 `git pull --rebase origin main` 먼저.** rebase 충돌 나면 사용자에게 보고하고 STOP (임의 해결 금지)
    - ahead > 0 → 이전에 push 못 한 커밋이 있다는 뜻. 이번 push에 같이 나가게 됨을 사용자에게 알릴 것
- `git status`에 **대상 스킬과 무관한** 변경/untracked 파일이 있으면: 커밋에 섞지 말고 사용자에게 목록 보고. 같이 올릴지 물어볼 것
- 대상 확인: `~/.claude/skills/<skill-name>/SKILL.md` 존재해야 함. 없으면 "디렉토리 또는 SKILL.md가 없습니다" 보고 후 STOP

## Step 1 — 모드 자동 감지

`~/.claude/skills/README.md`를 Read하고, 스킬 표(`## Skills` 아래 Markdown 테이블)에 `<skill-name>` row가 있는지 확인:

- 표에 **없으면 → new mode**
- 표에 **있으면 → update mode**

이때 대상 스킬의 frontmatter에서 `description`과 `argument-hint`를 추출해 둔다 (README 갱신 재료). `argument-hint`가 없으면 usage 예시는 `/skill-name`만으로 작성.

---

## new mode — 새 스킬 등록

### Step A: README.md 갱신

Edit으로 다음을 갱신 (실제 README 구조를 Read한 것 기준으로 판단):

1. **스킬 표** — `| [`/skill-name`](#skill-name) | Manual | <한 줄 설명> |` row를 **표 맨 아래에 추가**. 현재 표는 알파벳순이 아니라 **추가된 순서**이므로 정렬하지 말 것. 한 줄 설명은 frontmatter description을 1문장으로 압축 (표가 길어지지 않게)
2. **개별 스킬 설명 섹션** — 기존 섹션들과 같은 포맷으로, `## Uninstall` 위 (마지막 스킬 섹션 뒤)에 추가:

    ````markdown
    ## skill-name

    <설명 2–4문장 — frontmatter description + SKILL.md 본문 요약>

    ```
    /skill-name <argument-hint 내용>       ← argument-hint 없으면 /skill-name만
    ```
    ````

3. **스킬 개수 문구 동기화** — 아래 「개수·표 정합성 체크」 수행

### Step B: 개수·표 정합성 체크 (new/update 공통 — 매 publish마다 수행)

README의 "N개 스킬" 문구와 표를 **실제 스킬 디렉토리 개수 기준**으로 동기화:

```bash
cd ~/.claude/skills
ls -d */ | while read d; do [ -f "$d/SKILL.md" ] && echo "$d"; done   # 실제 스킬 목록
```

- **기준값 N = `SKILL.md`를 가진 디렉토리 개수** (README.md, .git 등은 제외)
- README에서 "N개 스킬" 패턴 전부 grep (`grep -n '개 스킬' README.md`) → 상단 소개 문구, Install 섹션 등 **나오는 곳 모두** N으로 갱신
- **표 row 개수 == N**인지 검증. 표에 누락된 스킬이 있으면 (repo에 디렉토리는 있는데 row가 없는 경우): 누락 스킬의 frontmatter description을 읽어 **표 row + 설명 섹션을 함께 추가**하고, 사용자에게 "누락돼 있던 X도 이번에 표에 추가했다"고 보고. 반대로 표에는 있는데 디렉토리가 없는 스킬이 있으면 지우지 말고 사용자에게 보고만 (삭제는 사용자 판단)

> Install 명령 자체(`git clone ...`)는 일괄 clone이라 변경 불필요. Update 섹션(`git pull`)도 변경 불필요. Uninstall 섹션은 `rm -rf ~/.claude/skills/<skill-name>` 제네릭 패턴이라 보통 변경 불필요 — 스킬별 라인을 나열하는 방식으로 바뀐 경우에만 한 줄 추가.

### Step C: git add + commit + push

```bash
cd ~/.claude/skills
git add <skill-name>/ README.md
git status
git diff --cached --stat
git commit -m "feat: add <skill-name> skill

<frontmatter description 핵심 한 줄>

Co-Authored-By: <현재 세션 모델명> <noreply@anthropic.com>"
git push origin main
```

> **Co-Authored-By 모델명은 절대 하드코딩하지 말 것.** 이 세션이 실제로 쓰고 있는 모델명(시스템 프롬프트에 명시된 것)으로 채운다. 예전 스킬 파일이나 과거 커밋에 적힌 모델명을 복붙하면 stale해짐.

---

## update mode — 기존 스킬 갱신

### Step A: 변경 사항 확인

```bash
cd ~/.claude/skills
git status --short <skill-name>/   # ← untracked 새 파일까지 잡힘. diff만으로는 못 잡으니 반드시 이걸 먼저
git diff <skill-name>/             # tracked 파일의 실제 변경 내용
```

- `git status --short`가 비어 있으면 (변경도 untracked도 없음) → "변경 사항 없음" 보고 후 STOP
- 변경 있으면 diff 요약을 보고 커밋 메시지 초안을 만들어 사용자에게 확인

### Step B: README 동기화 (필요 시)

- frontmatter `description` 또는 `argument-hint`가 바뀌었으면 → 표 row 한 줄 설명 + 개별 설명 섹션 + usage 예시를 새 내용으로 동기화
- major 버전 범프급 재작성이면 설명 섹션 본문도 현행화
- 스킬 내부 로직만 바뀐 경우 README는 건드리지 않음
- **단, 「개수·표 정합성 체크」(new mode Step B)는 update mode에서도 매번 수행** — 표 누락/개수 stale이 발견되면 이번 커밋에서 같이 고침

### Step C: git add + commit + push

```bash
cd ~/.claude/skills
git add <skill-name>/
# README도 변경했으면: git add README.md
git status
git diff --cached --stat
git commit -m "update: <skill-name> — <변경 요약>

Co-Authored-By: <현재 세션 모델명> <noreply@anthropic.com>"
git push origin main
```

---

## 실패 모드

| 상황 | 대응 |
|------|------|
| `<skill-name>/SKILL.md` 미존재 | "디렉토리 또는 SKILL.md가 없습니다" 보고 후 STOP |
| behind 상태에서 `git pull --rebase` 충돌 | 충돌 파일 목록 보고 후 STOP. 임의 해결·`--force` 금지 |
| `git push` 실패 (인증/network) | 에러 원문 그대로 보고. **force push 절대 시도 X.** SSH 키 미등록 PC면 HTTPS remote 전환을 제안만 |
| README 패턴 매칭 실패 (표/섹션 구조가 예상과 다름) | 어느 부분을 수동 갱신해야 하는지 구체적으로 안내. 억지로 Edit하지 말 것 |
| `~/.claude/skills`가 git repo가 아님 | 마이그레이션 미완료 PC. `git clone git@github.com:Bigenlight/claude-commands.git ~/.claude/skills` (기존 디렉토리는 새 셸에서 백업 후 swap — README legacy 섹션 참고) 안내 후 STOP |

## 체크리스트 (완료 보고 전 자가 검증)

- [ ] behind/ahead 확인했고 behind면 rebase 먼저 했다 (`--left-right --count`: 좌=behind, 우=ahead)
- [ ] `git status --short <skill>/`로 untracked 포함 변경을 확인했다
- [ ] README 표 row 개수 == 실제 스킬 디렉토리 개수, "N개 스킬" 문구도 일치
- [ ] 커밋 트레일러 모델명은 현재 세션 기준
- [ ] 대상 스킬과 무관한 파일이 커밋에 섞이지 않았다
- [ ] push 성공 확인 (`git rev-list --left-right --count origin/main...HEAD` → `0	0`)

## Changelog

- **2.0.0 (2026-07-07)**: 평탄 구조를 1급 전제로 전면 재작성. behind 체크를 `git rev-list --left-right --count origin/main...HEAD`로 (기존 `git log origin/main..HEAD`는 ahead만 보임). `tools:` → `allowed-tools:`로 교정 (Claude Code는 `tools` 키 무시). update mode에 `git status --short` 선행 (untracked 감지). README 표는 추가된 순서 유지. 개수 문구·표 누락을 실제 디렉토리 기준으로 매 publish마다 동기화. 커밋 트레일러 모델명 하드코딩 금지. `argument-hint`를 usage 예시 생성에 실제로 사용.
- **v2 (2026-05-04)**: `~/.claude/skills/` 자체가 git repo가 된 평탄 구조로 전환. 복사 단계 제거, in-place git workflow.
