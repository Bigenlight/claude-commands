#!/usr/bin/env python3
# 수식 퀴즈 로컬 서버 (formula-quiz)
# - 덱 폴더(02-areas/formula-deck/)의 formula-*.md = 모집단 1개씩
# - 한 세션 = 모집단 1개에서 최대 N개(기본 5) 출제
# - 결과는 각 카드 </details> 바로 아래 `기록:` 줄에 ✅/❌+날짜로 누적 (연속 3✅ → 💯 졸업)
# - 저장 끝나면 서버는 스스로 종료 (스킬 1회 = 세션 1회)
# - 기호 연습: 덱 폴더 밖 vocab-quiz 형식 노트(SYMBOL_NOTES, 예: 그리스 문자)도 모집단으로 띄움 → 랜덤 출제,
#   기록은 vocab-quiz와 같은 아이콘 줄(둘이 공유), 정답 끝에 다른 덱에서 그 기호가 쓰인 곳을 자동으로 붙임
import datetime
import json
import os
import random
import re
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(HERE, "quiz.html")
ANNO_PATH = os.path.join(HERE, "annotate.js")
LAST_SESSION_PATH = os.path.join(HERE, "last_session.json")
FEEDBACK_PATH = os.path.join(HERE, "feedback.json")

# 리포 위치가 PC마다 다름 → 존재하는 후보를 순서대로 탐색
_DEFAULT_CANDIDATES = [
    "~/For-Neural-Network-Improvement-Private-/02-areas/formula-deck",
    "~/OneDrive/Desktop/For-Neural-Network-Improvement-Private-/02-areas/formula-deck",
]

PORT = int(os.environ.get("FORMULA_PORT", "8766"))
SESSION_MAX = int(os.environ.get("FORMULA_MAX", "5"))   # 한 세션 최대 카드 수
MASTER_STREAK = 3                                        # 연속 ✅ 이만큼이면 💯 졸업
KEEP_ALIVE = os.environ.get("FORMULA_KEEP_ALIVE", "") not in ("", "0")

MARK_RE = re.compile(r"(✅|❌)(\d{4}-\d{2}-\d{2})")
GRAD = "💯"
CARD_MARK_RE = re.compile(r"^<!--\s*formula:\s*([A-Za-z0-9][\w-]*)\s*-->[ \t]*$", re.M)
HINT_RE = re.compile(r"^>\s*💡\s*\*\*힌트\*\*\s*[:：]\s*(.+?)\s*$", re.M)
TITLE_RE = re.compile(r"^##\s+(.+?)\s*$", re.M)
DETAILS_RE = re.compile(r"<details>\s*<summary>(.*?)</summary>(.*?)</details>", re.S)
RECORD_RE = re.compile(r"\A[ \t]*\r?\n기록:[^\n]*")


# ---------------------------------------------------------------- 경로/인자

def parse_args(argv):
    args = {"cmd": "serve", "dir": None, "pop": None}
    rest = argv[1:]
    if rest and rest[0] in ("serve", "check", "stats", "readme"):
        args["cmd"] = rest.pop(0)
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--pop" and i + 1 < len(rest):
            args["pop"] = rest[i + 1]
            i += 2
            continue
        if args["dir"] is None:
            args["dir"] = a
        i += 1
    return args


def resolve_deck_dir(cli_dir):
    if cli_dir:
        return os.path.abspath(os.path.expanduser(cli_dir))
    env = os.environ.get("FORMULA_DECK_DIR")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    for c in _DEFAULT_CANDIDATES:
        p = os.path.expanduser(c)
        if os.path.isdir(p):
            return p
    return os.path.expanduser(_DEFAULT_CANDIDATES[0])


