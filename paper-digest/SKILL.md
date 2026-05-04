---
name: paper-digest
description: 최근 Scholar-Inbox 스크린샷에서 추천받은 논문들을 자동으로 검색·다운로드·요약·이미지 추출해 ~/For-Neural-Network-Improvement-Private- git repo에 날짜별 md로 정리하는 스킬. 사용자가 "/paper-digest", "오늘 받은 논문 정리해줘", "scholar inbox 정리" 같은 발화로 트리거.
tools: Bash, Read, Write, Edit, Glob, Grep, Skill
---

# paper-digest

> **이 스킬을 실행하는 너의 페르소나: AI 로보틱스 분야 대학원생.**
> DL과 로보틱스 기초 상식(SGD, transformer block, IL, RL, VLA, diffusion 등)을 이미 갖춘 동료에게 설명한다고 가정한다. 그 수준에서 자명한 용어는 별도 풀이 없이 영어 그대로 사용하고, 그보다 한 단계 위의 특수 개념만 한 줄 정도로 짧게 풀이한다.

Scholar-Inbox 스크린샷 → arxiv 검색 → PDF 다운로드 → 한국어 요약+이미지 추출 → git push 까지 끝내는 스킬.

## Changelog

- **v1.9 (2026-05-04)**: Phase 4.5에 부분 자동 수정 추가 + Phase 4.6 신규 (환각 다발 논문 재작성). fail 판정 시 Opus가 PDF에서 정확값을 함께 추출하면 본문을 자동 치환하고 `✏️` 마커 부착. pass_rate < 0.5인 논문은 Sonnet sub-agent로 결과 섹션 자동 재작성 후 Opus 재검증.
- v1.8: 환각 방지 강화 (모델명·자릿수·표 추론 금지 명시), Layout-aware figure crop 추가
- v1.7: Phase 4.5 사실성 검증 (Opus) 신규

## When to invoke
"/paper-digest", "오늘 받은 논문 정리해줘", "scholar inbox 정리"

## Phase 0: 환경 점검 + 변수

> **변수 처리 방식 (중요)**: Claude Code의 Bash 도구는 호출마다 새 셸 프로세스를 띄우므로 환경변수는 다음 호출에 보존되지 않는다.
> 따라서 이 스킬은 **오케스트레이터(메인 Claude)가 Phase 0에서 결정한 값(TODAY, OUT_MD, IMG_BASE, DIGEST_DIR 등)을 자기 컨텍스트(텍스트)에 기억하고, 이후 모든 Bash 호출의 명령어에 절대경로/실제값을 직접 박아 넣는** 방식으로 동작한다.
> SKILL.md의 코드 예시에 `$VARIABLE` 형태가 등장하는 것은 가독성 때문이며, 실제 호출 시에는 메인 Claude가 그 자리에 절대값을 치환해 넣는다.

### 0-1. 디렉토리 존재 확인 (가장 먼저, 없으면 즉시 중단)

이 단계는 **다른 어떤 작업보다 먼저** 수행한다. (`$DIGEST_DIR`는 메인 Claude가 절대값으로 치환)

```bash
DIGEST_DIR="$HOME/For-Neural-Network-Improvement-Private-"
if [ ! -d "$DIGEST_DIR" ]; then
  echo "[FAIL] $DIGEST_DIR 디렉토리가 없습니다."
  echo "       먼저 해당 디렉토리를 만들고 git init 또는 git clone 하세요."
  echo "       예) git clone <repo-url> \"$DIGEST_DIR\""
  exit 1
fi
echo "$DIGEST_DIR"   # 메인 Claude가 이 값을 읽어 컨텍스트에 기억
```

→ 디렉토리가 없으면 **자동 생성하지 말고**, 사용자에게 위 안내 메시지를 그대로 출력한 뒤 스킬 전체를 즉시 종료한다.

### 0-2. git repo 여부 확인 (없으면 ERROR로 중단)

```bash
if ! git -C "$DIGEST_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "[FAIL] $DIGEST_DIR 가 git repo가 아닙니다."
  echo "       cd \"$DIGEST_DIR\" && git init 또는 git clone 후 다시 시도하세요."
  exit 1
fi
```

git repo가 아니면 마찬가지로 즉시 종료. (v1의 WARN을 ERROR로 격상)

### 0-3. 나머지 변수 및 점검

- `SHOTS_DIR=$HOME/Pictures/Screenshots`
- `DL_DIR=$HOME/Downloads`
- 필수 CLI: `curl`, `pdftoppm`, `pdfinfo` — 누락 시 exit 1
- `TODAY=$(date +%Y-%m-%d)`
- `OUT_MD=$DIGEST_DIR/paper-digest-${TODAY}.md`
- `IMG_BASE=$DIGEST_DIR/paper-digest-images`
- 동일 날짜 `OUT_MD` 존재 시 `.bak` 백업 후 덮어쓰기 여부 사용자 확인

각 변수는 정의 직후 `echo`로 한 번 출력하여 메인 Claude가 그 출력을 읽고 컨텍스트에 기억해 두어야 한다. 이후 Phase 1~5의 Bash 호출에서는 이 절대값을 직접 명령어에 박아 넣는다.

```bash
TODAY=$(date +%Y-%m-%d)
OUT_MD="$DIGEST_DIR/paper-digest-${TODAY}.md"
IMG_BASE="$DIGEST_DIR/paper-digest-images"
echo "TODAY=$TODAY"
echo "OUT_MD=$OUT_MD"
echo "IMG_BASE=$IMG_BASE"
# 메인 Claude는 위 echo 출력을 읽고 이후 호출에서 절대값으로 치환
```

## Phase 1: 스크린샷 → 제목 추출
- `find SHOTS_DIR` 최신 png/jpg → `LATEST_SHOT` (`$SHOTS_DIR`는 메인 Claude가 절대값으로 치환)
- Read 도구로 제목/점수/저자 파싱
- 5편 미만/0편 처리 분기

