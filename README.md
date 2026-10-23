# Claude Code Skills (Theo's personal collection)

Personal Claude Code skills — repo 자체가 곧 `~/.claude/skills/` 디렉토리. clone 한 방으로 15개 스킬 전부 활성화.

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
| [`/us-stock-advisor`](#us-stock-advisor) | Manual | 주간 결정 + 일일 워치독. 스카우트 4명 증거 수집 → Opus 클러크 검증 → Opus 결정자 3명 블라인드 투표, Python이 2/3 집계·정수 주 주문·원장·성적표 계산 후 한국어 슬랙 DM (v6) |
| [`/multi-agent-research`](#multi-agent-research) | Manual | Survey/compare/verify 5+ papers·docs·repos and produce a single consolidated markdown report — Sonnet × N parallel extraction + Opus audit + Opus consolidation pipeline |
| [`/paper-digest`](#paper-digest) | Manual | 최근 Scholar-Inbox 스크린샷에서 추천받은 논문들을 자동으로 검색·다운로드·요약·이미지 추출해 ~/For-Neural-Network-Improvement-Private- git repo에 날짜별 md로 정리하는 스킬. 사용자가 "/paper-digest", "오늘 받은 논문 정리해줘", "scholar inbox 정리" 같은 발화로 트리거. |
| [`/vocab-collect`](#vocab-collect) | Manual | 문서(PDF·md·txt·URL)에서 Theo 수준 영어 단어를 멀티에이전트로 추출·선정해 기존 단어장과 비교 후 새 단어만 양식대로 추가. vocab-quiz와 단어장 공유 |
| [`/vocab-quiz`](#vocab-quiz) | Manual | `<details>/<summary>` 포맷 단어장을 로컬 웹 퀴즈로 풀고 결과를 md에 아이콘으로 누적 마킹. vocab-collect와 단어장 공유 |
| [`/recall-quiz`](#recall-quiz) | Manual | 임의 노트 하나로 멀티에이전트가 복습 문제를 생성해 로컬 웹 퀴즈로 풀고, `:repeat:`/`⭐`를 소스 노트 heading에 마킹 + 문제별 라이브 튜터 채팅. warmup/check/recall 3모드 |
| [`/sleep-log`](#sleep-log) | Manual | 삼성헬스(갤럭시핏3) 수면 스크린샷을 vision 인식해 그날 데일리 노트 잠 블록 + sleep-tracking 기록표에 자동 기록. 일별/주별 판별, 여러 장 병합, idempotent 갱신 |
| [`/formula-quiz`](#formula-quiz) | Manual | 수식 덱을 로컬 웹 퀴즈로 공부 — 수식 + 힌트로 의미 guess → 정답 공개 시 기호마다 화살표 주석 + 해설 → ✅/❌/🛑. 1회 = 모집단 1개 최대 5개, 그리스 문자 기호 연습(랜덤 5개)도 포함 |
| [`/formula-collect`](#formula-collect) | Manual | 논문·노트에서 핵심 수식을 멀티에이전트(Sonnet 추출 → Opus 작성 → Opus 검수)로 뽑아 formula-quiz 덱에 카드로 추가. 중복 제외, 모집단 자동 배치 |

---

## Install

```bash
# SSH (권장 — GitHub 키 등록된 PC)
git clone git@github.com:Bigenlight/claude-commands.git ~/.claude/skills

# HTTPS (SSH 키 없는 PC)
git clone https://github.com/Bigenlight/claude-commands.git ~/.claude/skills
```

Restart Claude Code — 15개 스킬 전부 자동 활성화.

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

미국 주식 주간 결정 + 일일 워치독 (v6 — 2026-10-08 v5.1.2에서 롤백 후 재설계). 스카우트 4명이 최신 뉴스를 날짜·URL 근거로 수집하고, Opus 클러크가 검증해 증거 번들(`E1..En`)을 만든 뒤, 추세·실적·매크로 렌즈의 Opus 결정자 3명이 서로 모른 채 목표 비중을 투표한다. Python이 2/3 다수결 집계(`aggregate.py`)·정수 주 주문(`book.py`)·append-only 원장과 core-only 섀도우 대비 성적표(`ledger.py`)·리포트 헤더(`render.py`)를 계산하고, 결과를 한국어 슬랙 DM으로 보낸다. 모든 임계값은 `scripts/config.py`에만 둔다. 일일 워치독(`watchdog.py`)은 LLM 없이 Python만으로 시세를 마킹하고 이벤트를 감지하며, systemd user timer(`install_timer.sh`)로 돈다. 나스닥100 코어(QQQ/QQQM)가 기본값이고, 계좌 상태(`state/`)와 리포트는 git에 올리지 않는다. KIS API/실거래 없이 리서치·판단만.

```
/us-stock-advisor <보유 종목·수량·현금(USD)> 또는 "last" (원장 기준) [--deposit USD|--withdraw USD]
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

## vocab-quiz

`<details>/<summary>` 포맷의 영어 단어/개념 단어장 md를 로컬 웹 퀴즈 사이트로 띄우고, 푼 결과를 md 파일에 아이콘(✅🔁❌💯)으로 누적 마킹. `vocab-collect`와 **같은 단어장 파일을 공유**한다. 마킹(결정론적)은 서버가, 피드백 반영(LLM)은 스킬이 분리 처리.

```
/vocab-quiz <단어장.md 경로>
"단어 시험"
"단어장 퀴즈"
```

**동작**: `server.py`가 로컬 포트(기본 8765)로 퀴즈 웹앱 서빙 → 브라우저에서 풀기 → 정오답이 단어장 블록 아래 아이콘 줄로 누적. 단어장 경로는 인자 > `VOCAB_MD` env > 기본 후보경로 순으로 해석. 773 복습(3회 졸업 💯)과 연동. 덱별로 md frontmatter `quiz-batch: N`(한 판 랜덤 N개) · `quiz-graduate: false`(졸업 없이 계속 출제) 설정 가능.

---

## recall-quiz

임의 노트(md) 하나로 멀티에이전트가 모드별 복습 문제를 생성 → 로컬 웹 퀴즈로 풀고, 결과(`:repeat:`=다시 볼 것 / `⭐`=중요)를 **소스 노트 heading에 결정론적으로 마킹** + 문제별 **라이브 튜터 채팅**(`claude -p`). `vocab-quiz`의 "마킹=서버 결정론 / 콘텐츠=LLM" 패턴 계승 (단 상호작용은 배치가 아니라 라이브).

```
/recall-quiz <노트 경로> [mode=warmup|check|recall]
"이 노트 복습 퀴즈 내줘"
"오랜만에 복습" / "방금 배운 거 확인 문제" / "공부 전 워밍업"
```

**모드 3개** (인지과학 근거 기반): `warmup`(공부 전 사전점화 — 틀리는 게 목표) / `check`(당일 직후 인코딩 검증 + 오개념 즉시 교정) / `recall`(오랜만 지연 복습 — interleave·전이).

**파이프라인**: Segment(결정론 heading 분할) → Generate **Sonnet × N 병렬**(섹션별, note_anchor 부착) → Curate **Opus**(모드별 개수·난이도 밸런스) → Review **Opus**(~90% tractable·정답누출·앵커정확성 게이트) → `questions.json` → 로컬 서버(기본 포트 8770). 마킹은 서버가 heading 라인에만 **멱등** 삽입(본문/수식/이미지/코드펜스 안 건드림), 채팅은 텍스트 전용(`--disallowedTools`로 파일 미수정 보장). 문제 생성/검수 원칙은 `references/question-principles.md`(Matuschak·SuperMemo·Bloom·testing/spacing effect 종합). env: `RECALL_PORT`, `RECALL_CHAT_MODEL`(기본 sonnet, Fable 금지).

---

## sleep-log

삼성헬스(갤럭시핏3) 수면 스크린샷을 주면 값을 **vision으로 인식**해 (1) 그날 데일리 노트의 잠 블록("어제 잠 시간 및 금일 컨디션 체크")과 (2) `02-areas/sleep-management/sleep-tracking.md` 기록표에 자동 기록. WAKE date(기상일)로 노트를 매핑하고, 일별/주별 스샷을 판별해 주별이면 baseline/주별 추세로 라우팅. 여러 장(상·하 스크롤)이면 같은 날로 병합, 재실행 시 중복 없이 갱신(idempotent). 런타임은 솔로(서브에이전트 없음), 숫자는 이미지 대조 self-check 후 기록.

```
/sleep-log <수면 스샷 경로 1장+> [주관 텍스트: 기상방식/각성/컨디션/낮잠/한줄]
"잠 기록" / "수면 스샷 정리" / "어제 잠 기록해줘"
```

**동작**: 스샷 vision 추출(TIB/TST/단계/회복/SpO2 + 한국어 시간포맷 변환) → WAKE date로 데일리 노트 탐색(루트 → 00-inbox → 05-daily → 전역) → 잠 블록 삽입/갱신 + 기록표 날짜순 삽입 → 원본 스샷 `08-assets/<YYYY>/sleep/` 보관. 카페인·음주는 추적 안 함(교란은 낮잠만), 권장 실행 시점은 밤(운동·오후 컨디션 확정 후).

---

## formula-quiz

영단어장(`vocab-quiz`)의 수식 버전. 수식 덱(`02-areas/formula-deck/formula-*.md`, 모집단 1개 = 파일 1개)을 로컬 웹 퀴즈로 띄워서, **수식 + 짧은 힌트**만 보고 의미를 머릿속으로 해석 → 정답 공개(정체·의미·기호 풀이·전개·헷갈리는 점·참고 노트) → ✅ 맞췄다 / ❌ 틀렸다 / 🛑 오늘 그만. 결과는 카드 아래 `기록: ✅날짜 …` 줄에 서버가 결정론적으로 누적(연속 3✅ → 💯 졸업). 정답 공개 시 `annotate.js`가 카드의 **기호 풀이** bullet을 문제 수식과 자동 매칭해 기호마다 색칠 + 위·아래 라벨 화살표 + 호버 말풍선을 그림. **기호 연습**으로 그리스 문자 노트(vocab-quiz 형식, 기록 공유)에서 랜덤 5개 + 다른 덱에서 그 기호가 쓰인 곳 자동 첨부.

```
/formula-quiz [모집단 이름 일부 (예: flow, attention, bellman)]
"수식 퀴즈" / "오늘 수식" / "그리스 문자 연습"
```

**규칙**: 스킬 1회 = 모집단 1개(또는 일부) 최대 5개(`FORMULA_MAX` / frontmatter `quiz-batch`). 출제 우선순위 최근 ❌ → 안 본 카드 → ✅ 오래된 순, 출제 순서는 파일 순서. 저장 후 서버 자동 종료(기본 포트 8766). 유틸: `server.py check|stats|readme`, `check_katex.mjs`(KaTeX 렌더 검사), `check_annotate.mjs`(화살표 매칭 커버리지). 카드 포맷 스펙은 `references/deck_format.md`.

---

## formula-collect

논문(PDF·arXiv URL)이나 vault 노트에서 중요한 수식(모델 구조·loss/objective·update rule·핵심 전개 결과)을 뽑아 `formula-quiz` 덱에 카드로 추가. Sonnet 추출 → 메인 세션 선정·모집단 배치 → Opus 작성 → 다른 Opus 검수(작성자 ≠ 검수자). 기존 모집단에 맞으면 거기에, 아니면 새 모집단 파일 생성, 중복 카드는 제외. 작성 후 `server.py check` + `check_katex.mjs` + `check_annotate.mjs` 자가 검사.

```
/formula-collect <논문 PDF 경로 | arXiv URL | 노트 경로> [넣을 모집단]
"이 논문 수식 추가해줘" / "수식 덱에 넣어줘"
```

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
