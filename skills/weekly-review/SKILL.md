---
name: weekly-review
description: Use this skill when the user asks to generate a weekly review, 주간 리뷰 작성, weekly summary, or /weekly-review. Automates PARAZETTEL vault weekly review note generation with multi-agent data collection, synthesis, and validation.
version: 1.0.0
argument-hint: (optional) specific week like "2026-W15" — defaults to current week
allowed-tools: [Read, Glob, Grep, Bash, Write, Edit, Agent]
---

# Weekly Review 자동 생성 워크플로우

PARAZETTEL 연구 vault의 주간 리뷰를 자동으로 생성하는 skill.
3단계 multi-agent pipeline으로 동작: **데이터 수집 → 종합 작성 → 검수**.

## Task

$ARGUMENTS

---

## Step 0: 날짜 계산

리뷰 대상 주를 먼저 계산함. `$ARGUMENTS`에 특정 주가 주어지면 (예: "2026-W15") 해당 주를 사용, 없으면 오늘 기준 현재 주.

아래 Bash 명령으로 날짜를 계산할 것:

```bash
python -c "
import datetime, sys

# Parse target week
args = sys.argv[1] if len(sys.argv) > 1 else ''
today = datetime.date.today()

if args and args.startswith('20') and '-W' in args:
    # Parse YYYY-WNN format
    year, week = args.split('-W')
    year, week = int(year), int(week)
    # ISO week: Monday of that week
    mon = datetime.date.fromisocalendar(year, week, 1)
    sun = datetime.date.fromisocalendar(year, week, 7)
else:
    # Current ISO week
    year, week, _ = today.isocalendar()
    mon = datetime.date.fromisocalendar(year, week, 1)
    sun = datetime.date.fromisocalendar(year, week, 7)

# date-range: Monday ~ today (or Sunday if reviewing past week)
end_date = min(today, sun)

print(f'YEAR={year}')
print(f'WEEK={week:02d}')
print(f'MON={mon}')
print(f'SUN={sun}')
print(f'END_DATE={end_date}')
print(f'FILENAME={year}-W{week:02d}.md')
print(f'DATE_RANGE={mon} ~ {end_date}')

# Print each day of the week for file searching
for i in range(7):
    d = mon + datetime.timedelta(days=i)
    if d <= end_date:
        print(f'DAY_{i}={d}')
" "$ARGUMENTS"
```

이 결과를 변수로 저장해두고 이후 모든 단계에서 사용.

---

## Step 1: Phase 1 — 병렬 데이터 수집 (6개 Agent, 모두 sonnet)

6개의 Agent를 **동시에** 실행. 각 Agent는 독립적이므로 병렬 호출 가능.
모든 Agent에 `model: "sonnet"` 지정할 것.

### Agent 1: Daily Notes Reader

**prompt:**
```
너는 PARAZETTEL vault의 데일리 노트를 읽는 수집기임.

대상 주간: {MON} ~ {END_DATE}

작업:
1. 아래 경로들에서 해당 주의 데일리 노트를 찾아 읽어라 (데일리 노트가 아직 정리 안 돼서 여기저기 흩어져 있을 수 있음):
   - 05-daily/{YEAR}/{MM}/ 폴더 내 해당 날짜 파일들 (YYYY-MM-DD.md)
   - root 레벨 (vault 최상위)에 있는 YYYY-MM-DD.md 파일들 (아직 이동 안 된 것)
   - 00-inbox/ 에 있는 YYYY-MM-DD.md 파일들 (inbox에 던져둔 경우)

   추가로 Glob "**/{DAY_0}*" ~ "**/{DAY_6}*" 패턴으로 vault 전체를 검색해서 혹시 다른 곳에 있는 데일리 노트도 찾아라.

   구체적으로 찾아야 할 날짜: {각 DAY_0 ~ DAY_6 나열}

2. 각 노트에서 다음을 추출:
   - **Status** 섹션 내용
   - **Focusing** 섹션 내용 (논문 깊이 표시 포함)
   - **To do** 섹션: 완료된 항목 `[x]`과 미완료 항목 `[ ]` 분리
   - **메모 / 아이디어** 섹션 내용

3. 출력 형식 — 날짜별로 정리:
   ```
   ## YYYY-MM-DD (요일)
   ### Status
   (내용)
   ### Focusing
   (내용)
   ### To do — 완료
   - [x] 항목들
   ### To do — 미완료
   - [ ] 항목들
   ### 메모 / 아이디어
   (내용)
   ```

파일이 없는 날은 "노트 없음"으로 표시.
내용을 요약하지 말고 원문 그대로 추출할 것.

**중요**: 출력 마지막에 반드시 "읽은 파일 목록"을 붙여라:
```
## 참고한 파일
- 파일경로1
- 파일경로2
...
```
```