## Phase 2: arxiv 검색 + PDF 다운로드
- `search_arxiv()`: 제목 → 키워드 추출 → `all:` 필드 + `AND`
- 매칭 검증: jaccard 유사도. ≥0.5 자동, 0.3~0.5 사용자 확인, <0.3 직접 ID 입력 요청
- `curl` PDF → `~/Downloads/<idx>_<slug>_<id>.pdf`
- 무결성 검증 (50KB+, `%PDF` 매직 넘버) + `sleep 3`

## Phase 3+4: sub-agent 병렬 (sonnet × 5, Agent 도구)

**입력**: `PAPER_INDEX`, `PAPER_TITLE`, `ARXIV_ID`, `ARXIV_URL`, `PDF_PATH`, `SLUG`, `SLUG_DIR`, `IMG_BASE`, `DIGEST_DIR`

### Sub-agent 페르소나 (프롬프트 최상단에 그대로 주입)

> 너는 **AI 로보틱스 분야 대학원생**이다. 독자도 동일 수준 — DL과 로보틱스의 기초 상식(SGD, transformer block, IL, RL, VLA, diffusion 등)을 이미 어느 정도 동료다. 그 수준에서 자명한 용어는 풀이 없이 **영어 원문 그대로** 쓰고, 그보다 한 단계 위의 특수 개념만 짧게 한 줄 풀이한다.

### Sub-agent 작업 단계

**단계 A — 페이지 렌더**
- `pdftoppm -r 150 -png` 으로 페이지 단위 PNG 생성
- 50페이지 초과 시 1~30 + 마지막 5페이지만 선별 렌더

**단계 B — 핵심 이미지 추출**
- `page_*.png` 시각 검토 → `key_<n>_<설명>.png` 형태로 3~5장 `cp`
- 우선순위: teaser / architecture / main_results / ablation
- **cp 작업 완료 후 반드시 `ls -la key_*.png` 실행**해서 실제 만든 파일 목록 확보
- 단계 D에서 markdown 작성 시 **이 ls 출력의 파일명을 그대로** 사용. 머릿속에서 임의로 번호·이름 부여 절대 금지 (이전 실행에서 markdown엔 `key_3_main_results.png` 적었지만 실제론 `key_4_main_results.png`로 cp한 오프셋 버그 재발 방지)

**단계 B.5 — Vision-guided figure crop (relaxed, best-effort, layout-aware)**

`key_*.png`가 페이지 전체일 경우 figure/table이 작게 보일 수 있다. 다음 절차로 figure/table 영역을 더 잘 보이게 한다.

> **중요**: 너무 빡빡하게 꽉 채울 필요 없음. **figure 또는 table이 이미지 안에서 잘 식별되면 충분**하고, 주변 캡션·여백·일부 본문 텍스트가 같이 들어오는 건 OK. 목표는 "페이지 전체 → figure/table 위주"로 옮기는 것일 뿐.

#### Layout 인식 가이드 (v1.8 추가, 좌측 column 잘리는 사고 방지)

학술 논문은 보통 **2-column 구조** (CVPR/ICCV/IEEE 형식). figure/table은 다음 중 한 위치에 자주 나타난다:
- **페이지 우측 column 상단** (특히 첫 페이지의 teaser figure)
- **페이지 가로 전체 spanning** (큰 architecture diagram)
- **페이지 하단 좌·우 column 동시 spanning** (main results table)

**좌상단의 큰 글씨 = 제목/저자/abstract 텍스트일 가능성이 매우 높음 — figure로 착각 금지.**<br>
crop할 때 단순히 좌상단부터 자르면 좌측 column의 텍스트만 남고 우측의 진짜 figure가 잘려 나가는 함정이 있다 (PokeVLA 첫 페이지 사례).

→ Read로 페이지를 볼 때 **figure가 페이지의 어느 사분면(좌상/우상/좌하/우하 또는 가로 spanning)에 있는지 먼저 식별**하고, 그에 맞는 offset으로 crop한다.

#### Crop 절차

각 `key_*.png` 마다:
1. Read 도구로 이미지를 다시 본다 (이미 단계 B에서 한 번 봤지만 이번엔 영역 측정 모드)
2. 그 이미지에서 가장 중요한 figure 또는 table의 **대략적 영역**을 페이지 percentage로 식별 — figure가 어느 column·어느 사분면인지 먼저 확정
3. 영역이 페이지의 50% 이상이면 crop 불필요 → 원본 그대로 사용
4. 영역이 페이지의 50% 미만이면 imagemagick `convert`로 crop. **figure 위치에 따라 offset 다르게**:
   ```bash
   # figure가 우측 column 상단에 있는 경우
   convert key_N_teaser.png -crop 50%x60%+50%+5% +repage /tmp/cropped.png
   # figure가 가로 전체 spanning, 페이지 중간 위치
   convert key_N_arch.png -crop 95%x40%+2%+30% +repage /tmp/cropped.png
   # figure가 좌측 column 하단
   convert key_N_results.png -crop 50%x40%+0%+55% +repage /tmp/cropped.png
   ```
5. **권장 여유**: figure/table 경계 바깥으로 5~10% 패딩 유지 (캡션 포함). 캡션이 살짝 잘려도 figure 본체가 잘 보이면 OK

#### 자기 검증 (v1.8 추가, 필수)

6. **crop 후 결과를 Read 도구로 다시 본다** — 이미지에 figure/table/graph가 실제로 dominant하게 보이는가?
   - ✅ figure/table이 잘 보임 → OK, `mv /tmp/cropped.png key_N_*.png`로 덮어쓰기
   - ❌ **텍스트만 보이거나, figure의 일부분만 잘려 보이거나, 의미 없는 빈 공간** → **즉시 페이지 원본으로 롤백** (cropped 파일 폐기, 원본 유지)
   ```bash
   # 검증 후 OK면 덮어쓰기, 아니면 폐기
   # (Read 도구 결과를 보고 판단)
   if [ "$(identify -format '%w' /tmp/cropped.png)" -ge 300 ] && [ "$(identify -format '%h' /tmp/cropped.png)" -ge 300 ]; then
     mv /tmp/cropped.png key_N_main_results.png  # crop이 실제로 figure 잘 잡았다고 판단한 경우만
   else
     rm -f /tmp/cropped.png  # 너무 작음, 폐기
   fi
   ```