def find_repo_root(start):
    cur = start
    while True:
        if os.path.isdir(os.path.join(cur, ".git")) or os.path.isfile(os.path.join(cur, "CLAUDE.md")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return os.path.dirname(start)
        cur = parent


# ---------------------------------------------------------------- 파싱

def read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def frontmatter(text):
    m = re.match(r"\A---\r?\n(.*?)\r?\n---", text, re.S)
    out = {}
    if not m:
        return out
    for line in m.group(1).splitlines():
        kv = re.match(r"^([\w-]+):\s*(.*?)\s*(?:#.*)?$", line)
        if kv:
            out[kv.group(1)] = kv.group(2)
    return out


def deck_config(fm):
    cfg = {"batch": SESSION_MAX, "graduate": True, "order": "file", "mode": "formula"}
    if fm.get("quiz-batch", "").isdigit() and int(fm["quiz-batch"]) > 0:
        cfg["batch"] = int(fm["quiz-batch"])
    if fm.get("quiz-graduate", "").lower() in ("false", "no", "0"):
        cfg["graduate"] = False
    return cfg


def parse_history(record_line):
    marks = [(icon, date) for icon, date in MARK_RE.findall(record_line or "")]
    return marks, (GRAD in (record_line or ""))


def card_state(marks, grad_flag, graduate):
    streak = 0
    for icon, _ in reversed(marks):
        if icon != "✅":
            break
        streak += 1
    mastered = graduate and (grad_flag or streak >= MASTER_STREAK)
    last = marks[-1] if marks else None
    return {
        "seen": len(marks),
        "last_icon": last[0] if last else None,
        "last_date": last[1] if last else None,
        "streak": streak,
        "mastered": mastered,
    }


def parse_deck(path):
    """덱 md → {slug, population, cards[...], errors[...]}"""
    if not os.path.basename(path).startswith("formula-"):
        return parse_symbol_note(path)
    text = read(path)
    fm = frontmatter(text)
    cfg = deck_config(fm)
    slug = os.path.splitext(os.path.basename(path))[0]
    title_m = re.search(r"^#\s+(.+?)\s*$", text, re.M)
    order = fm.get("order", "")
    deck = {
        "slug": slug,
        "file": path,
        "population": fm.get("population") or (title_m.group(1) if title_m else slug),
        "group": fm.get("group") or "기타",
        "order": int(order) if order.isdigit() else 999,
        "config": cfg,
        "cards": [],
        "errors": [],
    }
    marks = list(CARD_MARK_RE.finditer(text))
    seen_ids = set()
    for i, m in enumerate(marks):
        cid = m.group(1)
        start = m.end()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        chunk = text[start:end]
        err = []
        if cid in seen_ids:
            err.append("중복 id")
        seen_ids.add(cid)
        t = TITLE_RE.search(chunk)
        h = HINT_RE.search(chunk)
        d = DETAILS_RE.search(chunk)
        if not t:
            err.append("제목(## ...) 없음")
        if not h:
            err.append("힌트(> 💡 **힌트**: ...) 없음")
        if not d:
            err.append("<details> 정답 블록 없음")
        if d and "<details>" in d.group(2):
            err.append("<details> 중첩")
        question = ""
        if h and d and h.end() < d.start():
            question = chunk[h.end():d.start()].strip()
        elif h and d:
            err.append("힌트가 <details> 뒤에 있음")
        if h and d and not question:
            err.append("문제 수식 없음 (힌트와 <details> 사이)")
        record = ""
        if d:
            rm = RECORD_RE.match(chunk[d.end():])
            record = rm.group(0).strip() if rm else ""
        hist, grad_flag = parse_history(record)
        if err:
            deck["errors"].append({"id": cid, "errors": err})
            continue
        deck["cards"].append({
            "id": cid,
            "title": t.group(1),
            "hint": h.group(1),
            "question": question,
            "answer": d.group(2).strip(),
            "history": [{"icon": a, "date": b} for a, b in hist],
            "state": card_state(hist, grad_flag, cfg["graduate"]),
            "order": i,
        })
    return deck


# ---------------------------------------------------------------- 기호 연습 노트 (vocab-quiz 형식)
# `<details><summary>기호</summary>본문</details>` + 바로 아래 아이콘 누적 줄(`✅ ❌ `, 날짜 없음).
# vocab-quiz/server.py의 BLOCK_MARK_RE·apply_icons와 **똑같이** 맞춰야 두 퀴즈가 기록을 공유함 (id = 블록 순번)

SYMBOL_NOTES = ["../greek-symbols-dl.md"]   # 덱 폴더 기준 상대경로
V_MARK = r"(?:✅|🔁|❌|💯)"
V_ICON = r"(?:✅|🔁|❌)"
V_BLOCK_RE = re.compile(
    r"(<details>.*?</details>)"
    r"((?:\r?\n[ \t]*" + V_MARK + r"(?:[ \t]+" + V_MARK + r")*[ \t]*)?)",
    re.S,
)


def parse_symbol_note(path):
    text = read(path)
    fm = frontmatter(text)
    title_m = re.search(r"^#\s+(.+?)\s*$", text, re.M)
    slug = os.path.splitext(os.path.basename(path))[0]
    graduate = fm.get("quiz-graduate", "").lower() not in ("false", "no", "0")
    deck = {
        "slug": slug,
        "file": path,
        "population": (title_m.group(1).split(" — ")[0] if title_m else slug),
        "group": "기호 연습",
        "order": 0,
        # 노트의 quiz-batch는 vocab-quiz용 → 여기선 수식 퀴즈 기본 개수(5)
        "config": {"batch": SESSION_MAX, "graduate": graduate, "order": "random", "mode": "symbol"},
        "cards": [],
        "errors": [],
    }
    for idx, m in enumerate(V_BLOCK_RE.finditer(text)):
        inner = re.search(r"<details>(.*)</details>", m.group(1), re.S)
        sm = inner and re.search(r"<summary>(.*?)</summary>", inner.group(1), re.S)
        if not sm:
            continue
        summary = re.sub(r"^\s*" + V_ICON + r"\s*", "", sm.group(1).strip())
        body = inner.group(1)[sm.end():].strip()
        glyph = re.sub(r"<[^>]+>", "", summary).strip()
        head = body.splitlines()[0] if body else ""
        nm = re.match(r"\*\*(.+?)\*\*\s*(?:\(([^)]*)\))?", head)
        title = glyph
        if nm:
            reading = re.split(r"\s*[,/←]\s*", nm.group(2) or "")[0].strip()
            title += f" — {nm.group(1)}" + (f" ({reading})" if reading else "")
        latex = head.split("LaTeX", 1)[1] if "LaTeX" in head else ""
        cmds = []
        for code in re.findall(r"`([^`]+)`", latex):
            cmds += [c for c in re.findall(r"\\[A-Za-z]+", code) if c not in cmds]
        marker = m.group(2) or ""
        icons = re.findall(V_ICON, marker)
        hist = [(i, None) for i in icons]
        deck["cards"].append({
            "id": f"b{idx}",
            "title": title,
            "hint": "",
            "question": summary,
            "answer": body,
            "cmds": cmds,
            "history": [{"icon": i, "date": None} for i in icons],
            "state": card_state(hist, GRAD in marker, graduate),
            "order": idx,
        })
    return deck


def apply_symbol_results(path, results, graduate):
    """vocab-quiz apply_icons와 같은 방식으로 블록 아래 아이콘 줄에 누적. results: {'b<순번>': 'right'|'wrong'}"""
    text = read(path)
    counter = {"i": -1, "n": 0}

    def repl(m):
        counter["i"] += 1
        r = results.get(f"b{counter['i']}")
        if r not in ("right", "wrong"):
            return m.group(0)
        icon = "✅" if r == "right" else "❌"
        marker = m.group(2) or ""
        line = (marker.rstrip() + " " + icon) if marker.strip() else ("\n" + icon)
        icons = re.findall(V_ICON, line)
        if graduate and GRAD not in line and len(icons) >= MASTER_STREAK and all(
            x == "✅" for x in icons[-MASTER_STREAK:]
        ):
            line += " " + GRAD
        counter["n"] += 1
        return m.group(1) + line + " "

    write_atomic(path, V_BLOCK_RE.sub(repl, text))
    return counter["n"]


def deck_files(deck_dir):
    if not os.path.isdir(deck_dir):
        return []
    files = sorted(
        os.path.join(deck_dir, f) for f in os.listdir(deck_dir)
        if f.startswith("formula-") and f.endswith(".md")
    )
    return files + [p for p in (os.path.normpath(os.path.join(deck_dir, s)) for s in SYMBOL_NOTES)
                    if os.path.isfile(p)]


def deck_path(deck_dir, slug):
    """모집단 slug → 파일 경로 (덱 폴더 + 기호 노트 중에서만, 없으면 None)"""
    return next((p for p in deck_files(deck_dir)
                 if os.path.splitext(os.path.basename(p))[0] == slug), None)


def load_decks(deck_dir):
    return [parse_deck(p) for p in deck_files(deck_dir)]


# ---------------------------------------------------------------- 출제 로직

def _bucket(card):
    st = card["state"]
    if st["last_icon"] == "❌":
        return 0
    if st["seen"] == 0:
        return 1
    return 2


def select_cards(deck):
    """우선순위로 고르고(❌ 최근 → 미학습 → ✅ 오래된 순), 출제는 파일 순서대로.
    기호 연습 노트(order random)는 💯 아닌 카드 중 무작위로 뽑고 순서도 무작위."""
    pool = [c for c in deck["cards"] if not c["state"]["mastered"]]
    if deck["config"]["order"] == "random":
        return random.sample(pool, min(deck["config"]["batch"], len(pool)))
    pool.sort(key=lambda c: (_bucket(c), c["state"]["last_date"] or "", c["order"]))
    picked = pool[: deck["config"]["batch"]]
    picked.sort(key=lambda c: c["order"])
    return picked


def deck_stats(deck, today):
    cards = deck["cards"]
    dates = [c["state"]["last_date"] for c in cards if c["state"]["last_date"]]
    return {
        "slug": deck["slug"],
        "population": deck["population"],
        "group": deck["group"],
        "order": deck["order"],
        "total": len(cards),
        "unseen": sum(1 for c in cards if c["state"]["seen"] == 0),
        "wrong": sum(1 for c in cards if c["state"]["last_icon"] == "❌" and not c["state"]["mastered"]),
        "right": sum(1 for c in cards if c["state"]["last_icon"] == "✅" and not c["state"]["mastered"]),
        "mastered": sum(1 for c in cards if c["state"]["mastered"]),
        "eligible": sum(1 for c in cards if not c["state"]["mastered"]),
        "last_studied": max(dates) if dates else None,
        "today": bool(dates) and max(dates) == today,
        "batch": deck["config"]["batch"],
        "random": deck["config"]["order"] == "random",
        "mode": deck["config"]["mode"],
        "errors": len(deck["errors"]),
    }


GROUP_ORDER = ["기호 연습", "DL 기초", "확률·정보이론", "아키텍처", "Transformer", "생성 모델", "강화학습"]


def curriculum_rank(s):
    """첫 화면 정렬 + 처음 시작할 때 추천 순서: 그룹 순서 → frontmatter order → 파일명."""
    g = GROUP_ORDER.index(s["group"]) if s["group"] in GROUP_ORDER else len(GROUP_ORDER)
    return (g, s["order"], s["slug"])


def recommend(stats):
    """출제할 게 있는 모집단 중: (안 본·틀린 게 있는 쪽 우선) → 가장 오래 안 본 순. 기호 연습 덱은 곁다리라 추천 X."""
    cand = [s for s in stats if s["eligible"] > 0 and s["mode"] != "symbol"]
    if not cand:
        return None
    cand.sort(key=lambda s: (0 if (s["unseen"] + s["wrong"]) > 0 else 1,
                             s["last_studied"] or "", curriculum_rank(s)))
    return cand[0]["slug"]


# ---------------------------------------------------------------- 기호 연습: 다른 덱에서 쓰인 곳

SYM_BULLET_RE = re.compile(r"^[-*] (\$.*?)\s*←\s*(.*)$")
SYM_HEAD_RE = re.compile(r"\$[^$]+\$(\s*(,|/|·|와)\s*\$[^$]+\$)*")
USES_MAX = 8


def symbol_bullets(answer):
    """정답의 **기호 풀이** 아래 `- $기호$ ← 설명` bullet → [(head, text)] (annotate.js parseBullets와 같은 범위)"""
    out, on = [], False
    for line in answer.splitlines():
        if not on:
            on = line.strip().startswith("**기호 풀이**")
            continue
        if re.match(r"^\*\*[^*]+\*\*", line) or line.startswith("</details>") or line.startswith("```"):
            break
        m = SYM_BULLET_RE.match(line)
        if m and SYM_HEAD_RE.fullmatch(m.group(1).strip()):
            out.append((m.group(1).strip(), m.group(2).strip()))
    return out


def first_phrase(text, cap=46):
    """bullet 설명의 첫 구절 (마침표·; · — · ← 앞). 자르는 위치는 $수식$ 바깥에서만"""
    mask = re.sub(r"\$[^$]*\$", lambda m: "□" * len(m.group(0)), text)
    m = re.search(r"\.(?=\s|$)|;| — |←", mask)   # 0.01 같은 소수점은 안 자름
    if m and m.start() > 0:
        text, mask = text[:m.start()], mask[:m.start()]
    text = text.strip()
    if len(text) > cap:
        cut = max((i for i, ch in enumerate(mask[:cap]) if ch == " "), default=-1)
        if cut > 8:
            text = text[:cut].rstrip(" ,") + "…"
    for mark in ("**", "`"):
        if text.count(mark) % 2:
            text += mark
    return text


def symbol_uses(card, decks):
    """기호 카드의 기호(기호 풀이 bullet 머리의 LaTeX 명령)가 다른 덱 카드의 기호 풀이에 나온 곳 → 정답 끝에 붙일 md"""
    cmds = list(card.get("cmds") or [])
    for head, _ in symbol_bullets(card["answer"]):
        for c in re.findall(r"\\[A-Za-z]+", head):
            if c not in cmds:
                cmds.append(c)
    if not cmds:
        return ""
    pats = [(c, re.compile(re.escape(c) + r"(?![A-Za-z])")) for c in cmds]
    per = {c: {} for c in cmds}   # 기호 → {덱 slug: [줄...]} (덱 커리큘럼 순)
    hit_cards, total, seen = set(), 0, set()
    for d in decks:
        if d["config"]["mode"] == "symbol":
            continue
        for c2 in d["cards"]:
            total += 1
            for head, text in symbol_bullets(c2["answer"]):
                c = next((c for c, p in pats if p.search(head)), None)
                if not c:
                    continue
                hit_cards.add((d["slug"], c2["id"]))
                label = first_phrase(text)
                if (head, label) in seen:
                    continue
                seen.add((head, label))
                line = f"- {head} ← {label} <sub>— {c2['title']} · [[{d['slug']}|{d['population']}]]</sub>"
                per[c].setdefault(d["slug"], []).append(line)
    if not hit_cards:
        return f"**이 덱 수식에서 쓰인 곳** ← 아직 없음 (지금 수식 카드 {total}장엔 안 나옴)"
    # 기호마다 덱을 돌아가며 하나씩 (한 덱이 독차지 X), 기호가 여럿이면 몫을 나눔
    live = [c for c in cmds if per[c]]
    share = max(2, USES_MAX // len(live))
    lines = []
    for c in live:
        buckets, got = list(per[c].values()), []
        while len(got) < share and any(buckets):
            for b in buckets:
                if b and len(got) < share:
                    got.append(b.pop(0))
        lines += got
    n_all = len(seen)
    more = f" · 그중 {len(lines)}개만 보여줌" if n_all > len(lines) else ""
    head = f"**이 덱 수식에서 쓰인 곳** ← 수식 카드 {total}장 중 {len(hit_cards)}장에 나옴 (자동 수집{more})"
    return head + "\n\n" + "\n".join(lines)


# ---------------------------------------------------------------- wikilink → vscode 링크

class NoteIndex:
    def __init__(self, root):
        self.root = root
        self.map = {}
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in dns if not d.startswith(".") and d != "node_modules"]
            for fn in fns:
                if fn.endswith(".md"):
                    self.map.setdefault(fn[:-3].lower(), os.path.join(dp, fn))

    def link(self, m):
        target, alias = m.group(1), m.group(2)
        name = target.split("#")[0].strip()
        if name.lower().endswith(".md"):
            name = name[:-3]
        label = (alias or target).strip()
        path = self.map.get(name.lower())
        if not path:
            return f"**{label}**"
        url = "vscode://file" + urllib.parse.quote(path, safe="/")
        return f"[📄 {label}]({url})"

    def convert(self, md):
        return re.sub(r"\[\[([^\]|]+?)(?:\|([^\]]+))?\]\]", self.link, md)


# ---------------------------------------------------------------- 기록 쓰기

def apply_results(path, results, today, graduate):
    """results: {card_id: 'right'|'wrong'} → 해당 카드 `기록:` 줄에 누적."""
    text = read(path)
    marks = list(CARD_MARK_RE.finditer(text))
    edits = []  # (start, end, replacement) — 뒤에서부터 적용
    for i, m in enumerate(marks):
        cid = m.group(1)
        if cid not in results:
            continue
        icon = "✅" if results[cid] == "right" else "❌"
        start = m.end()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        chunk = text[start:end]
        d = DETAILS_RE.search(chunk)
        if not d:
            continue
        abs_after = start + d.end()
        rm = RECORD_RE.match(text[abs_after:end])
        if rm:
            old = rm.group(0).strip()
            line = old + " " + icon + today
            seg_end = abs_after + rm.end()
        else:
            line = "기록: " + icon + today
            seg_end = abs_after
        hist, grad_flag = parse_history(line)
        st = card_state(hist, grad_flag, graduate)
        if graduate and st["streak"] >= MASTER_STREAK and GRAD not in line:
            line += " " + GRAD
        edits.append((abs_after, seg_end, "\n" + line))
    for s, e, rep in sorted(edits, reverse=True):
        text = text[:s] + rep + text[e:]
    write_atomic(path, text)
    return len(edits)


# ---------------------------------------------------------------- HTTP

def make_handler(deck_dir, notes, preselect, server_ref):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="application/json"):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False))

        def _body(self):
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")

        def log_message(self, *a):
            pass

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(url.query)
            today = datetime.date.today().isoformat()
            if url.path in ("/", "/index.html"):
                self._send(200, read(HTML_PATH), "text/html")
            elif url.path == "/annotate.js":
                self._send(200, read(ANNO_PATH), "application/javascript")
            elif url.path == "/api/pops":
                stats = sorted((deck_stats(d, today) for d in load_decks(deck_dir)), key=curriculum_rank)
                self._json({
                    "pops": stats,
                    "groups": GROUP_ORDER,
                    "recommended": recommend(stats),
                    "preselect": preselect,
                    "today": today,
                })
            elif url.path == "/api/session":
                slug = (q.get("pop") or [""])[0]
                path = deck_path(deck_dir, slug) if slug else None
                if not path:
                    return self._json({"error": "모집단 없음"}, 404)
                deck = parse_deck(path)
                mode = deck["config"]["mode"]
                others = (sorted(load_decks(deck_dir), key=lambda k: curriculum_rank(deck_stats(k, today)))
                          if mode == "symbol" else [])
                cards = []
                for c in select_cards(deck):
                    answer = c["answer"]
                    if mode == "symbol":
                        uses = symbol_uses(c, others)
                        if uses:
                            answer += "\n\n" + uses
                    cards.append({
                        "id": c["id"],
                        "title": c["title"],
                        "hint": c["hint"],
                        "question": c["question"],
                        "answer": notes.convert(answer),
                        "history": c["history"],
                        "state": c["state"],
                    })
                self._json({"slug": slug, "population": deck["population"], "mode": mode, "cards": cards})
            else:
                self._send(404, "not found", "text/plain")

        def do_POST(self):
            url = urllib.parse.urlparse(self.path)
            if url.path == "/api/save":
                p = self._body()
                slug = p.get("pop", "")
                path = deck_path(deck_dir, slug) if slug else None
                if not path:
                    return self._json({"ok": False, "error": "모집단 없음"}, 404)
                today = datetime.date.today().isoformat()
                results = {k: v for k, v in (p.get("results") or {}).items() if v in ("right", "wrong")}
                deck = parse_deck(path)
                write = apply_symbol_results if deck["config"]["mode"] == "symbol" else (
                    lambda pth, res, g: apply_results(pth, res, today, g))
                n = write(path, results, deck["config"]["graduate"]) if results else 0
                titles = {c["id"]: c["title"] for c in deck["cards"]}
                fb_new = []
                for cid, v in (p.get("feedback") or {}).items():
                    v = (v or "").strip()
                    if v:
                        fb_new.append({"date": today, "file": path, "id": cid,
                                       "title": titles.get(cid, ""), "feedback": v})
                if fb_new:  # 덮어쓰지 않고 누적 (Claude가 반영 후 비움)
                    old = []
                    if os.path.isfile(FEEDBACK_PATH):
                        try:
                            old = json.loads(read(FEEDBACK_PATH)) or []
                        except Exception:
                            old = []
                    write_atomic(FEEDBACK_PATH, json.dumps(old + fb_new, ensure_ascii=False, indent=2))
                session = {
                    "date": today,
                    "pop": slug,
                    "population": deck["population"],
                    "file": path,
                    "shown": p.get("shown", []),
                    "results": {cid: {"result": r, "title": titles.get(cid, "")} for cid, r in results.items()},
                    "guesses": {k: v for k, v in (p.get("guesses") or {}).items() if (v or "").strip()},
                    "stopped_early": bool(p.get("stopped_early")),
                    "feedback": len(fb_new),
                }
                write_atomic(LAST_SESSION_PATH, json.dumps(session, ensure_ascii=False, indent=2))
                self._json({"ok": True, "recorded": n, "feedback": len(fb_new),
                            "keep_alive": KEEP_ALIVE})
                schedule_shutdown(server_ref)
            elif url.path == "/api/quit":
                self._json({"ok": True})
                schedule_shutdown(server_ref)
            else:
                self._send(404, "not found", "text/plain")

    return Handler


