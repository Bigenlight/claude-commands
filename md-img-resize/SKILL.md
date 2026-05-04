---
name: md-img-resize
description: 마크다운 이미지 태그의 width를 실제 해상도 기반으로 자동 조정
version: 1.0.0
argument-hint: [마크다운 파일 경로] (생략 시 현재 대화에서 언급된 파일 사용)
allowed-tools: [Read, Write, Edit, Bash]
---

# md-img-resize 스킬

마크다운 파일 속 이미지 태그의 width를 이미지 실제 해상도 기반으로 자동 조정한다.

## 실행 절차

### 1. 대상 파일 결정

- argument로 파일 경로가 주어지면 그 파일을 사용
- argument가 없으면 현재 대화에서 언급된 마크다운 파일을 사용
- 둘 다 없으면 사용자에게 파일 경로를 물어볼 것

### 2. 파일 읽기

Read 툴로 대상 마크다운 파일을 읽는다.

### 3. 이미지 경로 수집

파일 내에서 아래 두 가지 패턴을 모두 찾는다:

- `![alt text](경로)` — 마크다운 이미지 문법
- `<img src="경로"` — HTML img 태그

### 4. 이미지 base path 결정

- 이미지 경로가 `/`로 시작하는 절대경로이면 → **현재 워킹 디렉토리(cwd)를 앞에 붙여서** 실제 파일 경로를 만든다
- 상대경로이면 → **마크다운 파일이 위치한 디렉토리 기준**으로 해석한다

### 5. Python으로 dimension 측정

Bash 툴로 Python 스크립트를 실행하여 각 이미지의 해상도를 측정한다.
PIL이 없으면 `pip install Pillow`를 먼저 실행한다.

```python
from PIL import Image
img = Image.open("실제_파일_경로")
w, h = img.size
ratio = w / h
new_width = max(380, min(int(ratio * 420), 860))
print(f"{w}x{h} ratio={ratio:.2f} new_width={new_width}")
```

**width 계산 공식:**
```
ratio = image_width / image_height
width = max(380, min(int(ratio * 420), 860))
```

### 6. 파일 업데이트

Edit 툴로 계산된 width를 적용한다:

- `![alt text](경로)` → `<img src="경로" width="계산값">` 으로 변환
- `<img src="경로" width="기존값">` → width 속성 값만 계산값으로 교체
- **이미지 태그 외 나머지 내용은 절대 변경하지 말 것**

### 7. 결과 출력

변경된 이미지 목록을 표로 출력한다:

| 파일명 | 해상도 | ratio | 적용 width |
|--------|--------|-------|-----------|
| example.png | 1920x1080 | 1.78 | 747 |

변경 없는 이미지가 있으면 (파일 미존재 등) 사유도 함께 출력한다.