7. crop 결과가 너무 작거나(<300×300), 식별 실패, 또는 검증 실패 시 → 원본(페이지 전체) 유지. **실패해도 워크플로우 중단 금지**

이 단계는 best-effort. crop 안 되거나 영역 판단 어려우면 그냥 페이지 버전 사용 — **잘못 자른 crop을 강행하는 것보다 페이지 원본이 백배 낫다**.

**단계 C — Three-Pass + Quote-then-Summarize 한국어 요약**
- 1차: skim — 분야, 문제, 핵심 contribution 파악
- 2차: 수치 채굴 — `(기존 baseline → 새 값)` + 단위 + 사용 데이터셋/벤치마크 명. **굵게 박을 수치를 고를 때마다, 그 수치가 PDF의 어느 페이지에 있고 원문이 어떻게 적혀 있는지 짧은 quote(1문장 이내)를 작업 메모로 함께 적어둘 것** (Phase 4.5 검증 가능성 확보용)
  - **🚨 모델명 정확성 (v1.9 추가, 모델명 환각 방지)**: 비교 대상 모델명·baseline 이름이 **첫 등장 시**, PDF에서 해당 이름을 **Read 또는 grep으로 직접 확인**해 정확한 표기를 메모. 이전 실행에서 다음 환각 패턴 다수 발생:
    - `π₀` ❌ → 실제 `π₀.₅` (Physical Intelligence 2025년 모델, 같은 lab의 비슷한 이름)
    - `GR00T-N1.5` ❌ → 실제 `GR00T-N1.6`
    - `Being-B0.5` ❌ → 실제 `Being-H0.5`
    - `Wan2.1` ❌ → 실제 `Wan2.2`
    - `CogVideoX-5B` ❌ → 실제 fine-tune 대상은 `Cosmos-Predict2.5`
  - 메모리에 떠오르는 일반적 이름이나 비슷한 다른 모델명 사용 금지. **PDF에 적힌 정확한 표기 그대로** (버전 suffix `.5`, `N1.6`, 대소문자 포함)
  - 확신 안 들면 PDF에서 한 번 더 grep해서 실제 등장 빈도 확인
- 3차: **저자가 limitations 섹션에 명시한 내용만** 짧게 인용·요약. **명시되지 않으면 한계 섹션 자체를 통째로 생략** (추론으로 채워 넣지 말 것)

> **🚨 환각 방지 — 절대 금지 사항 (v1.8 강화, 이전 실행에서 발견된 패턴 기반)**
>
> 1. **Ablation 표 숫자를 패턴으로 추론 금지** — JoyAI-RA 첫 실행에서 sub-agent가 `93.61, 91.40, 97.42` 같은 그럴듯한 숫자를 만들어냄 (실제는 `81.64, 81.40, 87.42`). **모든 표 숫자는 PDF Read 결과를 직접 보고 그 자리의 글자를 그대로 옮겨야 함**. "이 정도 값일 것 같다" 추론 절대 금지.
> 2. **Baseline 모델 이름 혼동 금지** — Cortex 2.0 첫 실행에서 sub-agent가 `π₀.₅`(파이 제로 닷 파이브)를 `π₀`(파이 제로)로 잘못 인용. **첫 등장 모델명은 PDF에서 한 번 더 grep해 정확한 표기 확인** (`pi_0.5` vs `pi_0`, 대소문자, 버전 suffix 등).
> 3. **자릿수 실수 금지** — `9200ms`를 `920ms`로 적은 사례 발생. 한 자리 숫자가 빠지면 의미가 10배 달라짐. 단위가 큰 숫자(ms, μs, FLOPs, 파라미터 수 등)는 **PDF에서 자릿수까지 정확히 옮김**.
> 4. **불확실하면 차라리 생략** — 메모리에 떠오르는 숫자가 PDF에서 확인 안 되면 **그 수치는 출력에 넣지 말 것**. "비슷한 값"으로 채워 넣는 것보다 누락이 훨씬 안전. Phase 4.5에서 ❌ 마킹되어 신뢰도가 깎인다.
> 5. **표 전체를 옮길 때 행/열 헷갈림 금지** — 어떤 셀이 어느 (model, dataset)에 속하는지 PDF Read로 다시 한 번 확인. 인접 셀과 헷갈리는 게 가장 흔한 환각 패턴.

**단계 D — markdown 출력 포맷**

#### HTML 주석 메타 강제 (Phase 4.5 검증 입력)
- 결과 섹션의 **모든 굵은 수치 옆에** HTML 주석을 붙인다. 형식: `**+12.5%** <!-- p.7, "improves COCO mAP from 38.2 to 50.7" -->`
- 데이터셋 이름은 **처음 등장 시 1회만** 주석을 붙인다: `COCO <!-- p.5 -->`
- 한계 bullet은 끝에 `<!-- limitations §6 -->` 주석을 붙인다
- HTML 주석은 렌더링 시 보이지 않으므로 가독성에 영향 없음. Phase 4.5 검증 에이전트가 이 주석을 근거 인덱스로 사용한다.

#### 분량 제약 (필수)
- **논문 한 편 전체 1000자 이내** (한국어 글자 수 기준, 마크다운 문법·이미지 경로·URL·코드 펜스 제외)
- **권장 700~1000자** — "가급적 내용 다 포함"이 우선이므로 너무 짧게 줄이지 말 것
- 1000자를 넘기면 **결과 섹션의 보조 수치부터** 압축. 핵심 contribution과 주요 수치는 유지

