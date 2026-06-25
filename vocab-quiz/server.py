#!/usr/bin/env python3
# 영어 단어/개념 퀴즈 로컬 서버
# - md를 직접 파싱해서 퀴즈 항목 제공 (연속 3✅ 졸업 항목은 제외)
# - 결과 받으면 각 <details> 블록 "아래 줄"에 아이콘을 누적 표시
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MD = os.path.expanduser(
    "~/For-Neural-Network-Improvement-Private-/02-areas/english-vocab-and-concepts.md"
)


def resolve_md(argv):
    """'migrate' 외의 첫 인자 → md 경로. 없으면 env VOCAB_MD, 없으면 기본값."""
    cand = [a for a in argv[1:] if a != "migrate"]
    if cand:
        return os.path.abspath(cand[0])
    return os.path.abspath(os.environ.get("VOCAB_MD", DEFAULT_MD))


MD_PATH = resolve_md(sys.argv)
RESULTS_PATH = os.path.join(HERE, "results.json")
FEEDBACK_PATH = os.path.join(HERE, "feedback.json")
PORT = int(os.environ.get("VOCAB_PORT", "8765"))

ICONS = {"known": "✅", "review": "🔁", "wrong": "❌"}
ICON_RE = r"(?:✅|🔁|❌)"        # 퀴즈 결과 아이콘
GRAD = "💯"                      # 연속 3✅ 졸업 표시
MARK_RE = r"(?:✅|🔁|❌|💯)"     # 아래 줄에 올 수 있는 모든 마크
MASTER_STREAK = 3  # 연속 ✅ 이만큼이면 졸업(💯 부여 + 출제 제외)

# 블록 + (블록 바로 아래의 아이콘 누적 줄, optional). CRLF 내성 위해 \r?\n.
BLOCK_MARK_RE = re.compile(
    r"(<details>.*?</details>)"
    r"((?:\r?\n[ \t]*" + MARK_RE + r"(?:[ \t]+" + MARK_RE + r")*[ \t]*)?)",
    re.S,
)


def read_md():
    with open(MD_PATH, "r", encoding="utf-8") as f:
        return f.read()


def write_md(text):
    with open(MD_PATH, "w", encoding="utf-8") as f:
        f.write(text)


def _section_of(text, pos):
    concept_h = text.find("## 주목할만한 개념")
    vocab_h = text.find("## 영어 단어 및 표현")
    if vocab_h < 0:
        vocab_h = len(text)
    return "concept" if (concept_h <= pos < vocab_h) else "vocab"


def parse_items(text):
    """퀴즈용 항목 리스트. 연속 3✅ 졸업 항목은 제외하되 id(=블록 순번)는 유지."""
    items = []
    for idx, m in enumerate(BLOCK_MARK_RE.finditer(text)):
        block = m.group(1)
        marker = m.group(2) or ""
        inner_m = re.search(r"<details>(.*)</details>", block, re.S)
        if not inner_m:
            continue
        inner = inner_m.group(1)
        sm = re.search(r"<summary>(.*?)</summary>", inner, re.S)
        if not sm:
            continue
        summary = re.sub(r"^\s*" + ICON_RE + r"\s*", "", sm.group(1).strip())
        body = inner[sm.end():].strip()
        icons = re.findall(ICON_RE, marker)
        mastered = (GRAD in marker) or (
            len(icons) >= MASTER_STREAK and all(x == "✅" for x in icons[-MASTER_STREAK:])
        )
        if mastered:
            continue
        items.append({
            "id": idx,
            "summary": summary,
            "body": body,
            "section": _section_of(text, m.start()),
            "history": "".join(icons),
        })
    return items


def block_index(text):
    """id(블록 순번) -> {summary, body, section}. 졸업 여부 무관하게 전체."""
    out = {}
    for idx, m in enumerate(BLOCK_MARK_RE.finditer(text)):
        inner_m = re.search(r"<details>(.*)</details>", m.group(1), re.S)
        if not inner_m:
            continue
        inner = inner_m.group(1)
        sm = re.search(r"<summary>(.*?)</summary>", inner, re.S)
        if not sm:
            continue
        out[str(idx)] = {
            "summary": re.sub(r"^\s*" + ICON_RE + r"\s*", "", sm.group(1).strip()),
            "body": inner[sm.end():].strip(),
            "section": _section_of(text, m.start()),
        }
    return out


