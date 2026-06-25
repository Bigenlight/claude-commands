---
name: vocab-collect
description: 특정 문서(논문 PDF·md·txt·웹 URL)에서 Theo가 모를 법하면서 학술적으로 중요한 영어 단어를 멀티에이전트(Sonnet 추출 → Opus 선정)로 뽑아, 기존 단어장과 비교해 새 단어만 양식에 맞게 추가하는 스킬. vocab-quiz와 같은 단어장을 공유. "이 문서에서 단어 뽑아줘", "단어 수집", "vocab collect", "이 논문 단어 정리해줘" 요청 시 트리거.
allowed-tools: [Bash, Read, Write, Edit, Glob, Grep, WebFetch, Workflow, Skill]
argument-hint: <문서 경로 또는 URL> [개수] [대상 단어장 경로]
---

# vocab-collect

문서 하나를 입력받아 **멀티에이전트로 영어 단어를 추출·선정**하고, **기존 단어장과 비교해 중복을 뺀 새 단어만** 양식에 맞춰 추가한다. `vocab-quiz` 스킬과 **같은 단어장 파일을 공유**하므로, 여기서 추가하면 바로 다음 퀴즈에 출제됨.

> **페르소나: AI 로보틱스 대학원생 동료.** Theo는 고급 영어 독해가 되는 석박과정생. 너무 쉬운 일상어/이미 아는 수준 단어는 거르고, "한 단계 위 학술 어휘 + 논문에서 의미상 중요한 단어 + 도메인 특유 뉘앙스"를 노린다.

---

## 전제

- **대상 단어장**: 기본 `~/For-Neural-Network-Improvement-Private-/02-areas/english-vocab-and-concepts.md` (인자로 다른 경로 지정 가능)
- **단어장 포맷**: vocab-quiz와 동일한 `<details>/<summary>` 블록. 두 섹션 헤더 `## 주목할만한 개념` / `## 영어 단어 및 표현` 사용.
- **단어 블록 양식** (정확히 지킬 것):

    ```markdown
    <details>
    <summary>문서 속 등장 문장 (target 단어를 <b>볼드</b>로 감쌈)</summary>

    뜻 (한국어, 간결)

    쉬운 일상 예문 한두 개 ← 화살표로 한국어 부연

    (필요시) 문서 맥락에서의 뉘앙스 한 줄

    </details>
    ```

  - **블록 아래 아이콘 줄(✅🔁❌💯)은 절대 넣지 말 것** ← 아직 안 풀었으니까. 아이콘은 vocab-quiz 서버가 푼 뒤 누적함.
  - 말투: **구어체 반말**, 기술 용어는 영어 유지, 부연은 `← ` 화살표. (프로젝트 CLAUDE.md 노트 스타일 준수)

---

## 입력

1. **문서** (필수): 로컬 파일 경로(PDF/md/txt) **또는** 웹 URL(arxiv/일반 페이지).
   - 안 주면 사용자에게 물어본다.
2. **개수** (선택): 안 주면 **문서 길이에 비례**해 자동 결정(아래 표).
3. **대상 단어장 경로** (선택): 안 주면 위 기본 경로.

---

## 동작 (파이프라인)

### 1. 문서 확보 & 길이 측정 → 개수 결정

- **URL이면**: arxiv abs/pdf URL은 PDF로 다운로드(`curl -L`), 일반 페이지는 `WebFetch`로 본문 확보. 임시 경로(`/tmp/`)에 저장.
- **PDF면**: 페이지 수 측정 — `pdfinfo X.pdf | grep -i pages` 또는 `python3 -c "import pypdf; print(len(pypdf.PdfReader('X.pdf').pages))"`.
- **텍스트(md/txt/웹)면**: 단어 수 측정 — `wc -w`.
- **개수 스케일** (사용자가 명시 안 했을 때):

  | 문서 길이 | 추출 개수 |
  |-----------|-----------|
  | 짧음 (PDF < 8p / 텍스트 < 1,500 단어) | ~5개 |
  | 보통 (PDF 8–25p / 텍스트 1,500–6,000) | ~10개 |
  | 긺 (PDF > 25p / 텍스트 > 6,000) | ~12–15개 |

### 2. 기존 단어장에서 중복 제외 목록 추출

```bash
grep -oP '(?<=<summary>).*?(?=</summary>)' "<단어장경로>" | sed 's/<[^>]*>//g'
```
- 나온 summary들에서 핵심 단어(lemma)를 뽑아 **제외 목록(EXCLUDE)** 으로 구성. 이미 아는 단어는 다시 추천 안 함.