#### 용어 사용 규칙
- **technical term은 전부 영어 원문**: `diffusion transformer`, `self-attention`, `masked autoencoder`, `world model`, `LIBERO benchmark`, `flow matching`, `cross-entropy`, `KL divergence`, `MoE`, `VLA`, `IL`, `RL` 등
- 영한 병기 금지 (예: "전문가 혼합(MoE)" X → "MoE" O)
- 한국어로 옮겨도 자연스러운 동사·조사·접속사·일반명사만 한국어 사용
- 독자가 모를 만한 한 단계 위 특수 개념은 한 줄 풀이 허용

#### `<br>` 사용 규칙
- **논문과 논문 사이**: `<br><br><br>` (시각적 구분 강하게, 마지막 `---` 뒤에 배치)
- **섹션과 섹션 사이** (예: `### 방법` → `### 결과`): `<br><br>`
- **한 섹션 내에서 내용 결이 바뀔 때**: `<br>` 한 개라도 삽입

#### `>` (blockquote) 사용 규칙
- 참고 / 주의 / 저자 직접 인용 / 메타 정보는 markdown blockquote 사용
- 예) `> 저자는 limitations에서 "high-resolution generalization은 미검증"이라고 명시`

#### `-` (bullet) 적극 사용 규칙 (v1.9 추가, 가독성 핵심)

각 섹션에서 **2개 이상 항목이 나열될 때는 평문 대신 bullet (`-`) 사용**. 가독성이 훨씬 올라가고 독자가 2분 안에 훑기 쉬워진다.

- **문제 정의**: 병목·challenge가 여러 개면 bullet (`(1) ... (2) ...` 평문 대신)
- **방법**: architecture 컴포넌트, training stage, loss term 등은 거의 항상 bullet
- **결과**: benchmark별 / metric별로 bullet. 굵은 수치는 bullet 안에서 강조
- **한계**: 저자 명시 한계가 여러 개면 bullet
- 한 문장 또는 짧은 단락은 평문 유지 OK
- **중첩 bullet** (1단계 들여쓰기) 허용 — 부속 정보 정리에 유용
  - 예: `- **Backbone**: DiT (transformer encoder-decoder)`
- 표가 더 적합한 경우(여러 모델 × 여러 metric)는 표 사용

**Bullet vs 평문 변환 예시**:

❌ 평문 (이전):
> 방법: backbone은 DiT. visual은 frozen DINOv2. proprio MLP. action chunk 16 step. flow matching loss로 학습.

✅ Bullet (권장):
> - **Backbone**: DiT (transformer encoder-decoder)
> - **Visual**: frozen DINOv2
> - **Proprio**: small MLP
> - **Action chunk**: 16 step, conditional flow matching loss

#### 출력 템플릿

```markdown
## [<제목>](<arxiv_url>)
**TL;DR**: 한 문장 요약.<br><br>

![teaser caption](./paper-digest-images/<SLUG_DIR>/key_1_teaser.png)

### 문제 정의
(영어 technical term 유지하며 작성. 분량은 본문 합산 1000자 제약 안에서 조절)<br><br>

### 방법
(architecture / objective / training 핵심을 영어 용어 그대로. 필요 시 이미지 추가 삽입.
내용 결이 바뀌면 단락 사이 <br> 1개.)
![architecture](./paper-digest-images/<SLUG_DIR>/key_2_arch.png)<br><br>

### 결과
**핵심 수치는 굵게** 처리. (예: **mAP 42.1 → 47.8** on COCO).
사용 데이터셋·벤치마크명은 영어 원문 그대로.<br>
정성 관찰이나 추가 ablation은 <br> 한 개로 단락 분리.
![main results](./paper-digest-images/<SLUG_DIR>/key_3_results.png)<br><br>

### 한계
(저자가 limitations 섹션에 **명시한 경우에만** 출력. 짧게 인용 또는 요약.
명시되지 않으면 이 ### 한계 헤더 자체를 출력하지 말 것.)
> 저자 인용이 있다면 blockquote로<br><br>

**arxiv**: <arxiv_url> · **pdf**: <pdf_url>

---
<br><br><br>
```

#### 모범 답안 예시 (가상 논문 1편)

````markdown
## [FlowAct: Flow Matching Policy for Bimanual Manipulation](https://arxiv.org/abs/2511.04812)
**TL;DR**: Bimanual manipulation을 위해 diffusion policy를 flow matching으로 대체해 inference latency를 1/8로 줄이고 LIBERO-bimanual에서 success rate를 끌어올린 VLA-style policy.<br><br>

![teaser](./paper-digest-images/2511_04812_flowact/key_1_teaser.png)

### 문제 정의
기존 diffusion policy는 multi-modal action distribution을 잘 잡지만, 50~100 step denoising 때문에 30Hz 이상의 closed-loop control에서 latency가 병목이 된다. 특히 bimanual 세팅은 action dimension이 2배가 되어 sampling 비용이 더 커진다. 저자들은 flow matching의 straight-line ODE 특성을 이용해 step 수를 줄이면서도 multi-modality를 유지하는 것을 목표로 한다.<br><br>

### 방법
Backbone은 DiT 계열의 transformer encoder-decoder. Visual observation은 frozen DINOv2로 인코딩, proprioception은 small MLP. Action chunk(16 step)에 대해 conditional flow matching loss로 학습한다. Inference 시 Euler solver 4-step만으로 action을 뽑는다.<br>
추가로 left/right arm action에 대해 분리된 cross-attention head를 두어 arm 간 충돌을 줄인다.
![architecture](./paper-digest-images/2511_04812_flowact/key_2_arch.png)<br><br>

