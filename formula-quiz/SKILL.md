---
name: formula-quiz
description: 수식 덱(02-areas/formula-deck/formula-*.md)을 로컬 웹 퀴즈로 공부. 수식 + 힌트만 보고 의미를 guess → 정답 공개(정체·기호별 풀이·전개·참고 노트) → ✅맞췄다/❌틀렸다/🛑오늘 그만. 스킬 1회 = 모집단 1개에서 최대 5개, 결과는 카드별 기록 줄에 날짜와 누적(연속 3✅ → 💯 졸업). 기호 연습(그리스 문자 랜덤 5개)도 같은 화면에서 고름. "수식 퀴즈", "수식 공부하자", "오늘 수식", "그리스 문자 연습", "formula quiz", "/formula-quiz" 요청 시. 새 논문 수식 추가는 formula-collect.
argument-hint: [모집단 이름 일부 (예: flow, attention, bellman)]
---

# 수식 퀴즈 (formula-quiz)

영단어장(vocab-quiz)의 수식 버전. **수식 + 짧은 힌트**만 보고 이게 뭔지 머릿속으로 해석 → **정답 보기** → 상세 해설 → 채점.

## 덱 구조

- 폴더: `02-areas/formula-deck/` — **모집단 1개 = `formula-<slug>.md` 1개** (README.md는 인덱스)
- 카드 포맷 스펙: `references/deck_format.md` ← 카드 고치거나 추가할 땐 반드시 이거 따를 것 (서버가 기계 파싱함)
- 카드 기록 줄: `</details>` 바로 아래 `기록: ✅2026-10-22 ❌2026-10-25 …` ← **서버가 자동으로 붙임**, 손으로 안 씀

## 세션 규칙

- **스킬 1회 = 모집단 1개 (또는 그 일부)**, 최대 5개 (`FORMULA_MAX` env / 덱 frontmatter `quiz-batch`로 조절)
- 모집단 안 출제 우선순위: 최근 ❌ → 안 본 카드 → ✅ 오래된 순 (💯 졸업 카드 제외). **출제 순서는 파일 순서** (앞 카드가 뒤 카드 재료라서)
- 모집단 추천: 안 본·틀린 카드 있는 것 중 가장 오래 안 본 것 → 처음엔 커리큘럼 순서 (그룹: DL 기초 → 확률·정보이론 → 아키텍처 → Transformer → 생성 모델 → 강화학습, 그룹 안은 frontmatter `order`)
- 버튼: **✅ 맞췄다 / ❌ 틀렸다 / 🛑 오늘 그만** (오늘 그만 = 두 번 눌러야 종료, 지금 카드는 기록 안 함, 그때까지 채점한 것만 저장)
- 저장되면 **서버가 스스로 종료**됨 → 백그라운드 task 종료 알림으로 세션 끝난 걸 알 수 있음
- 단축키: `Enter` 정답 보기, `1` 맞췄다, `2` 틀렸다, `L` 라벨 숨기기/보이기
- **화살표 주석**: 정답 공개 시 문제 수식의 기호마다 색칠 + 위·아래 라벨과 화살표 (데이터 = 카드의 **기호 풀이** bullet, `annotate.js`가 수식과 자동 매칭). 자리 모자란 기호는 색칠만 → 마우스 올리면 설명 말풍선, 기호 풀이 목록 항목에 올리면 수식에서 위치 강조
- 해석 입력칸은 없음 (머릿속으로 guess). 해설 피드백은 정답 아래 "✏️ 해설 피드백 남기기" 눌러야 칸이 열림

## 기호 연습 (그리스 문자)

- 원본 = Theo의 기존 노트 **`02-areas/greek-symbols-dl.md`** (vocab-quiz 형식 `<details><summary>기호</summary>`, 36장: 소문자 24 + 대문자 10 + 보너스 ∇·∂). 덱 폴더 밖이라 `server.py`의 `SYMBOL_NOTES`(덱 폴더 기준 `../greek-symbols-dl.md`)로 읽음 → 첫 화면 맨 위 **기호 연습** 그룹
- **랜덤 5개** (💯 아닌 카드 중 무작위, 순서도 무작위). 노트 frontmatter `quiz-batch: 12`는 vocab-quiz용이라 무시, `quiz-graduate: false`는 따름. 모집단 추천 대상에선 빠짐 → Theo가 골라야 뜸
- **기록은 vocab-quiz와 공유**: 블록 아래 아이콘 줄(`✅ ❌ `, 날짜 없음, id = 블록 순번 `b<N>`)에 vocab-quiz `apply_icons`와 똑같이 누적 ← 두 서버의 블록 regex·줄 형식 바꿀 땐 **둘 다 같이** 바꿀 것
- 노트 본문은 수정 안 함 (기록 줄만). 힌트 없음, 화살표 주석 없음 (기호 하나라서)
- 정답 끝 **이 덱 수식에서 쓰인 곳**은 서버가 세션 때 자동 생성 (`symbol_uses`): 카드 첫 줄 `LaTeX \`\cmd\``의 명령이 **다른 덱 카드의 기호 풀이 bullet 머리**에 나온 곳을 덱 돌아가며 최대 8개 → md엔 안 씀, 덱 늘면 같이 늘어남
- "그리스 문자 하자" → `--pop greek-symbols-dl`. 피드백 반영 땐 id `b<N>` = 노트의 N번째(0부터) `<details>` 블록

