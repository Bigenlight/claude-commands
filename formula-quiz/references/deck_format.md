# 수식 덱 포맷 스펙 (formula-deck v1)

퀴즈 서버(`~/.claude/skills/formula-quiz/server.py`)가 이 포맷을 **기계적으로 파싱**함. 마커 철자 하나라도 틀리면 카드가 안 잡히니까 그대로 지킬 것.

## 위치

- 폴더: `02-areas/formula-deck/`
- **모집단 1개 = 파일 1개**: `formula-<slug>.md` (slug는 영어 kebab-case. 예: `formula-transformer-attention.md`)
- 모집단 = 같이 보면 좋은 수식 묶음. **카드 3~8개** 권장 (한 세션 최대 5개 출제, 그 이상이면 다음 세션에서 나머지)

## 파일 구조

````markdown
---
type: formula-deck
tags: [study, math, formula, transformer]
population: Transformer — Attention & Positional Encoding
group: Transformer
sources: [study-transformer, study-attention-optimizations]
---

# 수식 덱 — Transformer: Attention & Positional Encoding

> 이 모집단에 뭐가 있는지 1~2줄. 원본 노트: [[study-transformer]] · [[study-attention-optimizations]]
> 퀴즈: `/formula-quiz` → 이 모집단 선택 (한 번에 최대 5개)

---

<!-- formula: tf-sdpa -->
## Scaled Dot-Product Attention

> 💡 **힌트**: Query랑 Key가 얼마나 닮았는지로 Value를 섞는 연산

```math
\mathrm{Attention}(Q, K, V) = \mathrm{softmax}\left(\frac{QK^\top}{\sqrt{d_k}}\right) V
```

<details>
<summary>정답 · 해설</summary>

**정체**: Transformer의 핵심 연산인 scaled dot-product attention ← *Attention Is All You Need* Eq.(1)

**의미**: (2~6줄) 이 수식이 뭘 하는지, 왜 이런 모양인지 직관. 비유 OK

**기호 풀이**

- $Q \in \mathbb{R}^{n \times d_k}$ ← query 행렬. 토큰 n개 각각이 "나는 이런 정보를 찾는다"
- $K \in \mathbb{R}^{m \times d_k}$ ← key 행렬. 토큰 m개 각각의 "나는 이런 정보를 갖고 있다" 라벨
- $QK^\top \in \mathbb{R}^{n \times m}$ ← 모든 (query, key) 쌍의 내적 = 유사도 점수표
- $\sqrt{d_k}$ ← 차원이 크면 내적 분산이 $d_k$배로 커짐 → softmax 포화 방지용 나눗셈
- $\mathrm{softmax}(\cdot)$ ← **행(row)마다** 적용. 각 query별로 key들에 대한 가중치 합 = 1
- $V \in \mathbb{R}^{m \times d_v}$ ← value 행렬. 실제로 섞이는 내용물

**전개** ← 중요한 수식만. 없으면 이 섹션 통째로 생략

```math
\begin{aligned}
q \cdot k &= \sum_{i=1}^{d_k} q_i k_i \\
\mathrm{Var}(q \cdot k) &= d_k \quad (q_i, k_i \sim \text{iid},\ \text{mean } 0,\ \text{var } 1)
\end{aligned}
```

→ 그래서 $\sqrt{d_k}$로 나누면 분산이 1로 돌아옴

**헷갈리는 점** ← 선택. 흔한 오해, 비슷한 식과 차이