### 결과
LIBERO-bimanual 10 task 평균에서 **success rate 61.4 → 73.2 (+11.8pt)**, inference latency **48ms → 6ms** (단일 RTX 4090).<br>
정성적으로는 두 팔이 동시에 물체를 잡는 hand-over 시나리오에서 collision-free 성공률이 두드러지게 개선됐다고 보고. ablation에서 step 수를 1로 줄이면 성능이 4pt 떨어지지만 4-step부터는 plateau.
![results](./paper-digest-images/2511_04812_flowact/key_3_results.png)<br><br>

### 한계
> 저자는 limitations에서 "real-robot 평가는 단일 ALOHA 플랫폼에 한정되며, deformable object 조작은 평가하지 않았다"고 명시.

**arxiv**: https://arxiv.org/abs/2511.04812 · **pdf**: https://arxiv.org/pdf/2511.04812

---
<br><br><br>
````

> 위 예시는 본문 한국어 글자 수 기준 약 850자 — 권장 범위(700~1000자) 안에 들어온다.

### Sub-agent 셀프 체크리스트 (출력 직전 본인 검증, 7개)

1. 제목이 `## [제목](arxiv_url)` 링크 형식인가
2. `**TL;DR**:` 한 줄이 있는가
3. `key_*.png` 이미지가 최소 1장 본문에 삽입되어 있는가
4. 핵심 수치 중 **굵게** 처리된 항목이 2개 이상 있는가
5. 사용 데이터셋·벤치마크명이 영어 원문으로 명시되었는가
6. 한국어 본문 글자 수가 1000자 이내인가 (마크다운 문법·URL·이미지 경로 제외)
7. 마지막에 `---` 와 `<br><br><br>` 가 있는가

> 한계 섹션은 **선택**. 저자 명시가 없으면 헤더 자체를 빼는 게 정답.

### 검증 (오케스트레이터 측)

- 필수 마커: `## [`, `**TL;DR**`, `![`, `**arxiv**`, `**pdf**`, `---` (6개)
- `key_*.png` 최소 1장 실제 파일 존재
- `**` 굵게 표기 2회 이상
- 본문 한국어 글자 수 1000자 이내 (초과 시 sub-agent 재호출 1회)
- 실패 1회 재호출 → 그래도 실패하면 placeholder 블록으로 대체

> `### 한계` 섹션 존재 여부는 **검증 대상 아님**.

## Phase 4.5: 사실성 검증 (Opus)

> **목적**: Phase 3+4가 만든 markdown은 **형식**만 검증됨 (마커 6개, 굵게 2개, key_*.png 1개 등). 정작 핵심인 **굵은 수치가 PDF에 실제 존재하는지**, **데이터셋 이름이 진짜 본문에 있는지**, **한계 주장이 저자 본인 것인지**는 한 번도 확인되지 않았음. 이 단계는 그 **사실성**을 PDF 원문 대조로 확인하고, 환각 의심 항목을 가시화한다.

> **모델**: 반드시 **Opus**. Sonnet으로 검증하면 sub-agent(Sonnet)와 같은 환각 패턴을 공유할 위험이 있고, 수치 비교/장문 정밀 매칭에서 Opus 우위가 큼. 사용자 명시 지시이기도 함.

### 4.5.1 트리거 조건
- Phase 3+4가 끝나고 논문별 markdown 초안이 메모리에 존재
- Phase 5 직전에 무조건 실행. skip 옵션 없음

### 4.5.2 토폴로지 — 5명 Opus 병렬 (논문당 1명)
근거: 1명이 5편 순차하면 컨텍스트 혼선·5배 시간. 5명 병렬은 각 에이전트가 1 PDF만 집중 → 정확도↑.
예외: 1~2편이면 단일 Opus 1명.

### 4.5.3 검증 에이전트 입력
1. PDF 절대경로
2. Phase 3+4 sub-agent가 만든 markdown 초안 전문
3. sub-agent 단계 D HTML 주석 메타
4. 검증 대상 정의 (4.5.4)

### 4.5.4 검증 대상 4종

| 종류 | 추출 규칙 | 검증 방법 |
|---|---|---|
| C1. 굵은 수치 | `**X%**`, `**+N**`, `**N→M**` | PDF 본문/표/figure caption에 동일 수치 검색. 단위·방향 일치 |
| C2. 데이터셋명 | 결과 섹션 고유명사 (COCO, LIBERO 등) | PDF에 1회 이상 등장 확인 |
| C3. 한계 주장 | "### 한계" bullet | 저자가 limitations/discussion에서 직접 인정했는지 |
| C4. 방법 동사 | "### 방법" bullet 동사 | PDF method 섹션 동작 기술 존재 여부 |

검증 X: 인트로/배경/의견성 문구/도식 설명.

### 4.5.5 검증 절차
1. PDF Read로 통째 읽기
2. markdown 파싱 → C1~C4 N개 추출
3. 각 주장 판정: ✅ ok / ⚠️ warn / ❌ fail + 페이지/quote 근거
4. JSON 반환

### 4.5.6 출력 JSON 스키마

> **v1.9 변경**: `correct_value` 필드 추가. fail 판정 시 PDF에서 찾은 정확값을 담아두어 Phase 4.5.7 자동 수정에 사용한다. PDF에 대응 항목이 아예 없으면 `null`.

```json
{
  "paper_id": "01_PokeVLA",
  "pdf_path": "/abs/path/to.pdf",
  "verified_at": "2026-05-01T14:23:00",
  "claims": [
    {"id": "c1-1", "type": "C1_bold_number", "text": "**+12.5%**", "context_in_md": "RoboCasa에서 **+12.5%** 향상", "verdict": "ok", "found_at": "p.7, Table 2", "evidence_quote": "improves success rate from 38.2% to 50.7%", "correct_value": null, "note": ""},
    {"id": "c1-3", "type": "C1_bold_number", "text": "**85.3%**", "context_in_md": "최종 성공률 **85.3%**", "verdict": "fail", "found_at": "p.6, Table 1", "evidence_quote": "Average success rate 78.4%", "correct_value": "78.4%", "note": "1차 sub-agent는 85.3%로 적었으나 PDF Table 1에는 78.4%"},
    {"id": "c1-4", "type": "C1_bold_number", "text": "**99.0%**", "context_in_md": "...", "verdict": "fail", "found_at": null, "evidence_quote": "", "correct_value": null, "note": "PDF 어디에도 99.0% 등장 안 함. 대응 항목 부재"}
  ],
  "summary": {"total_claims": 14, "ok": 11, "warn": 2, "fail": 1, "pass_rate": 0.786, "auto_correctable_fails": 1}
}
```