### 3. 읽기 분담 결정

- **PDF**: 리더 수 = `min(5, ceil(페이지/10))`. 페이지를 균등 분할(에이전트당 ≤ 20p — Read `pages` 한계). 예: 48p → 5명 × 약 10p.
- **텍스트/웹**: 본문을 균등 청크로 분할(리더 수 = min(5, ceil(단어/2000))). 각 청크 텍스트를 에이전트 프롬프트에 직접 넣거나, 파일 offset 범위를 지정.

### 4. Workflow 실행 (Sonnet 추출 → Opus 선정 → Opus 종합·자동분류)

아래 템플릿을 문서 타입/분할에 맞게 채워 `Workflow`로 실행한다. (스킬이 Workflow 호출을 지시하므로 opt-in 충족)

- **Read 단계 (Sonnet ×N)**: 각자 맡은 범위에서 후보 단어 + 논문 속 등장 문장(그대로 인용) + 페이지 + 뜻 + 난이도 + 중요 이유를 뽑음. EXCLUDE/너무 쉬운 단어 제외.
- **Select 단계 (Opus ×3)**: 서로 다른 관점(① 문서 이해 핵심 ② 범용 학술 어휘 ③ 난이도 높고 가치 큰)으로 각자 후보 풀에서 선정안 작성.
- **Synthesize 단계 (Opus ×1)**: 세 안 종합 → 최종 N개 확정 → **각 단어를 `vocab` / `concept`으로 자동 분류** → 정확한 양식의 markdown 블록 작성.

### 5. 자동 섹션 분류 후 append

- 종합 에이전트가 각 항목에 단 `section` 값으로 라우팅:
  - `vocab` → `## 영어 단어 및 표현` 섹션 끝에 추가
  - `concept` → `## 주목할만한 개념` 섹션 끝에 추가
- **분류 기준**: 영영사전에 단독 표제어로 실릴 법한 일반 영어 단어/표현 = `vocab`. ML/이론 고유 개념·기법(예: teacher forcing, rollout, ablation) = `concept`.
- append는 HTML 엔티티(`&lt;`,`&gt;`) 디코딩해서 실제 `<`,`>`로 넣을 것. 임시 파일에 쓴 뒤 해당 섹션 끝(다음 `## ` 또는 EOF 직전)에 삽입.
- 추가 직후 `grep -c "<summary>"`로 개수 증가 검증.

### 6. 보고

- 추가한 단어를 표로 요약(단어 / 뜻 / 왜 골랐나 / 섹션).
- "git 추적 파일이니 원하면 커밋" + "vocab-quiz로 바로 출제 가능" 안내.

---

## Workflow 스크립트 템플릿

> 아래를 문서 타입/페이지 분할/개수/EXCLUDE/대상경로에 맞게 채워 사용. 자동 분류를 위해 최종 스키마에 `section` 필드 포함.

