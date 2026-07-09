# 문제 생성/검수 원칙 (question-principles)

recall-quiz 파이프라인의 **Generate(Sonnet)·Review(Opus) 에이전트가 런타임에 읽고 준수하는 기준**. 체크리스트형이니 그대로 적용할 것. 각 주장에 출처 URL 달아둠.

---

## 1. 대원칙 — retrieval > recognition (testing effect)

- **인출(retrieval)이 재학습(re-reading)보다 장기기억에 압도적으로 강함.** 즉시 테스트에선 비슷해 보여도 지연 테스트에서 진짜 차이가 남 (1주 후 80% vs 36%). [Roediger & Karpicke 2006](https://journals.sagepub.com/doi/10.1111/j.1467-9280.2006.01693.x)
- 따라서 모든 문제는 **답을 기억에서 꺼내게** 설계할 것. 보기에서 고르는 재인(recognition), yes/no 판별, 노트 구절 그대로 되묻기는 인출이 아님.
- **Desirable difficulties**: spacing·interleaving·retrieval·generation은 초기 수행을 떨어뜨리지만 장기 파지·전이를 올림. 문제가 "쉽게 풀려서 기분 좋은 것"과 "기억에 남는 것"은 다름. [Bjork & Bjork 2011](https://bjorklab.psych.ucla.edu/wp-content/uploads/sites/13/2016/04/EBjork_RBjork_2011.pdf)
- **Retrieval effort hypothesis**: 성공한 인출 중에서도 **더 어렵게 성공한** 인출이 기억에 더 좋음 → 난이도를 "결국 맞히지만 힘든" 지점에 맞출 것. [Pyc & Rawson 2009](https://www.sciencedirect.com/science/article/abs/pii/S0749596X09000138)
- **Transfer-appropriate processing**: 연습할 때의 처리 방식이 나중에 써먹을 방식과 일치할수록 효과 극대 → Theo가 연구(VLA/robotics)에서 쓸 형태로 물을 것. [정리](https://notes.andymatuschak.org/Transfer-appropriate_processing)

---

## 2. 좋은 문제 조건 — Generate 체크리스트

문제 하나 만들 때마다 아래 전부 통과해야 함. [Matuschak "How to write good prompts"](https://andymatuschak.org/prompts/) + [SuperMemo 20 rules](https://www.supermemo.com/en/blog/twenty-rules-of-formulating-knowledge)

- [ ] **Focused** — 문제 하나에 사실/개념 하나. 두 개념 물으면 두 문제로 쪼갬 (Matuschak)
- [ ] **Precise** — 뭘 몇 개 답해야 하는지 명시. "~에 대해 설명하라" 같은 모호한 범위 금지 (Matuschak)
- [ ] **Consistent** — 같은 문제는 항상 같은 답이 나오게. 답이 여럿 가능하면 조건을 좁힘 — 간섭 방지 (Matuschak; [SuperMemo R11](https://www.supermemo.com/en/blog/twenty-rules-of-formulating-knowledge))
- [ ] **Tractable** — ~90% 맞힐 수 있는 난이도. 너무 어려우면 hint 달거나 두 문제로 쪼갬 (Matuschak) ← 단 §4 모드별 조정 있음 (warmup은 예외적으로 어렵게)
- [ ] **Effortful** — 문제 문면에서 답이 추론되면 안 됨. 패턴매칭으로 못 풀게 (Matuschak)
- [ ] **Minimum information** — 최소 단위로 쪼갬. 긴 답 요구 금지 ([SuperMemo R4](https://supermemo.guru/wiki/20_rules_of_knowledge_formulation))
- [ ] **이해 먼저, 암기 그다음** — 노트가 이해 안 된 채 쓴 부분이면 암기형 대신 why형으로 (SuperMemo R1·2)
- [ ] **나열/집합 금지** — "X의 요소 5가지?"는 최악. cloze로 하나씩 (SuperMemo R9·10)
- [ ] **기존 지식 연결 + 개인화** — Theo의 연구 맥락(VLA, robotics, 3D)에 연결하면 파지↑ (SuperMemo R13·14)
- [ ] **Elaborative interrogation** — "왜/어떻게" 질문 유효. 단 학생이 만든 설명이 틀리면 오개념이 굳으므로 answer에 정확한 모범답안 필수 ([PMC6449625](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC6449625/))
- [ ] **Generation effect** — "노트의 예시 말고 다른 예시 하나 더 들어봐" 식으로 스스로 만들게 (Matuschak)
- [ ] **변별 문제** — 유사 개념끼리(예: diffusion vs flow matching) 구분 문제로 간섭 예방 (SuperMemo R11)
- [ ] **Bloom 수준 섞기** — remember만 반복하지 말고 apply/analyze 1~2개 (§6)
- [ ] **가변 지식엔 출처·날짜** — "2026 기준" 같은 조건 명시 (SuperMemo R18·19)
- [ ] **answer는 노트 원문 근거로만** — LLM 자동생성 문제의 ~2/3가 품질 미달이라는 보고가 있음. 그래서 Review 게이트(§8)가 필수 ([arXiv:2507.05629](https://arxiv.org/abs/2507.05629))

---

## 3. 문제 유형 카탈로그

`type` 필드 6종. 언제 쓰는지 + 좋은/나쁜 예.

| type | 언제 | 좋은 예 | 나쁜 예 |
|---|---|---|---|
| `recall` | 정의·핵심 사실 인출 | "Flow Matching의 학습 목표(vector field가 근사하는 대상)는?" | "Flow Matching이란?" ← 범위 모호 |
| `cloze` | 수식·문장 속 핵심 항 하나 | "CFM loss에서 target은 ___ (조건부 확률경로의 ___)" | 빈칸 4개짜리 ← 나열, focused 위반 |
| `application` | 배운 걸 새 상황에 적용 | "action chunk가 8일 때 이 방식이면 inference 몇 번?" | 노트 예시 숫자 그대로 재사용 ← 패턴매칭 |
| `why` | 설계 이유·인과 | "왜 ODE 기반이 SDE 기반보다 sampling step이 적어도 되나?" | "왜 중요한가?" ← precise 위반 |
| `compare` | 인접 개념 변별(간섭 예방) | "diffusion과 flow matching의 학습 target 차이 한 가지?" | "diffusion과 FM을 비교하라" ← 범위 과대 |
| `misconception` | 흔한 오해 교정 | "'FM은 noise에서 시작 못 한다'는 맞는 말인가? 근거는?" | 단순 yes/no로 끝나는 형태 ← 근거 요구 없으면 이진 문제 |

- yes/no로 끝나면 안 됨 — misconception도 반드시 "근거는?"까지 물어서 인출 유발.
- 각 유형의 answer는 **노트 발췌 안에서 검증 가능**해야 함 (hallucination 금지, §8).

---

## 4. 모드별 전략 (핵심)

### `warmup` — 사전점화. **틀리는 게 목표**

- **Pretesting effect**: 아직 안 배운 내용에 틀리게 답해도(오답률 75%+여도) 이후 학습·기억이 향상됨. [Pan & Carpenter 2023 리뷰](https://664ef278-1723-43c6-bbe1-3bd4e65a87fe.filesusr.com/ugd/f4b9f1_c96d7d9b4efa44c482f52d92970347eb.pdf), [Richland et al. 2009](https://learninglab.uchicago.edu/Pre-Testing_files/RichlandKornellKao.pdf)
- 지시:
  - [ ] 난이도 4~5, 대부분 오답 나와도 OK — low-stakes임을 문제에 암시하지 말고 그냥 어렵게
  - [ ] **recall/cloze만.** 추론(application/why) 문제 지양 — pretest에서 추론형은 효과 약함 [Hausman & Rhodes 2018](https://pubmed.ncbi.nlm.nih.gov/29781391/)
  - [ ] 좁고 구체적으로 (이후 학습에서 "아 이거였구나" 연결점이 되도록)
  - [ ] `reveal_level: minimal` — 추측하게 만들고 답은 지연 공개 (기억 노력이 핵심)
  - [ ] 개수 4~6 (학습시간의 ~20% 수준)

### `check` — 인코딩 검증 + 오개념 즉시 교정. **VSAQ + 즉시 피드백**

- **VSAQ(아주 짧은 서술형)가 MCQ보다 오개념 진단에 우수** — MCQ는 선택지 자체가 힌트로 작용해 재인으로 풀림. [BMC Med Educ 2024](https://pmc.ncbi.nlm.nih.gov/articles/PMC11684041/)
- **Hypercorrection**: 확신하고 틀린 오답일수록 즉시 피드백으로 잘 교정됨 → misconception 유형을 반드시 포함하고 답을 즉시 완전 공개. [Butterfield & Metcalfe](https://link.springer.com/article/10.3758/s13423-011-0173-y)
- 지시:
  - [ ] 난이도 2~3, 성공률 높되 **재인이 아닌 인출**로
  - [ ] VSAQ형 — 한두 문장/한 항으로 답하는 짧은 서술. 보기 주지 말 것
  - [ ] 세션 전체 breadth 커버 + 취약해 보이는 섹션(노트가 흐리게 쓰인 곳)은 depth 추가
  - [ ] misconception 1~2개 필수 (그 개념에서 흔히 틀리는 지점을 노트 근거로)
  - [ ] `reveal_level: full` — 즉시 완전 공개가 최우선 (hypercorrection)
  - [ ] 개념별 1~2문제, 총 6~10

### `recall` — 지연 복습. **interleave + 전이**

- 지연 인출이 장기 파지의 본체 (§1 testing effect). 간격은 목표 파지기간의 10~30%가 최적. [Cepeda et al. 2008](https://laplab.ucsd.edu/articles/Cepeda%20et%20al%202008_psychsci.pdf)
- 장기적으로 **동일 간격 ≥ 확장 간격** — 간격 벌리기에 집착 말 것. [Karpicke & Roediger 2007](https://www.researchgate.net/publication/6261284)
- **Interleaving**: 주제를 섞어 출제하면 변별 학습이 일어나 파지·전이↑. [리뷰](https://link.springer.com/article/10.1007/s10648-021-09613-w)
- 지시:
  - [ ] 난이도 3~4 — desirable difficulty, 힘들지만 결국 성공하는 지점 (§1 retrieval effort)
  - [ ] recall 중심 + **application/compare 2~3개 섞기** (전이 유도)
  - [ ] 노트 전체를 넓게 + 섹션 순서대로 묶지 말고 **주제 interleave** (Curate 단계에서 순서 섞기)
  - [ ] 아주 오래된 노트(수개월+)면 첫 1~2문제는 힌트 있는 가벼운 재인으로 스키마 재활성화 후 recall 전환. [Successive Relearning](https://journals.sagepub.com/doi/full/10.1177/09637214221100484)
  - [ ] `reveal_level: partial`~`full` 유연
  - [ ] 개수 8~12

---

## 5. reveal_level 원칙

| 값 | 동작 의도 | 기본 모드 |
|---|---|---|
| `minimal` | 답 최소 공개 (핵심 키워드만) — 스스로 더듬게 | warmup |
| `partial` | 요지 공개 + 세부는 힌트/채팅으로 | recall (일부) |
| `full` | 모범답안 완전 공개 + 오개념 교정 설명 포함 | check, recall |

- 원칙: **인출 노력이 목적이면 아끼고, 교정이 목적이면 다 보여줌.** warmup은 기억 노력 자체가 효과의 원천이라 minimal ([Pan & Carpenter 2023](https://664ef278-1723-43c6-bbe1-3bd4e65a87fe.filesusr.com/ugd/f4b9f1_c96d7d9b4efa44c482f52d92970347eb.pdf)), check는 hypercorrection 때문에 full 즉시 ([Butterfield & Metcalfe](https://link.springer.com/article/10.3758/s13423-011-0173-y)).
- `full`의 answer엔 "왜 그런지" 한 줄 근거까지 포함 (교정 효과↑).

---

## 6. Bloom 수준별 문제표

[Bloom's Taxonomy 가이드](https://www.publichealth.pitt.edu/sites/default/files/assets/academic%20forms/EPCC/Bloom's%20Taxonomy%20Guide.pdf)

| 수준 | 목적 | 동사 | 템플릿 |
|---|---|---|---|
| Remember | 사실 인출 | define, list, name, recall | "OOO의 정의는?" / cloze로 하나씩 |
| Understand | 재진술·예시 | explain, summarize, classify | "자기 말로 설명하면?" / "예시 하나?" |
| Apply | 새 상황 적용 | apply, calculate, solve, use | "이 조건에서 어떻게 적용?" |
| Analyze | 분해·관계 | differentiate, compare, contrast | "OOO와 XXX의 차이?" / "원인 분해하면?" |
| Evaluate | 판단·비판 | assess, critique, justify | "OOO가 XXX보다 나은 근거는?" |
| Create | 종합·생성 | design, propose, hypothesize | "이 원리로 새 사례/가설 하나?" |

- remember만 쌓지 말 것 — 세트당 apply/analyze **1~2개** 섞기.
- evaluate/create는 노트 frontmatter `review-count` 2~3 이상(충분히 소화된 노트)일 때만.

---

## 7. 개념 5각도 프레임

한 개념을 여러 각도에서 각각 **별도 문제**로 (한 문제에 다 넣으면 focused 위반).

| 각도 | 질문 프레임 |
|---|---|
| 속성 | 어떤 조건/경향에서 성립하나? |
| 유사·차이 | 인접 개념과 뭐가 같고 다른가? (변별) |
| 부분·전체 | 구성 요소는? 상위 구조는? |
| 원인·결과 | 왜 이렇게 설계/발생했나? 결과는? (elaborative interrogation) |
| 의미·함축 | 왜 중요한가? (Theo 연구/실무 함의) |

출처: [Matuschak](https://andymatuschak.org/prompts/), [SuperMemo 20 rules](https://www.supermemo.com/en/blog/twenty-rules-of-formulating-knowledge)

---

## 8. Red flag 체크리스트 — Reviewer(Opus) 게이트

문제마다 아래를 전수 체크. **하나라도 걸리면 탈락 또는 수정 지시.** 근거: LLM 자동생성 문제의 12가지 결함 분류 [arXiv:2507.05629](https://arxiv.org/html/2507.05629v1) + Matuschak/SuperMemo.

- [ ] **패턴매칭** — 질문 모양만 외우면 풀리는 문제 (답이 문면에서 추론됨)
- [ ] **Yes/No 이진** — 근거 요구 없이 참/거짓으로 끝남
- [ ] **정답 누출** — stem이나 hint에 답 단서가 들어있음
- [ ] **문맥 부족** — 조건이 모호해 정답이 여러 개 가능 (consistent 위반)
- [ ] **범위 과대** — "~에 대해 설명하라"류, 답 경계 불명
- [ ] **나열 방치** — "N가지 전부 나열" 요구 (cloze로 쪼개야 함)
- [ ] **유사항목 간섭 방치** — 헷갈리는 인접 개념이 노트에 있는데 변별 장치 없음
- [ ] **이해 없이 암기** — 노트가 설명 없이 결론만 적은 부분을 기계적 암기로 냄
- [ ] **hallucination** — question/answer에 **노트 발췌에 없는 사실** 포함 ← 최우선 탈락 사유. answer의 모든 주장을 note_anchor 범위 원문과 대조할 것
- [ ] **노트와 사실 불일치** — 노트 내용을 잘못 요약/왜곡
- [ ] **이중 개념** — 한 문제에 개념 두 개 (focused 위반)

추가 게이트 (파이프라인 계약):

- [ ] **tractable ~90%** — 모드 기준 난이도 대비 과하게 어려운 문제 없는지 (warmup 제외 — warmup은 §4 기준 적용)
- [ ] **note_anchor 정확성** — `heading` 원문 일치 / `heading_line`·`line_start`·`line_end`가 실제 노트 라인과 일치 / `anchor_slug` 정규화 규칙 준수. 앵커 틀리면 마킹이 엉뚱한 heading에 붙으므로 **반드시 실제 노트 열어 대조**
- [ ] **모드 계약** — 유형·개수·reveal_level이 §4 모드 표와 일치
- [ ] **스키마** — questions.json 필드 누락 없음, `review_meta.tractable`/`flags` 기록

> Human-in-the-loop 원칙: 이 게이트가 있어도 최종 소비자는 Theo. 확신 없는 문제는 `review_meta.flags`에 사유를 남겨 통과시키지 말고 제거할 것. [arXiv:2507.05629](https://arxiv.org/abs/2507.05629)

---

## 9. 773 spaced-repetition 연동

이 vault는 773 복습법(당일 7시간 / 7일 / 30일) 사용 — 프로젝트 CLAUDE.md 참고.

| 773 단계 | 대응 모드 | 비고 |
|---|---|---|
| 1차 (당일) | `check` | 정리 직후 인코딩 검증 + 오개념 즉시 교정 |
| 2차 (7일, 주간 리뷰) | `recall` | 주간 inbox 정리 때 그 주 노트 대상 |
| 3차 (30일, review-due) | `recall` | 통과하면 `review-count` 3 = 기본 졸업 |
| (신규 학습 직전) | `warmup` | 773 밖의 보너스 — pretest로 인코딩 준비 |

- 간격 근거: 최적 간격 ≈ 목표 파지기간의 10~30% ([Cepeda et al. 2008](https://laplab.ucsd.edu/articles/Cepeda%20et%20al%202008_psychsci.pdf)) — 30일 복습은 수개월~1년 파지 목표와 정합.
- mode 스마트 제안이 frontmatter `review-due`를 읽는 이유가 이것: due 도래 = 773 3차 = `recall`.
- 퀴즈 결과의 `:repeat:` 마킹은 773 다음 회차에서 그 heading을 우선 복습하라는 신호. **단 frontmatter(`review-due`/`review-count`) 갱신은 이 스킬이 자동으로 하지 않음** — 복습 완료 처리는 Theo가 주간 리뷰 흐름에서 결정.