**참고**: [[study-transformer]] (## Attention 섹션) · Vaswani et al., 2017, *Attention Is All You Need*, Eq.(1)

</details>

---
````

## 파서 규칙 (꼭 지킬 것)

| 요소 | 규칙 |
|---|---|
| 카드 시작 | `<!-- formula: <id> -->` 한 줄. id는 영어 kebab-case, **파일 안에서 unique**, 앞에 모집단 약어 prefix (예: `tf-sdpa`, `fm-cfm-loss`). **한 번 정하면 절대 안 바꿈** (기록이 id에 묶임) |
| 제목 | 마커 바로 다음 줄 `## <수식 이름>` — 퀴즈에선 **정답 공개 때만** 보임 |
| 힌트 | `> 💡 **힌트**: ...` 한 줄. 퀴즈 문제 화면에 수식이랑 같이 보임 |
| 문제(수식) | 힌트 줄 ~ `<details>` 사이 전부. 보통 ```` ```math ```` 펜스 1개. 짝 수식이면 펜스 2개 or `aligned` OK |
| 정답 | `<details>` + `<summary>정답 · 해설</summary>` + 빈 줄 + 본문 + 빈 줄 + `</details>`. **`<details>` 중첩 금지** |
| 기록 줄 | `</details>` 바로 다음 줄 `기록: ✅2026-10-22 ❌2026-10-25` ← **서버가 자동으로 붙임. 작성할 땐 쓰지 마** |
| 카드 구분 | 카드 끝에 `---` |

## 문제 화면 원칙 (힌트 + 수식)

- 문제 화면엔 **힌트 + 수식만** 보임. 제목은 안 보임 → 힌트에 수식 이름을 그대로 쓰면 답을 다 알려주는 꼴. **"무슨 역할/어떤 상황의 식인지" 방향만** 살짝
  - 좋음: "Query랑 Key가 얼마나 닮았는지로 Value를 섞는 연산"
  - 나쁨: "Transformer의 scaled dot-product attention 식" (정답 그대로)
  - 나쁨: "어떤 연산" (너무 모호)
- 수식은 **해당 분야 표준 표기**로. 문제 수식 안에 `\text{Attention}`처럼 이름이 박혀 있는 건 OK (원래 식이 그렇게 생겼으면)
- 문제 수식에 주석·설명 달지 마 (그건 정답 쪽)

## 정답(해설) 섹션 순서

1. **정체** — 한 줄. 정식 이름 + 출처 짧게
2. **의미** — 왜 이런 모양인지 직관 2~6줄. Theo 노트에 비유가 있으면 그 비유 살려
3. **기호 풀이** — 문제 수식의 **모든 기호**를 bullet로. 형식: `- $기호$ ← 의미 (차원/범위/역할)`. 아주 세세하게. 첨자·연산자(softmax, $\mathbb{E}$, $\nabla$, $\log$ 밑 등)도 빠뜨리지 말 것
    - **이 bullet이 퀴즈의 화살표 주석 데이터임** (`annotate.js`가 파싱) ← 정답 공개 시 문제 수식 속 해당 기호를 색칠하고 위·아래 라벨 + 화살표로 가리킴, 호버하면 bullet 전문
    - `$기호$`는 **문제 수식에 쓴 LaTeX 그대로** (공백·`{}` 차이는 OK, `\left(` vs `(`는 다름). 정의식(`$t = g(x)$`)은 좌변, 콤마 나열(`$x_1, x_2$`)은 각각 매칭. 함수 인자 자리는 `\cdot`(`$h(\cdot)$`, `$\lVert \cdot \rVert^2$`)
    - `←` 뒤 **첫 구절(마침표 전)이 화살표 라벨**로 쓰임 → 첫 구절은 짧게(~15자) 그 자체로 뜻이 서게: `- $\eta$ ← 학습률(learning rate). 예: 0.01` (O) / `- $\eta$ ← 이건 보통 0.01 정도로 두는 …` (X)
    - 텍스트로 시작하는 bullet(`- 중괄호(cases) ← …`)은 화살표 없이 목록에만 나옴
    - 카드 추가·수정 후 `node check_annotate.mjs <덱>` → 수식에서 못 찾은 bullet 목록 확인
4. **전개** — 중요한 수식만. 어디서 이 식이 나오는지 or 이 식에서 뭐가 유도되는지. 단계별로 ```` ```math ```` + `aligned` + 한 줄 설명 조합. Theo 노트에 전개가 있으면 그 흐름을 따라감
5. **헷갈리는 점** — 선택
6. **참고** — 모를 때 볼 곳. **Theo 노트 wikilink + 섹션 heading** (예: `[[study-transformer]] (## Attention)`) + 원 논문/책 (저자, 연도, 제목, Eq 번호 확실할 때만)

## LaTeX 규칙 (GitHub + VS Code MPE + 퀴즈 KaTeX 셋 다 호환)

- 여러 줄 / `\\` 들어가면 → ```` ```math ```` 펜스 + `\begin{aligned}...\end{aligned}` (`align*`, `eqnarray` 금지)
- 인라인 → `$...$`. **인라인 안에 `\\` 절대 금지**
- `$$...$$` 멀티라인 블록 금지 (한 줄짜리도 되도록 펜스로 통일)
- 부등호 `<` `>` 대신 `\lt` `\gt` `\leq` `\geq`
- custom macro 금지. argmax → `\operatorname*{arg\,max}`, 기댓값 → `\mathbb{E}`, 노름 → `\lVert x \rVert`, softmax → `\mathrm{softmax}`
- 한글-수식 경계 띄어쓰기: `값은 $x$ 임` ← 숫자만 있을 때 특히
- 수식 밖 literal `$`는 `\$`
- list item 안에 펜스 넣을 땐 앞에 빈 줄 + 4칸 들여쓰기
- 이미지는 쓰지 말 것 (퀴즈에서 경로 안 맞음)

## frontmatter 필드

| 필드 | 값 |
|---|---|
| `type` | `formula-deck` 고정 |
| `tags` | `[study, math, formula, <주제>]` |
| `population` | 모집단 이름 (퀴즈 첫 화면에 표시) |
| `group` | 첫 화면 묶음. 아래 중 하나: `DL 기초` / `아키텍처` / `Transformer` / `확률·정보이론` / `생성 모델` / `강화학습` |
| `sources` | 원본 노트 파일명 리스트 (확장자 없이) |

> 기호 연습(그리스 문자)은 덱 폴더 밖 `02-areas/greek-symbols-dl.md`(vocab-quiz 형식)를 퀴즈가 직접 읽는 거라 이 포맷 대상 아님. 대신 수식 카드의 기호 풀이 bullet이 이 덱의 "쓰인 곳" 데이터가 되니까, 그리스 문자는 bullet 머리에 `$\theta$`처럼 LaTeX 명령으로 쓸 것 (유니코드 θ 말고)

## 내용 원칙

- **원본 노트(03-knowledge/ 등)는 절대 수정하지 마.** 덱 파일만 씀
- 카드 순서 = **공부 순서** (앞 카드가 뒤 카드의 재료가 되게). 퀴즈도 파일 순서대로 출제됨
- Theo 노트에 **오류/표기 혼용**이 있으면 카드엔 **올바른 식**을 쓰고, `**헷갈리는 점**`에 "노트 Lxxx엔 ~로 적혀있는데 실제론 ~" 처럼 짚어줘 ← 노트 고칠 때 참고용
- 노트에 식이 없고 개념만 있던 것(implied)은 표준 식을 쓰되, `**참고**`에 원 논문을 확실히 달아
- 원 논문 식 번호·표기가 기억이 애매하면 WebSearch/WebFetch로 확인해도 됨. 확인 못 하면 Eq 번호는 빼
- 같은 기호를 덱 안에서 일관되게 (예: RL은 $s, a, s', a', \gamma, \pi, Q^\pi, V^\pi$)

## 말투

- Theo 노트 스타일: **구어체 반말, 명사형 종결(~함, ~됨, ~것임)**, 기술 용어는 영어 그대로
- `← ` 화살표로 부연/이유, `**굵게**`로 핵심, `>` blockquote로 주의
- 격식체(`~합니다`) 금지. 불필요하게 길게 늘리지 말되 기호 풀이는 세세하게
