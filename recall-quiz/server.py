#!/usr/bin/env python3
# recall-quiz 로컬 서버
# - questions.json(생성 파이프라인 산출물)을 서빙
# - /api/mark : 소스 노트 heading에 :repeat:/⭐ 결정론 write-back (LLM 개입 없음)
# - /api/chat : claude -p 서브프로세스로 문제별 라이브 튜터 (텍스트만, 파일 안 건드림)
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_QUESTIONS = os.path.join(HERE, "questions.json")
MARKS_PATH = os.path.join(HERE, "marks.json")
CHAT_LOG_PATH = os.path.join(HERE, "chat_log.json")
HTML_PATH = os.path.join(HERE, "quiz.html")

PORT = int(os.environ.get("RECALL_PORT", "8770"))
CHAT_MODEL = os.environ.get("RECALL_CHAT_MODEL", "sonnet")
CHAT_TIMEOUT = 90
CHAT_CWD = tempfile.mkdtemp(prefix="recall-quiz-chat-")

PERSONA = (
    "너는 Theo(한양대 AI 석박)의 복습 튜터다. 말투는 구어체 반말, 기술용어는 영어 유지. "
    "**노트 발췌에 있는 근거로만** 설명하고 노트 밖 사실을 지어내지 마라. 간결하게."
)


def resolve_questions_path(argv):
    """첫 인자 = questions.json 경로. 없으면 스킬 폴더 기본값."""
    if len(argv) > 1 and argv[1].strip():
        p = os.path.abspath(argv[1])
        if p.endswith(".md"):
            # 노트 경로를 잘못 넘긴 경우 — 소스 노트는 questions.json 안 source_note에서 읽음
            print(f"[recall-quiz] 경고: 첫 인자가 .md 파일({p}) — questions.json 경로를 기대함. "
                  f"기본 경로 사용: {DEFAULT_QUESTIONS}")
            return DEFAULT_QUESTIONS
        return p
    return DEFAULT_QUESTIONS


QUESTIONS_PATH = resolve_questions_path(sys.argv)

MARK_LOCK = threading.Lock()   # 소스 노트 편집 + marks.json 직렬화
CHAT_LOCK = threading.Lock()   # chat_log.json 직렬화

# 라인 끝 마커 클러스터: ":repeat:"/"⭐"가 공백 섞여 붙어있는 꼬리 전체
MARKER_TAIL_RE = re.compile(r"(?:\s*(?::repeat:|⭐))+\s*$")
MARKER_RE = re.compile(r":repeat:|⭐")
HEADING_RE = re.compile(r"^#{1,6}\s")
# fallback blockquote: "> :repeat: ⭐" (마커만 있는 인용 줄)
BQ_MARK_RE = re.compile(r"^>\s*(?:(?::repeat:|⭐)\s*)+$")


# ---------- 파일 IO ----------

def atomic_write(path, data):
    """임시파일 → os.replace. 실패 시 원본 무손상."""
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".recall-tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def load_json_file(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def load_questions():
    """questions.json을 매번 디스크에서 read. 없거나 깨지면 명확한 에러."""
    if not os.path.exists(QUESTIONS_PATH):
        raise ValueError(f"questions.json 없음: {QUESTIONS_PATH} — 생성 파이프라인을 먼저 실행할 것")
    try:
        with open(QUESTIONS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"questions.json 파싱 실패: {e}")
    if not isinstance(data, dict) or not isinstance(data.get("questions"), list):
        raise ValueError("questions.json 형식 오류: 최상위 dict에 questions 배열 필요")
    return data


def find_question(data, qid):
    for q in data.get("questions", []):
        if str(q.get("id")) == str(qid):
            if not q.get("source_note"):
                q = dict(q)
                q["source_note"] = data.get("source_note")
            return q
    raise ValueError(f"question_id를 questions.json에서 못 찾음: {qid}")


# ---------- 마킹 결정론 (§5) ----------

def slugify(text):
    """heading 텍스트 정규화: #·마커 제거, 소문자, 공백→-"""
    t = re.sub(r"^#{1,6}\s*", "", text)
    t = MARKER_RE.sub("", t)
    t = t.strip().lower()
    t = re.sub(r"\s+", "-", t)
    return t


