---
name: skill-publish
description: Publish a ~/.claude/skills skill to the claude-commands repo, update README, and push
version: 1.0.0
argument-hint: <skill-name>
allowed-tools: [Read, Write, Edit, Bash]
---

`~/.claude/skills/<skill-name>` 에 있는 스킬을 `~/claude-commands` 리포에 추가하고, README를 업데이트한 뒤 커밋/푸시해.

## 수행 순서

### 1. 스킬 이름 결정
- argument로 스킬 이름이 주어지면 그걸 사용
- 없으면 `~/.claude/skills/` 디렉토리를 스캔해서 로컬에 설치된 스킬 목록을 출력하고, 사용자에게 어떤 스킬을 publish할지 선택하게 할 것:
  ```bash
  ls ~/.claude/skills/
  ```
  출력 예시:
  ```
  로컬에 설치된 스킬:
  - orchestrate
  - repo-context
  - git-pull-push
  - md-img-resize
  - skill-publish
  
  어떤 스킬을 claude-commands에 추가할까요?
  ```
- 이미 claude-commands에 있는 스킬(`~/claude-commands/skills/` 에 존재하는 것)은 "(already published)" 표시

### 2. 소스 파일 확인
`~/.claude/skills/<name>/SKILL.md` 존재 여부 확인:
- 없으면 에러 출력 후 종료
- 있으면 Read 툴로 읽어서 frontmatter 파싱:
  - `description` 필드 추출 (README 테이블·설명 섹션용)
  - `argument-hint` 필드 추출 (사용법 표시용)

### 3. claude-commands에 복사
```bash
mkdir -p ~/claude-commands/skills/<name>
```
Write 툴로 `~/claude-commands/skills/<name>/SKILL.md` 에 동일한 내용 저장.

### 4. README.md 업데이트
`~/claude-commands/README.md` 를 Read한 뒤 아래 5군데를 Edit으로 수정.

**① Skills 테이블** — 마지막 스킬 행 다음에 추가:
```
| [`/<name>`](#<name>) | Manual | <description> |
```

**② Install 코드블록** — 마지막 cp 명령어 다음에 추가:
```bash
# <name>
mkdir -p ~/.claude/skills/<name>
cp skills/<name>/SKILL.md ~/.claude/skills/<name>/SKILL.md
```

**③ Update 코드블록** — 마지막 cp 명령어 다음에 추가:
```bash
cp skills/<name>/SKILL.md ~/.claude/skills/<name>/SKILL.md
```

**④ 스킬 설명 섹션** — `## Uninstall` 바로 앞에 삽입 (`---` 포함):
```markdown
## <name>

<description>

```
/<name> <argument-hint>
```

---
```

**⑤ Uninstall 섹션** — 마지막 `rm` 명령어 다음에 추가:
```bash
rm -rf ~/.claude/skills/<name>
```

### 5. 커밋 & 푸시
```bash
cd ~/claude-commands
git add skills/<name>/SKILL.md README.md
git commit -m "feat: add <name> skill

<description>

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
git push
```

### 6. 완료 보고
추가된 스킬 이름, 커밋 해시, 푸시 결과 출력.

## 주의사항
- `~/claude-commands` 경로는 실제 경로 `/home/theo_lab/claude-commands` 로 해석
- `~/.claude/skills` 경로는 `/home/theo_lab/.claude/skills` 로 해석
- SKILL.md frontmatter에서 파싱한 실제 description을 README에 삽입 (하드코딩 금지)