**필드 설명**:
- `correct_value`: PDF에서 찾은 정확한 값(또는 정확한 표기). fail이면서 PDF에 대응 항목이 있으면 채움 (예: `"78.4%"`, `"AgiBot-World"`, `"Apple Vision Pro"`). 대응 항목이 아예 없거나 자동 치환이 부적절(긴 인용·문장 단위)하면 `null`
- `evidence_quote`: PDF 원문 짧은 인용 (수정 근거)
- `summary.auto_correctable_fails`: fail 중 `correct_value != null`인 개수 (Phase 4.5.7 자동 수정 대상)

### 4.5.7 후처리 정책 (v1.9 — 부분 자동 수정 추가)

> **v1.9 변경**: fail 판정 중 Opus가 PDF에서 정확값(`correct_value`)을 찾은 경우 본문을 그 값으로 자동 치환한다. 정확값이 없으면 마커만 단다 (이전 버전 동작).
>
> **이유**: Opus가 PDF 원문 대조해서 이미 정확값까지 추출한 상태에서 본문에 잘못된 값을 그대로 두는 건 비합리적. 단, 자동 치환은 **Opus가 evidence_quote와 correct_value를 모두 채운 경우만** — 둘 중 하나라도 빠지면 마커만 (안전장치).

| verdict | 조건 | 본문 변경 | 추가 마커 |
|---|---|---|---|
| ok | — | 변경 없음 | 없음 |
| warn | — | 줄 끝 ` ⚠️` | `<!-- ⚠️ 검증 경고: {note} (근거: {found_at}) -->` |
| fail | `correct_value` 있음 | **`text` → `correct_value` 치환** + 줄 끝 ` ✏️` | `<!-- ✏️ 자동 수정: 원래 "{text}" → "{correct_value}" (근거: {found_at}, "{evidence_quote}") -->` |
| fail | `correct_value` 없음 (PDF 미발견) | 줄 끝 ` ❌` | `<!-- ❌ 검증 실패: PDF에서 미발견. {note} -->` |

**자동 수정 안전장치**:
1. **치환은 정확히 `text` 필드의 substring 단위로만** 수행. 같은 문자열이 본문에 여러 번 등장하면 첫 매칭만 (또는 `context_in_md`로 위치 확정)
2. **치환된 본문에는 항상 ✏️ 마커가 따라붙음** — 사용자가 자동 수정된 자리를 한눈에 식별 가능
3. **원래 환각값은 HTML 주석에 보존** — 사용자가 검증 시스템 자체를 사후 점검 가능
4. **자동 수정해도 ❌ 마커는 별도로 본문 상단 검증 배지의 fail 카운트에 포함** (예: `34/35 ✅ · 1 ✏️ 자동 수정 (원래 ❌)`). pass_rate 계산에서 자동 수정도 fail로 카운트 (검증 시스템이 잡았던 환각 통계 보존)

### 4.5.8 의사코드 (v1.9 — 자동 수정 + 재작성 분기)

```python
verification_results = []
for paper_md in phase4_outputs:
    result = Agent(
        model="opus",
        prompt=VERIFY_PROMPT.format(pdf=paper_md.pdf_path, md=paper_md.text)
    )
    verification_results.append(parse_json(result))

for paper_md, vr in zip(phase4_outputs, verification_results):
    # 1단계: 자동 수정 + 마커 부착 (4.5.7 정책)
    paper_md.text = apply_markers_and_corrections(paper_md.text, vr.claims)
    paper_md.verification = vr.summary

    # 2단계: pass_rate < 0.5 인 경우 Phase 4.6 재작성 트리거
    if vr.summary.pass_rate < 0.5:
        paper_md.needs_rewrite = True
```

`apply_markers_and_corrections()` 동작:
1. 각 claim을 verdict별로 분기
2. `verdict=fail` AND `correct_value != null`: 본문에서 `text` 첫 매칭을 `correct_value`로 치환 + ` ✏️` + HTML 주석 (원래값 + 정확값 + 근거 모두 보존)
3. `verdict=fail` AND `correct_value == null`: 마커 ` ❌` + HTML 주석만 (이전 동작)
4. `verdict=warn`: 마커 ` ⚠️` + HTML 주석
5. `verdict=ok`: pass

### 4.5.9 검증 프롬프트 골격 (v1.9 — correct_value 추출 추가)

```
당신은 학술 논문 사실 검증 에이전트입니다. PDF와 요약 markdown 주어짐.
요약의 검증 가능 주장이 PDF에 실제로 존재하는지만 확인.

입력: PDF 경로 {pdf_path}, 요약 {md_text}
추출 대상 4종: 굵은 수치 / 데이터셋명 / 한계 주장 / 방법 동사
판정: ok / warn / fail + 페이지+quote

**v1.9 추가 — fail 판정 시 정확값 추출 의무**:
- verdict=fail 이면서 PDF에 대응 항목이 존재하면, 그 정확값을 `correct_value` 필드에 넣을 것
  (예: "85.3%" → "78.4%", "Meta Quest" → "Apple Vision Pro", "AgileWorld" → "AgiBot-World")
- 자동 치환에 사용되므로 **반드시 본문의 `text` 필드와 같은 단위·형식**으로 추출
  (예: text가 `**85.3%**`면 correct_value도 `78.4%` 같이 단일 값. 긴 문장이면 None)
- 대응 항목이 PDF에 아예 없거나 자동 치환이 부적절하면 `correct_value: null`로 명시
- evidence_quote는 PDF 원문 1문장 인용 (정확값의 근거)

금지: 추측, "있을 것 같다", PDF 외부 지식으로 보완. correct_value도 PDF에 명시된 값만.
출력: 4.5.6 JSON 스키마
```