## 실행 순서

1. 덱 폴더 확인: 기본 `~/For-Neural-Network-Improvement-Private-/02-areas/formula-deck` (없으면 `FORMULA_DECK_DIR` env 또는 현재 리포의 `02-areas/formula-deck`)
2. 사용자가 모집단을 말했으면 (예: "flow matching 하자") → `ls 02-areas/formula-deck/` 에서 맞는 slug 골라 `--pop <slug>` 로 넘김. 안 말했으면 생략 (첫 화면에서 추천 모집단이 선택돼 있음)
3. 포트 확인: `ss -ltn | grep ':8766 '` (쓰는 중이면 `FORMULA_PORT=8767` 등)
4. **백그라운드로** 서버 실행 (`run_in_background: true`):
   ```bash
   python3 ~/.claude/skills/formula-quiz/server.py [덱폴더] [--pop formula-xxx]
   ```
5. `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8766/` → 200 확인 후 사용자에게 **http://localhost:8766** 안내 (한두 줄)
6. 서버 종료 알림이 오면 `~/.claude/skills/formula-quiz/last_session.json` 읽고 **짧게 보고**: 모집단, ✅/❌ 카드 제목. 피드백이 저장됐으면 "피드백 반영해줘" 하면 된다고 안내
7. 덱 md가 수정됐으니 커밋은 Theo의 평소 루틴(git-pull-push)에 맡김 ← 직접 커밋하지 말 것

## 피드백 반영 ("피드백 반영해줘" 요청 시)

서버는 피드백을 `feedback.json`에 **누적 저장만** 함. 수정은 Claude가.

1. `~/.claude/skills/formula-quiz/feedback.json` 읽기 — 항목: `{date, file, id, title, feedback}`
2. 해당 덱 파일에서 `<!-- formula: <id> -->` 카드를 찾아 **정답·해설 영역**(또는 힌트) 수정. `references/deck_format.md` 규칙·말투 준수
   - **id는 절대 바꾸지 말 것** (기록이 id에 묶임). `기록:` 줄도 건드리지 말 것
   - 수식 자체를 고칠 때는 수학적으로 다시 검증 (가능하면 다른 에이전트에게 검수)
3. 방향이 여러 갈래면 `AskUserQuestion`으로 물어보고 진행
4. 검사 → 반영 요약 보고 → `feedback.json` 비우기 (`[]`)
   ```bash
   python3 ~/.claude/skills/formula-quiz/server.py check <덱폴더>
   node ~/.claude/skills/formula-quiz/check_katex.mjs <덱폴더>
   ```

## 유틸

- 덱 검사 (파싱): `python3 server.py check [덱폴더 | 파일]` — 에러 있으면 exit 1
- 진도 보기: `python3 server.py stats [덱폴더]` — 모집단별 미학습/❌/✅/💯/마지막 날짜 + 추천
- README 표 갱신: `python3 server.py readme [덱폴더]` — `README.md`의 `<!-- 자동 생성 표` 줄부터 끝을 모집단·카드 표로 다시 씀 (덱 추가/카드 변경 후)
- 화살표 주석 검사: `node check_annotate.mjs <덱폴더 | 파일> [-v]` — 기호 풀이 bullet 중 수식에서 못 찾은 것 목록 + 주석 넣은 LaTeX 렌더 확인 (`-v`: 카드별 매칭·라벨 전부)
- KaTeX 렌더 검사: `node check_katex.mjs <덱폴더 | 파일>` — 퀴즈와 같은 규칙으로 수식 추출·렌더. katex 없으면 `npm install katex@0.16.11 --prefix ~/.cache/formula-quiz`
- env: `FORMULA_PORT`(8766) · `FORMULA_MAX`(5) · `FORMULA_DECK_DIR` · `FORMULA_KEEP_ALIVE=1`(저장 후에도 서버 유지, 디버그용)
- 덱 frontmatter: `quiz-batch: N`(이 모집단만 세션 크기 변경) · `quiz-graduate: false`(💯 졸업 없이 계속 출제) · `order: N`(그룹 내 순서) · `group:`
- 서버 강제 종료: `kill $(ss -ltnp | grep ':8766 ' | grep -oP 'pid=\K[0-9]+')`

## 주의

- 서버가 덱 md를 **직접 수정**함 (기록 줄). atomic write라 중간에 죽어도 파일 안 깨짐
- 렌더는 KaTeX 0.16.11 + marked 12.0.2 CDN → 첫 로드에 인터넷 필요
- 해설 속 `[[노트]]` wikilink는 `vscode://file/...` 링크로 바뀜 → 누르면 VS Code에서 해당 노트 열림