### Agent 2: Meeting Notes Reader

**prompt:**
```
너는 PARAZETTEL vault의 미팅 노트를 읽는 수집기임.

대상 주간: {MON} ~ {END_DATE}

작업:
1. 아래 경로들에서 미팅 노트를 찾아라 (미팅 노트가 정리 안 돼서 다른 곳에 있을 수 있음):
   - 06-meetings/**/*.md ← 메인 위치
   - 00-inbox/ 내에서 frontmatter에 type: meeting-note가 있거나, 파일명/내용에 "meeting", "미팅", "weekly" 등이 포함된 파일
   - root 레벨에서 미팅 관련 파일 (파일명이나 frontmatter로 판별)
   - Grep으로 vault 전체에서 "type: meeting-note" 검색해서 누락된 미팅 노트 추가 탐색
2. 각 파일의 frontmatter에서 date 필드를 확인하거나, 파일명에서 날짜를 추출
3. 이번 주 범위({MON} ~ {END_DATE})에 해당하는 노트만 선택
4. 각 미팅 노트에서 추출:
   - date, type (lab/3D-VLA/value-function 등)
   - 주요 논의 주제 (heading 기준)
   - action item이나 추후 진행사항

5. 출력 형식:
   ```
   ## 미팅 목록
   ### YYYY-MM-DD — {미팅 유형}
   - 파일: {경로}
   - 주요 주제: ...
   - Action items: ...
   ```

이번 주에 미팅이 없으면 "이번 주 미팅 없음"으로 출력.

**중요**: 출력 마지막에 반드시 "읽은 파일 목록"을 붙여라:
```
## 참고한 파일
- 파일경로1
- 파일경로2
...
```
```

### Agent 3: Inbox & Root Notes Reader

**prompt:**
```
너는 PARAZETTEL vault의 inbox와 root 레벨 노트를 스캔하는 수집기임.

작업:
1. 00-inbox/ 내 모든 .md 파일 목록 (README.md 제외)
   - 각 파일의 제목(첫 heading 또는 파일명)과 간략한 내용 (첫 3~5줄)

2. vault root 레벨의 .md 파일 목록 (README.md, CLAUDE.md 제외, YYYY-MM-DD.md 형식의 데일리 노트 제외)
   - 각 파일의 제목과 간략한 내용

3. 출력 형식:
   ```
   ## Inbox (N개)
   - 파일명1: 간략 내용
   - 파일명2: 간략 내용

   ## Root 레벨 미분류 노트 (N개)
   - 파일명1: 간략 내용
   ```

파일이 없으면 "비어있음"으로 표시.

**중요**: 출력 마지막에 반드시 "읽은 파일 목록"을 붙여라:
```
## 참고한 파일
- 파일경로1
- 파일경로2
...
```
```

### Agent 4: Review-Due Scanner

**prompt:**
```
너는 PARAZETTEL vault의 복습 일정을 스캔하는 수집기임.

오늘 날짜: {END_DATE}
다음 주 일요일: {다음 주 SUN 날짜}

작업:
1. 03-knowledge/ 와 04-literature/ 하위 모든 .md 파일에서 Grep으로 "review-due:" 포함된 파일을 찾아라
2. 각 파일에서 review-due 날짜와 review-count를 추출
3. review-count: 3인 것은 제외 (이미 773 완료)
4. "2026-XX-XX" 같은 placeholder 날짜도 제외
5. 분류:
   - **기한 지남 (overdue)**: review-due < 오늘
   - **이번 주 내 (due this week)**: 오늘 ≤ review-due ≤ {SUN}
   - **다음 주 (due next week)**: {SUN 다음날} ≤ review-due ≤ {다음 주 SUN}

6. 출력 형식:
   ```
   ## 복습 대상
   ### 기한 지남 (N개)
   - [[파일명]] — review-due: YYYY-MM-DD, count: N

   ### 이번 주 (N개)
   - [[파일명]] — review-due: YYYY-MM-DD, count: N

   ### 다음 주 (N개)
   - [[파일명]] — review-due: YYYY-MM-DD, count: N
   ```

**중요**: 출력 마지막에 반드시 "읽은 파일 목록"을 붙여라:
```
## 참고한 파일
- 파일경로1
- 파일경로2
...
```
```