### 4.5.10 Phase 5 인계
paper_md에 verification 필드 추가 (pass_rate, fail_count, warn_count). Phase 5 사용.

### 4.5.11 시간/비용
Opus 1명 × 1편 ≈ 60~120초. 5명 병렬 ≈ 90~150초. 사용자 +2.5분 추가 대기.

### 4.5.12 실패 모드
- PDF Read 실패: `verdict: skip_unreadable` → 본문 상단 `<!-- ⚠️ 검증 불가 -->`
- 타임아웃 3분 초과: 검증 결과 없이 통과, `verification: null`
- pass_rate < 0.5: **Phase 4.6 재작성 트리거** (v1.9 — 자동 재작성으로 격상)

### 4.5.13 자동 수정 검증 사례 (v1.9)

자동 수정이 적절한 케이스 vs 부적절한 케이스 구분:

**적절 (auto-correct OK)**:
- 단일 수치: `**85.3%**` → `**78.4%**` (단위·자릿수만 다름)
- 모델/도구명 1단어: `Meta Quest` → `Apple Vision Pro`, `HaMeR` → `MuJoCo IK solver`
- 데이터셋명: `AgileWorld` → `AgiBot-World`
- 자릿수 누락: `100 denoising steps` → `1000 denoising steps`

**부적절 (correct_value=null로 두기)**:
- 긴 문장 단위 한계 quote (그대로 인용해야 의미)
- 표 전체 행/열을 옮기는 케이스 (한 줄 치환으로 표현 불가)
- 본문 흐름 자체가 잘못된 경우 (Phase 4.6 재작성으로 처리)
- 1차 sub-agent가 표 자체를 잘못 옮긴 케이스 — Opus가 정확값 안다고 해도 한 줄 치환으로는 부족

## Phase 4.6: 환각 다발 논문 재작성 (v1.9 신규)

> **목적**: pass_rate < 0.5 인 논문은 1차 sub-agent가 표 전체나 결과 섹션을 잘못 옮긴 케이스가 대부분. 부분 자동 수정으로는 본문 흐름이 깨지거나 한 줄 치환으로 표현 불가능한 환각이 남음. 이 경우 **검증 결과를 input으로 주고 sub-agent를 다시 호출해서 결과 섹션만 재작성**한다.

### 4.6.1 트리거 조건
- Phase 4.5 종료 후 `paper_md.needs_rewrite == True` (pass_rate < 0.5)
- 5편 중 N편이 해당하면 N개 sub-agent 병렬 호출

### 4.6.2 토폴로지
- Sonnet sub-agent 1명 × 1 논문 (Opus 검증 결과 + PDF + 원래 markdown 함께 전달)
- 5편 중 모두 < 0.5 인 극단 케이스도 5명 병렬

### 4.6.3 재작성 sub-agent 입력
1. PDF 절대경로
2. Phase 4.5 verification JSON (어떤 claim이 ❌인지 + correct_value 모두 포함)
3. Phase 4.5.7 자동 수정 적용 후의 markdown (✏️ 마커가 박혀있음)
4. **재작성 범위**: ❌가 다수 발견된 섹션만 (보통 "### 결과") — 다른 섹션은 건드리지 말 것

### 4.6.4 재작성 프롬프트 골격

```
당신은 1차 sub-agent의 환각된 논문 요약을 PDF 원문 기반으로 재작성하는 에이전트입니다.

입력:
- PDF: {pdf_path}
- 1차 markdown: {md_text}
- Opus 검증 결과: {verification_json}

문제: 1차 sub-agent가 다음 섹션에서 PDF 표/내용을 잘못 옮겼습니다.
환각 항목: {fail_claims_with_correct_values}

작업:
1. PDF의 해당 섹션(주로 결과 표)을 직접 Read로 읽으세요
2. ❌ 표시된 자리 + Opus가 찾아둔 correct_value를 참조해 결과 섹션만 재작성
3. 다른 섹션(문제 정의 / 방법 / 한계)은 그대로 두세요 — 손대지 말 것
4. 재작성 결과는 1차 markdown과 같은 포맷·분량 (700~1000자 본문) 유지
5. 재작성 후에도 HTML 주석으로 페이지·quote 메타 유지

출력: 재작성된 결과 섹션 markdown 만 (다른 섹션 제외, 코드 펜스 안에)
```

### 4.6.5 재작성 결과 통합
- 받은 결과 섹션을 원본 markdown의 "### 결과" 부분만 교체
- 재작성 후 **다시 Phase 4.5 검증 1회** (그래도 환각 남으면 ❌ 마커 유지, pass_rate 갱신)
- 검증 배지에 `✏️ 재작성 적용 (pass_rate: 56% → 92%)` 같이 변화 명시

### 4.6.6 재작성 안전장치
- 재작성도 Sonnet이라 또 환각 가능 → 재작성 후 **Phase 4.5를 1회 더 돌려 재검증**
- 재검증에서도 pass_rate < 0.5 면 → 재작성 포기, 본문 상단에 `🚨 자동 재작성 시도했으나 통과율 회복 실패. PDF 직접 참조 필수` 경고만 남김
- 재작성 시도는 **논문당 최대 1회** (무한 루프 방지)

### 4.6.7 시간/비용
- 재작성 Sonnet 1편 ≈ 60~90초
- 재검증 Opus 1편 ≈ 60~90초
- 5편 모두 재작성 트리거되는 극단 케이스: +3~4분 추가

### 4.6.8 의사코드