def schedule_shutdown(server_ref):
    if KEEP_ALIVE:
        return
    threading.Timer(1.0, lambda: server_ref["srv"].shutdown()).start()


# ---------------------------------------------------------------- CLI

def cmd_check(deck_dir):
    """덱 파싱 검증 (폴더 전체 또는 파일 1개). 에러 있으면 exit 1."""
    decks = [parse_deck(deck_dir)] if os.path.isfile(deck_dir) else load_decks(deck_dir)
    bad = 0
    ids = {}
    print(f"덱 폴더: {deck_dir}  (모집단 {len(decks)}개)")
    for d in decks:
        print(f"- {d['slug']}: 카드 {len(d['cards'])}개 · {d['population']}")
        for e in d["errors"]:
            bad += 1
            print(f"    ✗ {e['id']}: {', '.join(e['errors'])}")
        for c in d["cards"]:
            ids.setdefault(c["id"], []).append(d["slug"])
    for cid, where in ids.items():
        if len(where) > 1:
            bad += 1
            print(f"  ✗ id 중복(파일 간): {cid} → {where}")
    total = sum(len(d["cards"]) for d in decks)
    print(f"총 카드 {total}개, 에러 {bad}건")
    return 1 if bad else 0


TABLE_MARK = "<!-- 자동 생성 표"