### Agent 5: Projects & Areas Status

**prompt:**
```
너는 PARAZETTEL vault의 프로젝트와 영역 상태를 확인하는 수집기임.

대상 주간: {MON} ~ {END_DATE}

작업:
1. 01-projects/ 와 02-areas/ 하위 모든 .md 파일을 Glob으로 찾아라
2. git log로 이번 주에 수정된 파일 필터링:
   ```bash
   git log --since="{MON}" --until="{END_DATE + 1일}" --name-only --pretty=format: -- "01-projects/" "02-areas/"
   ```
3. 수정된 파일은 내용 요약 (첫 heading + 주요 변경 내용)
4. 수정 안 된 파일은 목록만 표시

5. 출력 형식:
   ```
   ## Projects (이번 주 수정)
   - 파일명: 변경 내용 요약

   ## Projects (변경 없음)
   - 파일명 목록

   ## Areas (이번 주 수정)
   - 파일명: 변경 내용 요약

   ## Areas (변경 없음)
   - 파일명 목록
   ```

**중요**: 출력 마지막에 반드시 "읽은 파일 목록"을 붙여라:
```
## 참고한 파일
- 파일경로1
- 파일경로2
...
```
```

### Agent 6: Git Activity

**prompt:**
```
너는 PARAZETTEL vault의 git 활동을 분석하는 수집기임.

대상 주간: {MON} ~ {END_DATE}

작업:
1. 아래 git 명령들을 실행:
   ```bash
   # 이번 주 커밋 로그
   git log --since="{MON}" --until="{END_DATE + 1일}" --oneline --stat

   # 커밋 수
   git log --since="{MON}" --until="{END_DATE + 1일}" --oneline | wc -l

   # 추가/수정된 파일 목록
   git log --since="{MON}" --until="{END_DATE + 1일}" --name-status --pretty=format:

   # 가장 활발한 폴더 (상위 5개)
   git log --since="{MON}" --until="{END_DATE + 1일}" --name-only --pretty=format: | sort | uniq -c | sort -rn | head -20
   ```

2. 출력 형식:
   ```
   ## Git 활동 요약
   - 총 커밋 수: N
   - 추가된 파일: N개
   - 수정된 파일: N개
   - 삭제된 파일: N개

   ## 가장 활발한 폴더 (상위 5)
   1. 폴더명 — N개 변경

   ## 커밋 목록
   - hash: 메시지
   ```

**중요**: 출력 마지막에 실행한 git 명령어 목록과 "참고한 파일" 목록을 붙여라 (파일을 직접 Read하지 않았으면 "참고한 파일: 없음 (git log만 사용)"으로 표기):
```
## 참고한 파일
- (파일 목록 또는 "없음 (git log만 사용)")
```
```

---

## Step 2: Phase 2 — 종합 작성 (Opus agent, 1개)

Phase 1의 6개 Agent 결과를 모두 모아서 하나의 Agent에 전달.
이 Agent는 **model 지정 없이** (기본 opus) 실행.

### 이전 주 리뷰 확인

Agent 실행 전에, 07-reviews/ 에서 **직전 주 리뷰 파일**을 읽어둘 것.
예: 현재 W15면 07-reviews/{YEAR}-W{WEEK-1:02d}.md를 읽음.
없으면 "이전 주 리뷰 없음"으로 처리.

이전 주 리뷰가 있으면 "다음 주 목표" 섹션을 추출해서 Phase 2 Agent에 함께 전달.

### Synthesis Agent prompt:

```
너는 PARAZETTEL 연구 vault의 주간 리뷰를 작성하는 에이전트임.

## 대상 정보
- 주차: {YEAR}-W{WEEK}
- 범위: {MON} ~ {END_DATE}
- 출력 파일: 07-reviews/{FILENAME}

## 수집된 데이터

### [Agent 1] 데일리 노트
{Agent 1 결과}

### [Agent 2] 미팅 노트
{Agent 2 결과}

### [Agent 3] Inbox & Root
{Agent 3 결과}

### [Agent 4] Review-Due
{Agent 4 결과}

### [Agent 5] Projects & Areas
{Agent 5 결과}

### [Agent 6] Git Activity
{Agent 6 결과}

### 이전 주 "다음 주 목표"
{이전 주 리뷰의 다음 주 목표 섹션 또는 "이전 주 리뷰 없음"}

## 작성 규칙

### frontmatter
```yaml
---
type: weekly-review
week: "{YEAR}-W{WEEK}"
date-range: "{MON} ~ {END_DATE}"
tags:
  - type/weekly-review