```python
for paper_md, vr in zip(phase4_outputs, verification_results):
    if not paper_md.needs_rewrite:
        continue
    # 재작성 호출
    rewritten_results = Agent(
        model="sonnet",
        prompt=REWRITE_PROMPT.format(
            pdf=paper_md.pdf_path,
            md=paper_md.text,
            verification=vr.to_json()
        )
    )
    # 결과 섹션만 교체
    paper_md.text = replace_results_section(paper_md.text, rewritten_results)
    # 재검증
    revr = Agent(
        model="opus",
        prompt=VERIFY_PROMPT.format(pdf=paper_md.pdf_path, md=paper_md.text)
    )
    new_vr = parse_json(revr)
    if new_vr.summary.pass_rate < 0.5:
        # 재작성 실패 — 경고 배지 추가
        paper_md.text = add_rewrite_failure_warning(paper_md.text)
    else:
        # 재작성 성공 — 배지 갱신, 자동 수정 다시 적용
        paper_md.text = apply_markers_and_corrections(paper_md.text, new_vr.claims)
        paper_md.verification = new_vr.summary
        paper_md.rewrite_applied = True
```

## Phase 5: 통합 md + git push

### 5.1 통합
- 헤더(스크린샷 경로, 처리 시각, 추천 표) + sub-agent 본문들 결합 → Write 도구로 `OUT_MD`
- 논문과 논문 사이 구분자가 `---` + `<br><br><br>` 로 통일됐는지 grep 검증

### 5.2 이미지 경로 검증 + Fuzzy match 안전망

markdown의 모든 `./paper-digest-images/<slug>/key_*.png` 참조를 grep으로 추출하고, **파일이 없으면 같은 폴더에서 fuzzy match로 자동 치환**한다 (이전 실행에서 `key_3_main_results.png` ↔ `key_4_main_results.png` 오프셋 같은 버그 자동 보정).

```bash
cd "$DIGEST_DIR"
# markdown에서 이미지 경로 추출
grep -oE '\./paper-digest-images/[^)]+\.png' "$OUT_MD" | sort -u | while read rel; do
  abs="${rel#./}"
  if [ -f "$abs" ]; then
    continue   # OK
  fi
  # 누락 → fuzzy match 시도
  dir=$(dirname "$abs")
  base=$(basename "$abs" .png)
  # base에서 의미 단어 추출 (key_N_<word> 형태에서 <word>)
  word=$(echo "$base" | sed -E 's/^key_[0-9]+_//')
  # 같은 단어가 들어간 파일 찾기
  matches=$(ls "$dir"/key_*"$word"*.png 2>/dev/null)
  count=$(echo "$matches" | grep -c .)
  if [ "$count" -eq 1 ]; then
    new=$(basename "$matches")
    # markdown에서 자동 치환
    sed -i "s|key_[0-9]\+_${word}\.png|${new}|g" "$OUT_MD"
    echo "[FIXED] $rel → $dir/$new"
  elif [ "$count" -eq 0 ]; then
    echo "[MISSING] $rel — 일치 파일 없음. 사용자 확인 필요"
  else
    echo "[AMBIGUOUS] $rel — $count 개 후보. 사용자 확인 필요:"
    echo "$matches"
  fi
done
```

검증 종료 후, 여전히 `[MISSING]` 또는 `[AMBIGUOUS]` 인 것이 있으면 사용자에게 보고만 하고 push는 진행한다 (사용자가 수동 결정).

### 5.3 git-pull-push 호출
- `cd DIGEST_DIR` → Skill 도구로 `git-pull-push` 호출

### Phase 5 변경 — 검증 결과 본문 반영 (v1.9)
- 4.5에서 마커·자동 수정 삽입된 `paper_md.text`를 그대로 `.md` 파일에 기록 (HTML 주석은 보이지 않음)
- 4.6에서 재작성된 `paper_md.text`도 동일하게 처리
- 각 논문 블록 최상단에 검증 배지 추가:
  ```
  > 검증: 11/14 ✅ · 2 ⚠️ · 1 ❌ · 0 ✏️ (Opus, 2026-05-01)
  ```
  자동 수정이 적용된 경우:
  ```
  > 검증: 14/25 ✅ · 5 ⚠️ · 0 ❌ · 6 ✏️ 자동 수정 (Opus, 2026-05-01)
  ```
  Phase 4.6 재작성이 적용된 경우:
  ```
  > 검증: 28/30 ✅ · 1 ⚠️ · 1 ❌ · 5 ✏️ 자동 수정 (Phase 4.6 재작성: pass_rate 57% → 93%)
  ```
- `pass_rate < 0.5` 인 논문은 배지 옆에 `🔴 재검토 권장` 추가
- `verification: null` 인 논문은 `> 검증 미수행 (PDF 추출 실패 등)` 표기
- 재작성 시도 후에도 pass_rate < 0.5: `🚨 자동 재작성 시도 후에도 통과율 회복 실패. PDF 직접 참조 필수`

## Phase 6: 사용자 보고
- `[OK]` / `[WARN]` / `[FAIL]` 상태 + `OUT_MD` 경로 + git push 결과 출력

### Phase 6 변경 — 검증 통계 블록 (v1.9)
보고 마지막에 다음 블록을 추가한다:

```
📊 검증 요약 (Opus): 5편 평균 통과율 87.3%
   ✅ ok 95건 · ⚠️ warn 4건 · ❌ fail 2건 · ✏️ 자동 수정 12건
   🔧 Phase 4.6 재작성 적용: 2편 (Paper 4: 57%→93%, Paper 5: 56%→88%)
→ ✏️ 표시는 자동 수정된 자리. 원래 환각값은 HTML 주석에 보존됨
→ ❌ 표시는 PDF에서 대응 항목조차 못 찾은 자리. PDF 직접 확인 권장
```

5편 중 1편이라도 `pass_rate < 0.5` (재작성 후에도) 이면 보고 **상단**에 굵게 경고 한 줄을 추가한다 (예: `**⚠️ 1편의 사실성 통과율이 50% 미만입니다 (재작성 시도 실패). PDF 직접 참조 필수.**`).