def fence_flags(lines):
    """각 라인이 코드펜스 내부(여닫는 델리미터 포함)인지 True/False 리스트."""
    flags = []
    in_fence = False
    fence_ch = ""
    fence_len = 0
    for line in lines:
        s = line.lstrip()
        if not in_fence:
            m = re.match(r"(`{3,}|~{3,})", s)
            if m:
                in_fence = True
                fence_ch = m.group(1)[0]
                fence_len = len(m.group(1))
                flags.append(True)
            else:
                flags.append(False)
        else:
            flags.append(True)
            m = re.match(r"(`{3,}|~{3,})\s*$", s)
            if m and m.group(1)[0] == fence_ch and len(m.group(1)) >= fence_len:
                in_fence = False
    return flags


def markers_suffix(marks):
    """마커 set → 재부착 꼬리. :repeat: 먼저, ⭐ 나중, 공백 1개씩."""
    out = ""
    if "repeat" in marks:
        out += " :repeat:"
    if "star" in marks:
        out += " ⭐"
    return out


def _existing_marks(segment):
    return {"repeat" if t == ":repeat:" else "star" for t in MARKER_RE.findall(segment)}


def apply_mark(q, mark, on):
    """소스 노트에 마커 멱등 편집. 반환 (heading_line 1-indexed, 편집 후 라인)."""
    note_path = q.get("source_note") or ""
    if not note_path or not os.path.exists(note_path):
        raise ValueError(f"source_note 없음/미존재: {note_path!r}")
    with open(note_path, "r", encoding="utf-8") as f:
        text = f.read()
    trailing_nl = text.endswith("\n")
    lines = text.splitlines()
    flags = fence_flags(lines)
    anchor = q.get("note_anchor") or {}

    def commit():
        atomic_write(note_path, "\n".join(lines) + ("\n" if trailing_nl else ""))

    # 1) heading 매칭 (anchor_slug 정규화, 복수면 heading_line에 가장 가까운 것)
    target_slugs = set()
    if anchor.get("anchor_slug"):
        target_slugs.add(str(anchor["anchor_slug"]).strip().lower())
    if anchor.get("heading"):
        target_slugs.add(slugify(str(anchor["heading"])))
    hint = anchor.get("heading_line") or 0

    candidates = []
    if target_slugs:
        for idx, line in enumerate(lines):
            if flags[idx]:
                continue  # 코드펜스 내부의 heading-유사 라인 무시
            if not HEADING_RE.match(line):
                continue
            if slugify(line) in target_slugs:
                candidates.append(idx)

    if candidates:
        if isinstance(hint, int) and hint > 0:
            idx = min(candidates, key=lambda x: abs((x + 1) - hint))
        else:
            idx = candidates[0]
        line = lines[idx]
        m = MARKER_TAIL_RE.search(line)
        if m:
            marks = _existing_marks(line[m.start():])
            base = line[:m.start()].rstrip()
        else:
            marks = set()
            base = line.rstrip()
        if on:
            marks.add(mark)
        else:
            marks.discard(mark)
        new_line = base + markers_suffix(marks)
        if new_line != line:
            lines[idx] = new_line
            commit()
        return idx + 1, new_line

    # 2) fallback: line_start 바로 위 "> :repeat: ⭐" blockquote (멱등)
    ls = anchor.get("line_start")
    if not (isinstance(ls, int) and ls >= 1):
        raise ValueError(f"heading 매칭 실패 + line_start 없음 (qid={q.get('id')})")
    ins = min(max(ls - 1, 0), len(lines))
    # 코드펜스 밖 보장: 삽입 지점 라인이 펜스 내부면 펜스 위로 올림
    while 0 < ins < len(flags) and flags[ins]:
        ins -= 1

    # 인접 기존 마커 blockquote 스캔 (직전 삽입분 재사용 → 멱등)
    for j in (ins - 1, ins):
        if 0 <= j < len(lines) and not flags[j] and BQ_MARK_RE.match(lines[j].strip()):
            marks = _existing_marks(lines[j])
            if on:
                marks.add(mark)
            else:
                marks.discard(mark)
            if marks:
                new_line = ">" + markers_suffix(marks)
                if new_line != lines[j]:
                    lines[j] = new_line
                    commit()
                return j + 1, new_line
            del lines[j]  # 마커 0개 → 빈 blockquote 깔끔히 제거
            commit()
            return j + 1, ""

    if not on:
        return ls, ""  # 지울 마커가 애초에 없음 → no-op (멱등)
    new_line = ">" + markers_suffix({mark})
    lines.insert(ins, new_line)
    commit()
    return ins + 1, new_line


