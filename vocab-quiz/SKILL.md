---
name: vocab-quiz
description: 영어 단어/개념 단어장(markdown)을 로컬 웹 퀴즈로 풀고, 결과를 md 파일에 아이콘으로 누적 기록한다. `<details><summary>단어</summary> 뜻 </details>` 포맷의 단어장에 사용. "단어 시험", "단어장 퀴즈", "vocab quiz" 요청 시.
---

# 영어 단어/개념 퀴즈 (vocab-quiz)

`<details>/<summary>` 포맷의 단어장 md를 로컬 퀴즈 사이트로 띄우고, 결과를 md에 누적 마킹한다.

## 단어장 포맷 (전제)

각 단어/개념은 아래 형태의 블록 하나. `<summary>`가 문제(예문/단어), 안쪽 본문이 정답.

```markdown
<details>
<summary>coarse</summary>

거친, 조잡한

</details>
```

- 섹션 헤더 `## 주목할만한 개념` / `## 영어 단어 및 표현` 로 개념·단어 구분 (있으면 태그로 표시, 없어도 동작)

## 동작

1. md를 파싱해 퀴즈 항목 생성 (랜덤 출제)
2. 카드에 문제 표시 → **[안다]/[모른다]** → 정답 공개 → 안다면 **[맞췄다]/[틀렸다]**
3. 끝나면 각 블록 **아래 줄에 아이콘 누적**:
   - `✅` 안다+맞췄다 / `🔁` 모른다(다시 볼 것) / `❌` 안다인데 틀림
4. **연속 3✅ 달성 → `💯` 부여 + 이후 출제 제외**(졸업)

## 실행 방법

1. 대상 단어장 md 경로를 확인한다 (사용자가 안 주면 물어본다).
2. 포트 비었는지 확인: `ss -ltn | grep ':8765 '` (쓰는 중이면 `VOCAB_PORT` 환경변수로 변경)
3. 백그라운드로 서버 실행:
   ```bash
   python3 ~/.claude/skills/vocab-quiz/server.py "<단어장.md 절대경로>"
   ```
   (경로 생략 시 `VOCAB_MD` env → 그것도 없으면 스킬의 기본 경로)
4. `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8765/` 로 200 확인
5. 사용자에게 **http://localhost:8765** 안내. 다 풀면 결과가 md에 자동 저장됨.

## 옵션 / 유지보수

- **포트 변경**: `VOCAB_PORT=8770 python3 server.py <md>`
- **기존 단어장 마이그레이션**(예전에 `<summary>` 안에 아이콘이 들어가 있던 경우 → 블록 아래로 이동):
  ```bash
  python3 ~/.claude/skills/vocab-quiz/server.py migrate "<단어장.md 절대경로>"
  ```
  멱등하므로 여러 번 돌려도 안전.
- **서버 종료**: `kill -9 $(ss -ltnp | grep ':8765 ' | grep -oP 'pid=\K[0-9]+')`
- 마킹 규칙은 `server.py`의 `ICONS`, `MASTER_STREAK`, `apply_icons()` 참고.

## 주의

- 서버가 md를 **직접 수정**함(아이콘 누적). git으로 추적되는 파일이면 변경 후 커밋 권장.
- 정답/문제 렌더링은 `marked.js` CDN 사용 → 첫 로드 시 인터넷 필요(수식 없는 단어장은 오프라인도 대체 렌더).