def cmd_readme(deck_dir):
    """README.md의 `<!-- 자동 생성 표` 줄부터 끝까지를 모집단 표로 다시 씀."""
    decks = sorted(load_decks(deck_dir), key=lambda k: curriculum_rank(deck_stats(k, "")))
    total = sum(len(k["cards"]) for k in decks)
    out, g = [f"{TABLE_MARK}: 모집단 {len(decks)}개 · 카드 {total}개 -->"], None
    for k in decks:
        if k["group"] != g:
            g = k["group"]
            out.append(f"\n### {g}\n\n| 모집단 | 카드 | 파일 |\n|---|---|---|")
        titles = " · ".join(c["title"] for c in k["cards"])
        out.append(f"| **{k['population']}** <br><sub>{titles}</sub> | {len(k['cards'])} | [[{k['slug']}]] |")
    path = os.path.join(deck_dir, "README.md")
    text = read(path) if os.path.isfile(path) else "# 수식 덱\n\n## 모집단 목록\n\n"
    i = text.find(TABLE_MARK)
    head = text[:i] if i >= 0 else text.rstrip() + "\n\n"
    write_atomic(path, head + "\n".join(out) + "\n")
    print(f"README 표 갱신: 모집단 {len(decks)}개 · 카드 {total}개")


def cmd_stats(deck_dir):
    today = datetime.date.today().isoformat()
    stats = sorted((deck_stats(d, today) for d in load_decks(deck_dir)), key=curriculum_rank)
    rec = recommend(stats)
    for s in stats:
        star = " ← 추천" if s["slug"] == rec else ""
        print(f"{s['slug']}: 총 {s['total']} · 미학습 {s['unseen']} · ❌ {s['wrong']} · ✅ {s['right']} · "
              f"💯 {s['mastered']} · 마지막 {s['last_studied'] or '-'}{star}")


def main():
    args = parse_args(sys.argv)
    deck_dir = resolve_deck_dir(args["dir"])
    if args["cmd"] == "check":
        sys.exit(cmd_check(deck_dir))
    if args["cmd"] == "stats":
        return cmd_stats(deck_dir)
    if args["cmd"] == "readme":
        return cmd_readme(deck_dir)
    if not os.path.isdir(deck_dir):
        sys.exit(f"덱 폴더 없음: {deck_dir}")
    pre = args["pop"]
    if pre and not pre.startswith("formula-"):
        pre = "formula-" + pre
    notes = NoteIndex(find_repo_root(deck_dir))
    server_ref = {}
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), make_handler(deck_dir, notes, pre, server_ref))
    server_ref["srv"] = srv
    print(f"덱 폴더: {deck_dir}")
    print(f"수식 퀴즈: http://localhost:{PORT}", flush=True)
    srv.serve_forever()
    print("세션 종료 → 서버 내림", flush=True)


if __name__ == "__main__":
    main()
