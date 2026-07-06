---
name: git-pull-push
description: 현재 리포를 pull하고, 변경사항 커밋하고, push까지 한 번에 처리. 충돌 발생 시 Opus 최대 5명 동원해서 자동 resolve.
version: 1.0.0
argument-hint: (없음)
allowed-tools: [Bash, Read, Edit, Agent]
---

현재 git 리포에서 pull → commit → push 시퀀스를 수행해.

## 수행 순서

### 1. 현재 상태 파악
- `git status`와 `git diff --stat`으로 변경사항 확인

### 2. 변경사항 커밋 (있을 때만)
- unstaged/untracked 파일이 있으면:
  - `git add -A`
  - 변경 내용 보고 적절한 커밋 메시지 작성 (한국어, 간결하게)
  - `git commit -m "..."`
- 변경사항 없으면 이 단계 스킵

### 3. Pull (rebase)
- `git pull --rebase` 실행
- 현재 변경점이 없어도 리모트에 있을 수 있으니 무조건 실행
- **충돌 발생 시** → 아래 충돌 해결 섹션으로

### 4. Push
- `git push`
- 리모트가 앞서 있어서 거부되면 → 3번으로 돌아가서 반복 (**최대 3회**)
- 3회 넘게 실패하거나 충돌 외 원인(인증, upstream 미설정 등)이면 반복하지 말고 원인 보고 후 중단

### 5. 완료 보고
- 최종 상태 한 줄로 요약

---

## 충돌 해결 규칙

충돌이 발생하면 **Opus 모델 에이전트를 최대 5개까지 동원**해서 resolve.

### 우선순위
1. **두 내용 공존** — 충돌하는 두 버전이 서로 다른 내용이면 둘 다 살림
2. **나중 것 우선** — 공존이 어려운 경우(같은 라인을 다르게 수정 등) rebase 기준으로 나중에 생긴 것(내 로컬 커밋) 채택

> rebase 중엔 marker 방향이 merge와 반대임 — `<<<<<<< HEAD` 쪽이 **리모트**, `=======` 아래(`>>>>>>>` 쪽)가 **내 로컬 커밋**. 헷갈리지 말 것.

### 충돌 해결 절차
1. 충돌 파일 Read로 읽어서 conflict marker 확인
2. 각 충돌 블록을 Opus 에이전트에 넘겨서 해결 텍스트 생성
3. Edit 툴로 conflict marker 제거 및 해결 내용 적용
4. `git add <해결된 파일>`
5. `GIT_EDITOR=true git rebase --continue` ← 에디터가 열리면 non-interactive 환경에서 멈추니까 반드시 `GIT_EDITOR=true` 붙일 것
6. 추가 충돌 있으면 반복 (최대 5 에이전트까지 병렬 활용)
7. 해결이 불가능하다고 판단되면 (바이너리 충돌 등) `git rebase --abort`로 원상복구 후 상황 보고하고 중단 ← 리포를 rebase 중간 상태로 방치하지 말 것

### Opus 에이전트 프롬프트 형식
충돌 블록을 넘길 때 아래 형식으로:
```
아래 git conflict를 해결해줘.
규칙: 두 내용 공존 우선, 공존 불가 시 ======= 아래(로컬) 것 우선.
conflict marker 없이 해결된 텍스트만 출력.

[충돌 내용 붙여넣기]
```

---

## 출력

- 각 단계별 진행 상황을 간결하게 출력
- 충돌 해결 시 어떤 방식으로 resolve했는지 한 줄 보고
- 최종 push 성공 여부 확인