```javascript
export const meta = {
  name: 'vocab-collect-run',
  description: '문서에서 Theo 수준 영어 단어 N개 추출·선정·자동분류',
  phases: [
    { title: 'Read', detail: 'Sonnet N명이 범위 분담해 후보 추출' },
    { title: 'Select', detail: 'Opus 3명이 관점별 선정안' },
    { title: 'Synthesize', detail: 'Opus 1명이 최종 N개 종합 + vocab/concept 분류' },
  ],
}

const DOC = '<문서 경로>'                 // PDF 경로 또는 텍스트
const N = <개수>                           // 길이 비례로 결정한 값
const EXCLUDE = `<기존 단어장에서 뽑은 제외 목록>`
const THEO_LEVEL = `Theo는 한양대 AI학과 석박통합과정생(고급 영어 독해 가능). 이미 아는 수준 예: spurious, intrinsic, perpendicular, extrapolate, implicit, heterogeneity, empirical, albeit, eschew, heuristic, infeasible. 기본~중급 학술 어휘는 이미 앎. 선정 대상 = (1) 학술/논문 빈출이며 의미있는 단어이면서 (2) Theo가 모를 법한 한 단계 위 어휘 또는 ML/논문 특유 뉘앙스 단어. 너무 쉬운 일상어/이미 아는 단어 제외.`

const CAND_SCHEMA = { type:'object', properties:{ candidates:{ type:'array', items:{ type:'object',
  properties:{ word:{type:'string'}, sentence:{type:'string'}, page:{type:'integer'},
    meaning:{type:'string'}, difficulty:{type:'string',enum:['easy','medium','hard']}, importance:{type:'string'} },
  required:['word','sentence','meaning','difficulty','importance'] } } }, required:['candidates'] }

phase('Read')
const ranges = [/* PDF면 [[1,10],[11,20],...]; 텍스트면 청크 인덱스 */]
const reads = await parallel(ranges.map((r,i) => () =>
  agent(`PDF "${DOC}"의 ${r[0]}~${r[1]} 페이지를 Read 툴(pages:"${r[0]}-${r[1]}")로 읽고, 학술 빈출/문서상 중요/석박과정생이 모를 법한 영어 단어 후보를 뽑아라. 각 후보: 단어(lemma), 문서 속 실제 문장(그대로 인용), 페이지, 한국어 뜻, 난이도, 중요 이유. 이미 아는 제외: ${EXCLUDE}. ${THEO_LEVEL} 범위당 10~20개.`,
    { label:`read:${i+1}`, phase:'Read', model:'sonnet', schema:CAND_SCHEMA })))
const pool = reads.filter(Boolean).flatMap(r => r.candidates)
log(`후보 ${pool.length}개`)

const SELECT_SCHEMA = { type:'object', properties:{ selection:{ type:'array', items:{ type:'object',
  properties:{ word:{type:'string'}, sentence:{type:'string'}, meaning:{type:'string'}, reason:{type:'string'} },
  required:['word','sentence','meaning','reason'] } } }, required:['selection'] }

phase('Select')
const poolStr = JSON.stringify(pool, null, 1)
const angles = ['문서 이해에 핵심적인 단어 위주로','범용 학술 어휘(타 논문에서도 빈출) 위주로','난이도 medium~hard이며 가치 높은 단어 위주로']
const picks = await parallel(angles.map((a,i) => () =>
  agent(`후보 풀:\n${poolStr}\n\n${THEO_LEVEL}\n제외: ${EXCLUDE}\n\n${a} ${N}~${N+2}개 선정. 각 단어에 문서 속 문장/뜻/선정이유.`,
    { label:`select:${i+1}`, phase:'Select', model:'opus', schema:SELECT_SCHEMA })))
const proposals = picks.filter(Boolean)

phase('Synthesize')
const FINAL_SCHEMA = { type:'object', properties:{
  items:{ type:'array', items:{ type:'object', properties:{
    section:{type:'string',enum:['vocab','concept']}, word:{type:'string'},
    block:{type:'string',description:'완성된 <details> markdown 블록 1개 (아이콘 줄 없음)'},
    meaning:{type:'string'}, why:{type:'string'} },
    required:['section','word','block','meaning','why'] } } }, required:['items'] }

const final = await agent(
  `Opus 3명 선정안:\n${JSON.stringify(proposals,null,1)}\n\n${THEO_LEVEL}\n제외: ${EXCLUDE}\n\n`+
  `종합해 정확히 ${N}개 확정(겹치게 추천된 단어 우선 + 품사/난이도 다양성). 각 단어를:\n`+
  `- section: 일반 영어 단어/표현이면 "vocab", ML/이론 고유 개념·기법이면 "concept"\n`+
  `- block: 아래 양식의 <details> 블록 1개. summary=문서 속 등장 문장(target을 <b>볼드</b>), 본문=뜻+일상 짧은 예문(← 부연)+필요시 맥락 뉘앙스. 아이콘 줄 절대 금지. 구어체 반말.\n`+
  `으로 만들어라.`,
  { label:'synthesize', phase:'Synthesize', model:'opus', schema:FINAL_SCHEMA })

return final
```

---

## 주의

- **대상 단어장은 직접 수정**(섹션 끝에 블록 append)됨. git 추적 파일이면 추가 후 커밋 권장.
- vocab-quiz가 **블록 순서로 id를 매기므로**, 새 블록은 항상 **섹션 끝에 추가**(중간 삽입 금지)해야 기존 마킹/feedback id가 안 깨짐.
- 중복 방지: 반드시 step 2의 EXCLUDE를 거쳐 같은 단어 재추가 방지. 표기만 다른 동의 표현도 종합 단계에서 거를 것.
- 분류 애매한 단어(개념 vs 일반어 경계)는 종합 에이전트 판단에 맡기되, 결과 보고 시 어느 섹션에 넣었는지 표로 명시.
- 문서를 못 읽거나(스캔 PDF 등 텍스트 추출 실패) 후보가 빈약하면, 무리해서 채우지 말고 사용자에게 알릴 것.
