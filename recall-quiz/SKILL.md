---
name: recall-quiz
description: 임의 노트(md) 하나로 멀티에이전트가 복습 문제를 생성해 로컬 웹 퀴즈로 풀고, 결과(:repeat:/⭐)를 소스 노트 heading에 마킹 + 문제별 라이브 튜터 채팅. 모드 3개 — warmup(공부 전 사전점화)·check(방금 배운 거 확인)·recall(오랜만에 지연 복습). "복습 퀴즈 내줘", "recall quiz", "이 노트 문제 내줘", "오랜만에 복습", "공부 전 워밍업 문제", "방금 배운 거 확인 문제", "이 노트로 퀴즈", "복습하자" 요청 시.
---

# 노트 복습 퀴즈 (recall-quiz)

임의 노트 md 1개 → 멀티에이전트 파이프라인(Sonnet 생성 → Opus 큐레이션/검수)이 모드별 복습 문제를 만들고 → 로컬 웹 퀴즈로 풀면서 → `:repeat:`/`⭐`를 소스 노트에 마킹하고, 문제마다 라이브 튜터(`claude -p`)와 대화한다.

> vocab-quiz의 정신 계승: **마킹 = 서버 결정론, 콘텐츠 = LLM**. 둘을 섞지 말 것. 단 배치 피드백 반영 단계는 **없음** — 상호작용이 전부 라이브 채팅이라서.

## 모드 3개

| 축 | `warmup` | `check` | `recall` |
|---|---|---|---|
| 언제 | 공부 **전** 사전점화(pretest) | 정리 **당일 직후** 인코딩 검증 | **오랜만에**(review-due 도래) 지연 복습 |
| 난이도 | 매우 어려움 — **틀리는 게 목표** | 중간 — 성공률 높되 재인 아님 | 어려움 — desirable difficulty, 결국 성공 |
| 유형 | recall/cloze만 (추론 문제 X) | VSAQ형 recall + why + misconception | recall 중심 + application/compare **interleave** |
| 개수 | 4~6 | 6~10 | 8~12 |
| reveal_level | `minimal` (추측 후 지연 공개) | `full` (즉시 완전 공개 + 교정) | `partial`~`full` (유연) |

> 근거·세부 전략은 [references/question-principles.md](references/question-principles.md) §4 — Generate/Review 에이전트가 반드시 읽고 준수.

## 런타임 모델 배정 (고정 — Fable 절대 쓰지 말 것, 크레딧 이슈)

| 단계 | 모델 |
|---|---|
| Segment | 결정론 (코드/메인, LLM 아님) |
| Generate | **Sonnet** × N 병렬 |
| Curate | **Opus** |
| Review | **Opus** |
| 라이브 채팅 | **Sonnet** (env `RECALL_CHAT_MODEL`) |

## 실행 순서

### ① 노트 경로 확인

대상 노트 md 절대경로를 확인한다 (사용자가 안 주면 물어본다).

### ② mode 결정

- `mode=` 인자가 있으면 **그것 그대로** 사용.
- 없으면 스마트 제안 → **AskUserQuestion으로 확인** 받고 진행:
  - 노트 frontmatter `review-due` 읽기 + `git log -1 --format=%cs -- "<노트경로>"`로 최근 수정일 확인
  - 제안 규칙: 방금 정리했거나 오늘 수정된 노트 → `check` / `review-due`가 오늘이거나 과거 → `recall` / 사용자가 "공부 전"·"워밍업" 명시 → `warmup`
  - 애매하면(예: 오늘 수정됐는데 review-due도 지남) 후보 2개를 제안하고 사용자가 고르게 함

### ③ 생성 파이프라인 → questions.json