def apply_icons(statuses):
    """각 블록 아래 줄에 아이콘 누적 추가. statuses: {str(id): status}."""
    text = read_md()
    counter = {"i": -1}

    def repl(m):
        counter["i"] += 1
        idx = counter["i"]
        block = m.group(1)
        marker = m.group(2) or ""
        st = statuses.get(str(idx))
        icon = ICONS.get(st) if st else None
        if not icon:
            return m.group(0)
        line = (marker.rstrip() + " " + icon) if marker.strip() else ("\n" + icon)
        # 연속 3✅ 달성 & 아직 졸업 안 했으면 💯 부여
        icons = re.findall(ICON_RE, line)
        if GRAD not in line and len(icons) >= MASTER_STREAK and all(
            x == "✅" for x in icons[-MASTER_STREAK:]
        ):
            line = line + " " + GRAD
        return block + line + " "

    write_md(BLOCK_MARK_RE.sub(repl, text))


def migrate():
    """기존 <summary> 안 아이콘 → 블록 아래 줄로 이동/통일 (1회용, 멱등)."""
    text = read_md()

    def repl(m):
        block = m.group(1)
        marker = m.group(2) or ""
        existing = re.findall(ICON_RE, marker)
        sm = re.search(r"(<summary>)(.*?)(</summary>)", block, re.S)
        if sm:
            inner = sm.group(2)
            lead = re.match(r"\s*(" + ICON_RE + r")\s*", inner)
            if lead:
                existing.append(lead.group(1))
                cleaned = inner[lead.end():]
                block = block[:sm.start()] + sm.group(1) + cleaned + sm.group(3) + block[sm.end():]
        if existing:
            return block + "\n" + " ".join(existing) + " "
        return block

    write_md(BLOCK_MARK_RE.sub(repl, text))


HTML_PATH = os.path.join(HERE, "quiz.html")


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path in ("/", "/index.html", "/quiz.html"):
            with open(HTML_PATH, "r", encoding="utf-8") as f:
                self._send(200, f.read(), "text/html")
        elif self.path == "/api/items":
            self._send(200, json.dumps(parse_items(read_md()), ensure_ascii=False))
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        if self.path == "/api/save":
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            statuses = payload.get("statuses", {})
            feedback = payload.get("feedback", {})
            with open(RESULTS_PATH, "w", encoding="utf-8") as f:
                json.dump(statuses, f, ensure_ascii=False, indent=2)
            # 피드백을 단어 정보와 함께 기록 (Claude가 나중에 일괄 반영)
            idx = block_index(read_md())
            fb_list = []
            for k, v in feedback.items():
                v = (v or "").strip()
                if not v:
                    continue
                info = idx.get(str(k), {})
                fb_list.append({
                    "id": int(k),
                    "summary": info.get("summary", ""),
                    "section": info.get("section", ""),
                    "body": info.get("body", ""),
                    "feedback": v,
                })
            with open(FEEDBACK_PATH, "w", encoding="utf-8") as f:
                json.dump(fb_list, f, ensure_ascii=False, indent=2)
            apply_icons(statuses)
            counts = {"known": 0, "review": 0, "wrong": 0}
            for v in statuses.values():
                if v in counts:
                    counts[v] += 1
            self._send(200, json.dumps(
                {"ok": True, "counts": counts, "feedback": len(fb_list)},
                ensure_ascii=False))
        else:
            self._send(404, "not found", "text/plain")


if __name__ == "__main__":
    print(f"단어장: {MD_PATH}")
    if "migrate" in sys.argv[1:]:
        migrate()
        print("migrate 완료")
    else:
        print(f"퀴즈 서버: http://localhost:{PORT}")
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