---
```

### 섹션 구성 (이 순서대로 작성)

1. **이번 주 요약**
   - 3~6개 bullet point로 핵심만
   - 총 커밋 수 포함 (예: "총 N개 커밋")
   - 이전 주 "다음 주 목표" 대비 실제 달성도 언급 (이전 주 리뷰가 있는 경우)

2. **3D VLA**
   - 이번 주 3D VLA 관련 활동 (미팅, 논문 읽기, 구현 등)
   - 데일리 Focusing에서 3D VLA/Spatial VLA/depth 관련 내용 추출
   - 해당 미팅 내용 반영
   - 없으면 "이번 주 활동 없음" 한 줄

3. **Value Function**
   - 이번 주 Value Function 관련 활동
   - RL/value function 관련 내용 추출
   - 없으면 "이번 주 활동 없음" 한 줄

4. **개인 공부 / 수업**
   - PyTorch 공부, 선형대수, 딥러닝 기초, 수업, 세미나, 행정 등
   - 데일리 To do에서 공부/수업 관련 항목 추출

5. **인프라 / 도구 세팅**
   - Docker, git, 서버 접속, 환경 세팅 등 tool 관련 작업
   - 03-knowledge/tool-* 관련 변경 포함
   - 없으면 이 섹션 생략

6. **이번 주 복습 대상**
   - Agent 4 결과 기반
   - overdue + 이번 주 due 노트를 [[wikilink]] 형식으로 나열
   - 다음 주 due 노트도 "다음 주 예정"으로 별도 표시

7. **새로 생성한 Permanent Notes**
   - 이번 주 git log에서 03-knowledge/ 에 새로 추가된 파일
   - [[wikilink]] 형식으로 나열
   - 없으면 "이번 주 새 permanent note 없음"

8. **아쉬운 점 / 개선할 것**
   - 반복적으로 미완료된 To do 항목 (여러 날 걸쳐 나타나는 [ ])
   - 구조적 개선 사항
   - 이전 주 목표 중 달성 못한 것
   - **주의**: inbox에 노트가 쌓여 있는 것 자체는 정상 워크플로우임 ← "방치"로 해석해서 여기에 넣지 말 것. inbox는 주중 누적 → 리뷰 *이후* 일괄 정리하는 흐름

9. **다음 주 목표**
   - 서브섹션: Domain A (3D VLA) / Domain B (Value Function) / 개인 공부
   - 미완료 To do + 이번 주 흐름 기반으로 현실적 목표 설정
   - 각 2~4개 bullet

10. **이번 주 Inbox 누적 현황**
    - 현재 inbox 파일 수 (= 이번 리뷰 직후 정리할 대상 수)
    - 카테고리별 카운트만 간략히 (예: 데일리 N개, 미팅 N개, 논문 N개, 기타 N개)
    - **분류 제안/이동 작업은 여기서 하지 말 것** ← 주간 리뷰는 inbox를 *입력 데이터*로 읽는 단계고, 실제 inbox 비우기는 리뷰 끝난 *후* 별도로 진행함

### 스타일 규칙 (필수)
- **구어체 반말** 사용: "~됨", "~함", "~해야 함", "~인 듯"
- 격식체(~합니다, ~입니다) 절대 금지
- 기술 용어는 영어 유지: "Transformer", "Docker", "value function"
- **굵게** — 핵심 개념, 중요한 것
- `← ` 화살표 — 부연 설명, 이유
- `>` blockquote — 참고/주의
- [[wikilink]] — vault 내 노트 참조
- 데이터에 없는 내용은 절대 창작하지 말 것
- 간결하게, 불필요한 줄 늘리기 금지
- 이모지 사용하지 말 것

이 규칙을 지키면서 Write 도구로 07-reviews/{FILENAME} 에 파일을 작성해라.
```

---

## Step 3: Phase 3 — 검수 (Opus agent, 1개)

Phase 2가 작성한 파일을 읽고, Phase 1 데이터와 대조하여 검수.
이 Agent도 **model 지정 없이** (기본 opus) 실행.

### Validation Agent prompt:

```
너는 PARAZETTEL vault 주간 리뷰의 검수 에이전트임.
작성된 리뷰 파일과 원본 데이터를 비교해서 문제를 찾고 수정해라.

## 검수 대상
- 파일: 07-reviews/{FILENAME}
  (Read 도구로 읽어라)

## 원본 데이터 (Phase 1 수집 결과)

### [Agent 1] 데일리 노트
{Agent 1 결과}

### [Agent 2] 미팅 노트
{Agent 2 결과}

### [Agent 3] Inbox & Root
{Agent 3 결과}

### [Agent 4] Review-Due
{Agent 4 결과}

### [Agent 5] Projects & Areas
{Agent 5 결과}

### [Agent 6] Git Activity
{Agent 6 결과}

## 검수 체크리스트

하나씩 확인하고 결과를 보고해라:

1. **데일리 To do 반영 여부**
   - 모든 완료 항목 `[x]`이 리뷰에 반영되었는가?
   - 미완료 항목 `[ ]`이 "아쉬운 점" 또는 "다음 주 목표"에 반영되었는가?
   - 누락된 항목이 있으면 명시

2. **미팅 반영 여부**
   - 이번 주 모든 미팅이 해당 섹션(3D VLA/Value Function/개인 공부)에 언급되었는가?
   - 미팅이 없었다면 그 사실이 명시되었는가?

3. **Inbox 수 정확성**
   - Agent 3이 보고한 inbox 파일 수와 리뷰의 "이번 주 Inbox 누적 현황" 수가 일치하는가?
   - 리뷰가 inbox 항목을 "방치/지연"으로 표현하거나 "처리 안 됨" 식의 부정적 뉘앙스로 적었는가? ← 그러면 수정. inbox 누적은 정상 워크플로우임

4. **Review-due 노트 완전성**
   - Agent 4가 찾은 overdue + 이번 주 due 노트가 모두 "이번 주 복습 대상"에 [[wikilink]]로 나열되었는가?

5. **frontmatter 정확성**
   - type: weekly-review
   - week: "{YEAR}-W{WEEK}" (정확한 주차)
   - date-range: 정확한 날짜 범위
   - tags에 type/weekly-review 포함

6. **스타일 검증**
   - 구어체 반말 사용 확인 ("~됨", "~함")
   - 격식체(~합니다, ~입니다) 없는지 확인
   - 기술 용어가 영어로 유지되는지 확인
   - 이모지가 사용되지 않았는지 확인

7. **창작 내용 없음**
   - 리뷰에 있는 모든 내용이 원본 데이터에서 확인 가능한가?
   - 데이터에 없는 내용이 들어가 있으면 해당 부분 명시

## 조치

- 문제가 발견되면 Edit 도구로 직접 수정
- 수정 사항이 많으면 Write 도구로 전체 재작성
- 모든 검수 통과하면 "검수 완료 — 문제 없음" 보고
- 수정한 경우 "검수 완료 — N개 항목 수정" + 수정 내역 보고
```

---

## 실행 순서 요약

1. **Bash로 날짜 계산** → 변수 확보
2. **이전 주 리뷰 파일 확인** → 07-reviews/ 에서 직전 주 파일 Read (없으면 skip)
3. **Phase 1**: 6개 Agent **동시 실행** (모두 sonnet)
4. **Phase 2**: 1개 Synthesis Agent 실행 (opus) — 6개 결과 + 이전 주 목표 전달 → 07-reviews/{FILENAME} 작성
5. **Phase 3**: 1개 Validation Agent 실행 (opus) — 작성된 파일 + Phase 1 데이터 대조 → 필요시 수정
6. **완료 보고**: 아래 내용을 사용자에게 출력
   - 생성된 리뷰 파일 경로
   - 주요 내용 요약 (2~3줄)
   - 검수 결과
   - **참고한 전체 파일 목록** — 6개 Agent가 각각 보고한 "참고한 파일" 목록을 합쳐서 중복 제거 후 하나의 리스트로 출력. 사용자가 빠진 파일이 없는지 직접 확인할 수 있게 함.

## Edge Cases

- **오늘의 데일리 노트가 root에 있을 수 있음** — 05-daily/ 뿐 아니라 vault root도 확인
- **inbox 파일에 frontmatter가 없을 수 있음** — 파일명과 본문 첫 줄로 판단
- **review-due가 "2026-XX-XX" 같은 placeholder일 수 있음** — 이런 건 skip
- **미팅이 없는 주** — "이번 주 미팅 없음"으로 처리
- **07-reviews/ 폴더가 없을 수 있음** — 없으면 mkdir로 생성
- **이전 주 리뷰가 없을 수 있음** — "이전 주 리뷰 없음"으로 처리하고 달성도 비교 생략