def handle_mark(payload):
    qid = payload.get("question_id")
    mark = payload.get("mark")
    on = bool(payload.get("on"))
    if mark not in ("repeat", "star"):
        raise ValueError(f"mark는 'repeat'|'star'만 가능: {mark!r}")
    data = load_questions()
    q = find_question(data, qid)
    with MARK_LOCK:
        heading_line, line = apply_mark(q, mark, on)
        audit = load_json_file(MARKS_PATH, [])
        if not isinstance(audit, list):
            audit = []
        audit.append({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "question_id": qid,
            "mark": mark,
            "on": on,
            "source_note": q.get("source_note"),
            "heading_line": heading_line,
            "line": line,
        })
        atomic_write(MARKS_PATH, json.dumps(audit, ensure_ascii=False, indent=2))
    return {"ok": True, "heading_line": heading_line, "line": line}


# ---------- 라이브 튜터 채팅 (§6) ----------

def run_claude(prompt):
    cmd = [
        "claude", "-p", prompt,
        "--model", CHAT_MODEL,
        "--output-format", "text",
        "--append-system-prompt", PERSONA,
        "--disallowedTools", "Bash", "Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch",
    ]
    proc = subprocess.run(cmd, shell=False, cwd=CHAT_CWD, timeout=CHAT_TIMEOUT,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[-500:]
        raise RuntimeError(f"claude -p 실패 (exit {proc.returncode}): {err}")
    return (proc.stdout or "").strip()


def note_excerpt(q, max_chars=8000):
    note_path = q.get("source_note") or ""
    if not note_path or not os.path.exists(note_path):
        return f"(소스 노트를 읽을 수 없음: {note_path})"
    with open(note_path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    anchor = q.get("note_anchor") or {}
    ls = anchor.get("line_start")
    le = anchor.get("line_end")
    hl = anchor.get("heading_line")
    if not (isinstance(ls, int) and ls >= 1):
        ls = hl if (isinstance(hl, int) and hl >= 1) else 1
    if not (isinstance(le, int) and le >= ls):
        le = ls + 40
    ls = max(1, min(ls, len(lines) or 1))
    le = max(ls, min(le, len(lines)))
    start = hl if (isinstance(hl, int) and 1 <= hl < ls) else ls  # heading 라인 포함
    excerpt = "\n".join(lines[start - 1:le])
    if len(excerpt) > max_chars:
        excerpt = excerpt[:max_chars] + "\n…(발췌 잘림)"
    return excerpt


def build_prompt(q, excerpt, thread, action, user_text):
    anchor = q.get("note_anchor") or {}
    parts = [
        f"[노트 발췌: {anchor.get('heading', '(heading 없음)')} / "
        f"line {anchor.get('line_start', '?')}..{anchor.get('line_end', '?')}]",
        excerpt,
        "",
        f"[문제] {q.get('question', '')}",
        f"[정답] {q.get('answer', '')}",
    ]
    if q.get("hint"):
        parts.append(f"[힌트] {q['hint']}")
    if thread:
        parts.append("")
        parts.append("[이 문제 이전 대화]")
        for t in thread:
            who = "학생" if t.get("role") == "user" else "튜터"
            parts.append(f"{who}: {t.get('text', '')}")
    parts.append("")
    if action == "explain":
        parts.append("[지시] 이 문제를 학생이 아직 이해 못 함. 노트 발췌 근거로 더 쉽고 자세히 다시 설명해줘.")
        if user_text:
            parts.append(f"[사용자 요청] {user_text}")
    elif action == "regen":
        orig = {k: q.get(k) for k in
                ("id", "mode", "type", "difficulty", "reveal_level",
                 "note_anchor", "source_note", "tags")}
        parts.append(
            "[지시] 같은 note_anchor·같은 difficulty로 문제 하나만 새로 만들어라. "
            "아래 스키마의 JSON 객체 1개만 출력해라 (코드펜스/설명/여는말 전부 금지). "
            "question은 기존 문제와 다르게, stem에 정답 누출 금지. "
            "id/mode/type/difficulty/reveal_level/note_anchor/source_note/tags는 아래 값 그대로, "
            "question/answer/hint만 새로 작성:"
        )
        parts.append(json.dumps(orig, ensure_ascii=False))
        if user_text:
            parts.append(f"[사용자 요청] {user_text}")
    else:  # freeform
        parts.append(f"[사용자 요청] {user_text}")
    return "\n".join(parts)


def parse_json_lenient(s):
    """코드펜스/잡설 벗겨내는 관용 JSON 파서."""
    s = (s or "").strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```\s*$", s, re.S)
    if m:
        s = m.group(1).strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    a = s.find("{")
    b = s.rfind("}")
    if a >= 0 and b > a:
        return json.loads(s[a:b + 1])
    raise ValueError("응답에서 JSON 객체를 찾지 못함")


def _append_chat(qid, entries):
    with CHAT_LOCK:
        log = load_json_file(CHAT_LOG_PATH, {})
        if not isinstance(log, dict):
            log = {}
        log.setdefault(str(qid), []).extend(entries)
        atomic_write(CHAT_LOG_PATH, json.dumps(log, ensure_ascii=False, indent=2))


def handle_chat(payload):
    qid = payload.get("question_id")
    action = payload.get("action") or "freeform"
    user_text = (payload.get("user_text") or "").strip()
    if action not in ("explain", "regen", "freeform"):
        return {"ok": False, "error": f"알 수 없는 action: {action}"}
    if action == "freeform" and not user_text:
        return {"ok": False, "error": "freeform은 user_text가 필요함"}

    data = load_questions()
    q = find_question(data, qid)
    excerpt = note_excerpt(q)
    log = load_json_file(CHAT_LOG_PATH, {})
    thread = log.get(str(qid), []) if isinstance(log, dict) else []
    prompt = build_prompt(q, excerpt, thread, action, user_text)

    if action == "regen":
        try:
            raw = run_claude(prompt)
            new_q = parse_json_lenient(raw)
        except (ValueError, json.JSONDecodeError):
            # 파싱 실패 → 1회 재시도, 또 실패하면 상위에서 {ok:false,error}
            retry = prompt + "\n\n주의: 방금 출력이 JSON 파싱에 실패했음. 반드시 JSON 객체 1개만, 코드펜스·설명 없이 출력."
            raw = run_claude(retry)
            new_q = parse_json_lenient(raw)
        if not isinstance(new_q, dict) or not new_q.get("question"):
            return {"ok": False, "error": "regen 응답에 question 필드 없음"}
        # 서버가 anchor·difficulty·식별자 강제 유지 (모델 출력 신뢰 안 함)
        for k in ("id", "mode", "type", "difficulty", "reveal_level",
                  "note_anchor", "source_note", "tags"):
            if q.get(k) is not None:
                new_q[k] = q.get(k)
        _append_chat(qid, [
            {"role": "user", "text": user_text or "(문제 바꿔 다시)", "action": action},
            {"role": "tutor", "text": f"[재생성된 문제] {new_q.get('question', '')}", "action": action},
        ])
        return {"ok": True, "new_question": new_q}

    reply = run_claude(prompt)
    if not reply:
        return {"ok": False, "error": "claude -p 응답이 비어 있음"}
    _append_chat(qid, [
        {"role": "user", "text": user_text or "(더 자세히 설명 요청)", "action": action},
        {"role": "tutor", "text": reply, "action": action},
    ])
    return {"ok": True, "reply": reply}


# ---------- HTTP ----------

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

    def _json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        return json.loads(raw)

    def do_GET(self):
        if self.path in ("/", "/index.html", "/quiz.html"):
            try:
                with open(HTML_PATH, "r", encoding="utf-8") as f:
                    self._send(200, f.read(), "text/html")
            except OSError as e:
                self._send(500, f"quiz.html 읽기 실패: {e}", "text/plain")
        elif self.path == "/api/questions":
            try:
                self._send(200, json.dumps(load_questions(), ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        try:
            payload = self._json_body()
        except Exception as e:
            self._send(400, json.dumps({"ok": False, "error": f"요청 JSON 파싱 실패: {e}"},
                                       ensure_ascii=False))
            return
        if self.path == "/api/mark":
            try:
                result = handle_mark(payload)
            except Exception as e:
                result = {"ok": False, "error": str(e)}
            self._send(200, json.dumps(result, ensure_ascii=False))
        elif self.path == "/api/chat":
            try:
                result = handle_chat(payload)
            except subprocess.TimeoutExpired:
                result = {"ok": False, "error": f"claude -p 타임아웃({CHAT_TIMEOUT}s)"}
            except Exception as e:
                result = {"ok": False, "error": str(e)}
            self._send(200, json.dumps(result, ensure_ascii=False))
        else:
            self._send(404, "not found", "text/plain")


if __name__ == "__main__":
    print(f"questions.json: {QUESTIONS_PATH}")
    try:
        info = load_questions()
        print(f"source_note: {info.get('source_note')} · mode: {info.get('mode')} "
              f"· 문제 {len(info.get('questions', []))}개")
    except ValueError as e:
        print(f"[recall-quiz] 경고: {e}")
    print(f"퀴즈 서버: http://localhost:{PORT} (chat model: {CHAT_MODEL})")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
