---
name: formula-collect
description: 논문(PDF·arXiv URL)이나 vault 노트에서 중요한 수식(모델 구조·loss/objective·update rule·핵심 전개 결과)을 멀티에이전트(Sonnet 추출 → Opus 선정·작성 → 다른 Opus 검수)로 뽑아 formula-quiz 수식 덱(02-areas/formula-deck/)에 카드로 추가. 기존 모집단에 맞으면 거기에, 아니면 새 모집단 파일 생성, 중복 카드는 제외. "이 논문 수식 추가해줘", "수식 덱에 넣어줘", "수식 수집", "formula collect", "/formula-collect" 요청 시.
argument-hint: <논문 PDF 경로 | arXiv URL | 노트 경로> [넣을 모집단]
---

# formula-collect

문서 하나 → 중요한 수식 카드 몇 장 → `formula-quiz` 덱에 추가. 퀴즈는 `/formula-quiz`.

- 덱 폴더: `~/For-Neural-Network-Improvement-Private-/02-areas/formula-deck/` (모집단 1개 = `formula-<slug>.md`)
- **카드 포맷: `~/.claude/skills/formula-quiz/references/deck_format.md`** ← 작성자·검수자 둘 다 반드시 읽힘
- 추출 브리프: `~/.claude/skills/formula-quiz/references/scout_brief.md`

## 원칙

- **중요한 수식만**: 논문의 핵심 (모델 구조식, loss/objective, update rule, 논문이 기여라고 주장하는 핵심 유도 결과). 보통 논문 1편당 **3~6장**. 표준 식 재탕(softmax, Adam 등 이미 덱에 있는 것)은 넣지 말고 기존 카드 참조
- **작성자 ≠ 검수자**: 카드 쓴 에이전트가 자기 검수 금지
- **원본(논문·노트)은 안 건드림**. 덱 파일만 수정
- 기존 카드의 **id는 절대 바꾸지 않음** (퀴즈 기록이 id에 묶임), `기록:` 줄도 손대지 않음

## 파이프라인

### 1. 문서 확보

- arXiv URL → PDF 받기 (`curl -L https://arxiv.org/pdf/<id> -o <scratchpad>/paper.pdf`), 또는 HTML판(`arxiv.org/html/<id>`)이 있으면 수식 텍스트가 깔끔함
- PDF → 텍스트: `pdftotext -layout paper.pdf paper.txt` (수식이 깨지면 해당 페이지를 이미지로 렌더해서 Read로 보기 — `pdftoppm -png -r 110 -f N -l N`)
- vault 노트(03-knowledge / 04-literature) → 그대로 사용. 논문 노트가 04-literature에 있으면 카드 `**참고**`에 wikilink 연결

### 2. 후보 추출 (Sonnet)

- 짧은 논문(본문 ~15p 이하): Sonnet 1명. 길면 섹션(Method / Appendix 유도 등)별로 2~3명 병렬
- 프롬프트: `scout_brief.md` 읽고 따르라고 + 문서 경로·담당 범위. 결과 = JSON 후보 + 클러스터 제안

### 3. 선정 + 모집단 배치 (메인 세션, 판단)

- 기존 덱 훑기: `grep -h '^population:\|^group:' 02-areas/formula-deck/formula-*.md`, 카드 제목 `grep -h '^## ' ...`
- 같은 주제 덱이 있고 합쳐서 **8장 이하**면 그 덱 **끝에** 추가 (파일 순서 = 공부 순서). 아니면 새 덱 `formula-<논문약칭 or 주제>.md`
  - 새 덱 frontmatter: `type: formula-deck`, `tags`, `population`, `group` (기존 6개 중: DL 기초 / 확률·정보이론 / 아키텍처 / Transformer / 생성 모델 / 강화학습 — 안 맞으면 새 이름 써도 됨, 첫 화면 맨 뒤로 감), `order`, `sources`
- 중복 제거: 기존 카드와 같은 식이면 빼고, 해설에서 `[[formula-xxx]]`로 참조만
- 선정안이 애매하면 (특히 기존 덱에 섞을지 새로 만들지) Theo에게 짧게 확인

### 4. 카드 작성 (Opus)

- 작성자 Opus 1명 (카드 많으면 덱별로 나눠 병렬). 프롬프트에 포함할 것:
  - `deck_format.md` 경로 (필독), 대상 덱 파일, 카드 목록(id·제목·문제 수식 초안·논문 위치 Eq/Section), 논문 텍스트 경로
  - 기존 덱에 추가면: **기존 카드는 수정 금지, 파일 끝(마지막 `---` 뒤)에만 append**
  - id prefix는 덱의 기존 prefix 따르거나, 새 덱이면 짧은 약어
  - 참고 = 논문 (저자, 연도, 제목, Eq 번호) + 관련 vault 노트 wikilink(있으면, 실제 heading까지)
- 작성자 자체 검사 (에러 0까지):
  ```bash
  python3 ~/.claude/skills/formula-quiz/server.py check <덱파일>
  node ~/.claude/skills/formula-quiz/check_katex.mjs <덱파일>
  node ~/.claude/skills/formula-quiz/check_annotate.mjs <덱파일>   # 기호 풀이 ↔ 수식 매칭 (화살표 주석). 못 찾은 bullet은 $기호$를 문제 수식 LaTeX와 맞추기
  ```

### 5. 검수 (작성자 아닌 Opus)

- 백지 상태 검수자 1명: 논문 원문 대조로 **수식·부호·첨자·조건 정확성**, 기호 풀이 누락, 힌트가 답을 누설하거나 너무 모호한지, 참고 링크(노트 heading 실존) 확인 → 이슈 리스트만 보고 (직접 수정 X)
- 이슈를 작성자에게 `SendMessage`로 넘겨 반영 → 다시 자체 검사

### 6. 마무리

- README 모집단 표 갱신: `python3 ~/.claude/skills/formula-quiz/server.py readme <덱폴더>` (새 덱이면 frontmatter `order`부터 정하고)
- Theo에게 보고: 어느 덱에 / 어떤 카드(제목) / 새 덱인지, 빠진 후보와 이유 한 줄. `/formula-quiz <slug>` 로 바로 풀 수 있다고 안내
- 커밋은 Theo 루틴(git-pull-push)에 맡김