1. **Segment (결정론, 메인이 직접)** — 노트를 `^#{1,6}` heading 기준으로 섹션 분할. 코드펜스(```` ``` ````/`~~~`) 내부의 `#` 라인은 heading 아님. 각 섹션의 `heading`(원문 그대로)·`heading_line`(1-indexed)·`line_start`·`line_end`를 기록.
2. **Generate (Sonnet 병렬)** — 섹션별로 Sonnet 에이전트를 병렬로 띄워 문제 생성. 각 에이전트는:
   - `references/question-principles.md`를 **먼저 Read**하고 원칙 준수 (특히 모드별 전략 §4, red flag §8)
   - 담당 섹션 원문만 근거로 사용 — 노트에 없는 사실 지어내기 금지
   - 각 문제에 `note_anchor` **정확히** 부착: `{heading, heading_line, line_start, line_end, anchor_slug}` (anchor_slug = heading 텍스트에서 `#` 제거, 소문자화, 공백→`-`)
3. **Curate (Opus)** — 전 섹션 산출물을 모아 모드별 개수·난이도·유형 밸런스 맞추고, dedupe, 출제 순서 결정 (recall 모드는 주제 interleave).
4. **Review (Opus)** — 게이트 검수: ~90% tractable인지 / 정답 누출 없는지 / note_anchor가 실제 노트 라인과 일치하는지 / question-principles.md §8 red flag 전수 체크. 탈락 문제는 제거하거나 수정 지시 후 재생성.
5. 최종 산출을 **BUILD_SPEC §3 스키마**로 `~/.claude/skills/recall-quiz/questions.json`에 기록. 최상위: `mode`, `source_note`(절대경로), `generated_at`(ISO8601), `model_pipeline`, `questions[]`. 각 문제: `id`, `mode`, `type`(recall|cloze|application|why|compare|misconception), `difficulty`(1~5), `question`, `answer`, `hint`, `reveal_level`, `note_anchor`, `source_note`, `tags`, `generator`, `review_meta`.

### ④ 서버 실행

1. 포트 비었는지 확인: `ss -ltn | grep ':8770 '` (쓰는 중이면 env `RECALL_PORT`로 변경)
2. 백그라운드로 서버 실행:
   ```bash
   python3 ~/.claude/skills/recall-quiz/server.py "<questions.json 절대경로>"
   ```
3. `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8770/` 로 200 확인

### ⑤ 안내

사용자에게 **http://localhost:8770** 안내. 카드 풀면서 마킹·채팅이 실시간으로 동작함.

## 마킹 시맨틱

| 마커 | 의미 | 트리거 |
|---|---|---|
| `:repeat:` | 다시 볼 것 (이해 부족) | 퀴즈에서 [아예 모르겠다] |
| `⭐` | 중요 (또 보고 싶음) | 퀴즈에서 [⭐ 중요] 토글 |

- **서버가** 해당 문제 `note_anchor`의 heading 라인 끝에 **결정론·멱등** 삽입/제거 (`:repeat:` 먼저, `⭐` 나중, 토글 off면 깔끔히 제거).
- heading 라인만 편집 — **본문/수식/이미지/코드는 절대 안 건드림**. 코드펜스 내부도 무시.
- heading 없는 노트는 앵커 위치에 `> :repeat: ⭐` blockquote 줄로 fallback (역시 멱등).
- audit은 스킬 폴더 `marks.json`에 누적.

## 라이브 채팅 (문제별 튜터)

답 공개 후 카드마다 텍스트칸 + 프리셋 버튼이 뜬다:

- **[더 자세히]** (explain) — 노트 발췌 근거로 더 쉽게 재설명
- **[문제 바꿔 다시]** (regen) — 같은 note_anchor·같은 difficulty로 새 문제 교체
- **자유 입력** (freeform) — 아무 질문

서버가 `claude -p` (모델: `RECALL_CHAT_MODEL`, 기본 sonnet)를 **텍스트 전용**으로 호출한다 — Bash/Write/Edit/WebFetch/WebSearch 전부 차단된 상태라 **파일은 절대 안 건드림**. 튜터는 노트 발췌(`line_start..line_end` 원문)에 있는 근거로만 설명/재출제하고, 문제별 대화 스레드는 `chat_log.json`에 누적되어 이어진다.

## 안전 규칙

- **마킹 = 서버만** (결정론, heading 라인 한정). Claude가 직접 소스 노트에 마커 넣지 말 것.
- **채팅 = 텍스트만** (`claude -p` + disallowedTools). 채팅 경로로 파일 수정 시도 금지.
- 둘을 **섞지 말 것** — LLM이 마킹하거나, 서버가 콘텐츠를 생성하는 구조 금지.
- 소스 노트가 git 추적 파일이면 서버가 직접 수정하므로 **퀴즈 세션 끝나고 커밋 권장**.

## 옵션 / 종료

- **포트 변경**: `RECALL_PORT=8771 python3 ~/.claude/skills/recall-quiz/server.py <questions.json>`
- **채팅 모델 변경**: `RECALL_CHAT_MODEL=opus ...` (기본 `sonnet` — Fable 지정 금지)
- **서버 종료**:
  ```bash
  kill -9 $(ss -ltnp | grep ':8770 ' | grep -oP 'pid=\K[0-9]+')
  ```
- 렌더링은 `marked.js` + KaTeX CDN 사용 → 첫 로드 시 인터넷 필요.

## References

- [references/question-principles.md](references/question-principles.md) — 문제 생성/검수 원칙 (Generate·Review 에이전트 필독)
