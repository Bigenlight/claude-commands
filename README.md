# Claude Code Skills (Theo's personal collection)

Personal Claude Code skills — repo 자체가 곧 `~/.claude/skills/` 디렉토리. clone 한 방으로 10개 스킬 전부 활성화.

> **2026-05-04 마이그레이션**: 기존 `~/claude-commands/skills/<name>/` → `~/.claude/skills/<name>/` 평탄 구조로 변경. 자세한 내용은 하단 [legacy 섹션](#이전-구조-legacy) 참고.

## Skills

| Skill | Trigger | Description |
|-------|---------|-------------|
| [`/orchestrate`](#orchestrate) | Auto or manual | Multi-agent orchestration pipeline |
| [`/repo-context`](#repo-context) | Manual | Read CLAUDE.md + README to orient in any repo |
| [`/git-pull-push`](#git-pull-push) | Manual | Pull → commit → push in one shot; auto-resolves conflicts with up to 5 Opus agents |
| [`/md-img-resize`](#md-img-resize) | Manual | Auto-resize markdown image widths based on actual image dimensions |
| [`/skill-publish`](#skill-publish) | Manual | Publish a ~/.claude/skills skill to the claude-commands repo, update README, and push |
| [`/weekly-review`](#weekly-review) | Manual | Automates PARAZETTEL vault weekly review note generation with multi-agent data collection, synthesis, and validation |
| [`/us-stock-advisor`](#us-stock-advisor) | Manual | 미국 주식 시장 조사 + 전략 판단 + 리스크 리뷰를 멀티에이전트로 수행하고, 결과를 슬랙 DM으로 전송 |
| [`/multi-agent-research`](#multi-agent-research) | Manual | Survey/compare/verify 5+ papers·docs·repos and produce a single consolidated markdown report — Sonnet × N parallel extraction + Opus audit + Opus consolidation pipeline |
| [`/paper-digest`](#paper-digest) | Manual | 최근 Scholar-Inbox 스크린샷에서 추천받은 논문들을 자동으로 검색·다운로드·요약·이미지 추출해 ~/For-Neural-Network-Improvement-Private- git repo에 날짜별 md로 정리하는 스킬. 사용자가 "/paper-digest", "오늘 받은 논문 정리해줘", "scholar inbox 정리" 같은 발화로 트리거. |
| [`/vocab-collect`](#vocab-collect) | Manual | 문서(PDF·md·txt·URL)에서 Theo 수준 영어 단어를 멀티에이전트로 추출·선정해 기존 단어장과 비교 후 새 단어만 양식대로 추가. vocab-quiz와 단어장 공유 |

---

## Install

```bash
# SSH (권장 — GitHub 키 등록된 PC)
git clone git@github.com:Bigenlight/claude-commands.git ~/.claude/skills

# HTTPS (SSH 키 없는 PC)
git clone https://github.com/Bigenlight/claude-commands.git ~/.claude/skills
```

Restart Claude Code — 10개 스킬 전부 자동 활성화.

> 이미 `~/.claude/skills/` 디렉토리가 있으면 먼저 백업하거나 비워야 함. swap 절차는 [legacy 섹션](#이전-구조-legacy) 참고.

## Update

```bash
cd ~/.claude/skills
git pull
```

`git pull` 한 번이면 모든 스킬 동기화. cp 작업 없음.

## Publish (변경사항 push)

스킬을 수정했으면 그대로 commit·push:

```bash
cd ~/.claude/skills
git add <skill-name>/
git commit -m "feat(<skill>): ..."
git push
```

또는 `/skill-publish <skill-name>` 스킬 사용.

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

Model selection is automatic: Sonnet for workers (coding), Fable for the top judgment/strategy gates — planning (HIGH+), reviews, and verification.

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

## us-stock-advisor

미국 주식 시장 조사 + 전략 판단 + 리스크 리뷰를 멀티에이전트로 수행하고, 결과를 슬랙 DM으로 전송. 뉴스·매크로·기술적 분석 → 전략 수립 → 리스크 검토 → 검증 → 슬랙 보고 파이프라인. KIS API/실거래 없이 순수 리서치·판단만.

```
/us-stock-advisor <포트폴리오 정보 — 현금 잔고(USD), 보유 종목(ticker, 수량, 평단가)>
```

---

## multi-agent-research

여러 자료(논문·문서·리포·사이트 ≥5개)를 비교·검증해서 단일 markdown 보고서로 정리하는 multi-agent 파이프라인. Sonnet × N 병렬 추출 + Opus 감사 + Opus 최종 작성의 7-phase 구조 (Identify → Canonical deep-dive → Per-source extract → Reference clone+verify → Audit × 2 → Targeted re-extract → Consolidate). `orchestrate`(코딩)·`review`(단일 리포)와 달리 reading + synthesis 작업 전용. ≤4 sources이면 단일 Agent로 충분하므로 skip.

```
/multi-agent-research <topic and what facts to extract across sources>
```

핵심 컨벤션: `findings/` 폴더에 모든 산출물 저장, `_progress.md` source × fact 매트릭스로 lens-mutation 추적, citation 의무 (PDF/code/web 형식 분리), no-fabrication 룰. 최종 산출물은 working dir 루트의 `<TOPIC>_SURVEY.md`.

---

## paper-digest

최근 Scholar-Inbox 스크린샷에서 추천받은 논문들을 자동으로 arxiv 검색·다운로드·요약·이미지 추출해 `~/For-Neural-Network-Improvement-Private-` git repo에 날짜별 md로 정리하는 6-phase 파이프라인. AI 로보틱스 대학원생 페르소나로 한국어 요약 작성.

```
/paper-digest
"오늘 받은 논문 정리해줘"
"scholar inbox 정리"
```

**Phase**: 0 환경 점검 → 1 최신 스크린샷 vision 추출 → 2 arxiv 검색·PDF 다운로드 → 3+4 sub-agent × 5 병렬 (sonnet, 페이지 렌더 + layout-aware crop + Three-Pass + Quote-then-Summarize 한국어 요약) → 4.5 Opus × 5 사실성 검증 (수치·데이터셋·한계·방법 4종 환각 검출) → 5 통합 md + fuzzy match 안전망 → 6 git-pull-push 호출.

핵심 컨벤션: 논문당 한국어 ~1000자 / technical term 영어 원문 / `<br>` 적극 / bullet `-` 위주 / 모든 굵은 수치에 `<!-- p.N, "원문" -->` HTML 주석. ablation 표 환각·baseline 모델명 혼동·자릿수 실수 방지 가이드 내장. 사전 요구: `~/For-Neural-Network-Improvement-Private-` git repo + `pdftoppm`/`pdfinfo`/`curl`/`imagemagick`.

---

## vocab-collect

특정 문서(논문 PDF·md·txt·웹 URL)에서 Theo가 모를 법한 학술 영어 단어를 멀티에이전트로 추출·선정해, 기존 단어장과 비교한 뒤 중복을 뺀 새 단어만 양식에 맞게 추가. `vocab-quiz`와 **같은 단어장 파일을 공유**해서 추가 즉시 다음 퀴즈에 출제됨.

```
/vocab-collect <문서 경로 또는 URL> [개수] [대상 단어장 경로]
"이 논문에서 단어 뽑아줘"
"단어 수집"
```

**파이프라인**: 문서 길이 측정 → 개수 자동 결정(짧음~5 / 보통~10 / 긺~12–15) → 기존 단어장 summary로 중복 제외 → Sonnet × N 추출 → Opus × 3 관점별 선정 → Opus × 1 종합 + vocab/concept 자동 분류 → 섹션 끝에 양식대로 append. 입력은 로컬 파일 + 웹 URL 모두 지원. 아이콘 줄은 안 붙이고(아직 안 푼 단어), 블록은 섹션 끝에만 추가해 vocab-quiz의 id/마킹이 안 깨지게 함.

---

## Uninstall

```bash
# 전체 제거 — 다른 도구가 ~/.claude/skills에 파일을 안 두고 있다면
rm -rf ~/.claude/skills

# 특정 스킬만
rm -rf ~/.claude/skills/<skill-name>
```

---

## 이전 구조 (legacy)

2026-05-04 이전에는 다음 구조였음:

```
~/claude-commands/
├── skills/
│   ├── orchestrate/SKILL.md
│   ├── paper-digest/SKILL.md
│   └── ...
```

매번 `cp skills/<name>/SKILL.md ~/.claude/skills/<name>/SKILL.md`로 손수 복사해야 했음.

**현재 구조** (평탄화):

```
~/.claude/skills/         ← repo가 곧 이 디렉토리
├── orchestrate/SKILL.md
├── paper-digest/SKILL.md
├── ...
└── .git/
```

`git clone ... ~/.claude/skills` 한 번으로 끝. cp 단계 제거.

### 기존 PC swap 절차

기존 `~/claude-commands/` + 별도 `~/.claude/skills/` 가 있는 PC에서는 **새 셸**(Claude Code 세션 외부)에서:

```bash
# 1. 기존 ~/.claude/skills 백업
mv ~/.claude/skills ~/.claude/skills.bak.$(date +%Y%m%d)

# 2. 새 구조로 clone
git clone git@github.com:Bigenlight/claude-commands.git ~/.claude/skills

# 3. 검증 후 백업 + 옛 claude-commands 폴더 삭제
ls ~/.claude/skills
rm -rf ~/.claude/skills.bak.*
rm -rf ~/claude-commands
```

> **주의**: Claude Code 실행 중인 셸에서 `~/.claude/skills/`를 swap하면 진행 중 세션이 깨질 수 있음 (ENOENT). 반드시 새 터미널에서.

## License

MIT
