// 수식 화살표 주석 — 카드의 **기호 풀이** bullet(`- $기호$ ← 설명`)을 문제 수식 속 기호와 매칭해서
// 그 부분을 \htmlData{sym=k}{...}로 감싼 LaTeX를 만든다. 덱 md는 안 건드림 (렌더 직전에만 변환).
// quiz.html(브라우저)과 check_annotate.mjs(node)가 같이 씀.
(function (root) {
  "use strict";

  // ---------------------------------------------------------------- 기호 풀이 bullet 파싱
  // 반환: [{li, pats:[latex...], text}] — li = 기호 풀이 목록의 top-level 항목 순번
  function parseBullets(answerMd) {
    const lines = answerMd.split(/\r?\n/);
    const start = lines.findIndex(l => /^\*\*기호 풀이\*\*/.test(l.trim()));
    if (start < 0) return [];
    const out = [];
    let li = -1;
    for (let i = start + 1; i < lines.length; i++) {
      const l = lines[i];
      if (/^\*\*[^*]+\*\*/.test(l) || /^<\/details>/.test(l) || /^```/.test(l)) break;
      const m = /^[-*] (.*)$/.exec(l);
      if (!m) continue;
      li++;
      const body = m[1];
      const arrow = body.indexOf("←");
      if (arrow < 0) continue;
      const head = body.slice(0, arrow).trim();
      // 머리 부분이 `$a$`, `$a$, $b$`, `$a$ / $b$` 꼴일 때만 (텍스트 머리는 수식 기호가 아님)
      if (!/^\$[^$]+\$(\s*(,|\/|·|와)\s*\$[^$]+\$)*$/.test(head)) continue;
      const pats = [...head.matchAll(/\$([^$]+)\$/g)].map(x => x[1].trim());
      out.push({ li, pats, text: body.slice(arrow + 1).trim() });
    }
    return out;
  }

  // 화살표 라벨용 짧은 설명: 첫 구절(마침표·← 앞), 길면 괄호 부연 제거 → 그래도 길면 자름
  // 자르는 위치는 전부 $수식$ 바깥에서만 찾음 (mask = 수식 부분을 같은 길이 □로 가린 문자열)
  function shortLabel(text) {
    const mask = t => t.replace(/\$[^$]*\$/g, m => "□".repeat(m.length));
    const vis = t => t.replace(/\$([^$]+)\$/g, (m, x) => "x".repeat(Math.min(6, Math.ceil(x.length / 3)))).replace(/\*\*/g, "").length;
    const cutAt = (t, re, min = 0) => { // min: 잘린 결과가 이보다 짧으면 안 자름 ("행마다" 같은 반토막 방지)
      const i = mask(t).search(re);
      return i > 0 && vis(t.slice(0, i)) >= min ? t.slice(0, i) : t;
    };
    const dropParens = t => { // 괄호 부연 (수식 바깥 괄호만) 제거
      let m = mask(t), out = t;
      for (let guard = 0; guard < 4; guard++) {
        const r = /\s*\([^()]*\)/.exec(m);
        if (!r) break;
        out = out.slice(0, r.index) + out.slice(r.index + r[0].length);
        m = m.slice(0, r.index) + m.slice(r.index + r[0].length);
      }
      return out.trim();
    };
    const first = cutAt(text, /\s←\s|←|(?<!\d)\.(?=\s|$)|;|\s—\s|\s→\s/).trim();
    let s = first;
    if (vis(s) > 14) s = dropParens(s);
    if (vis(s) > 20) s = cutAt(s, /,\s|\s=\s|:\s/, 7);
    if (vis(s) > 18) { const t = s.replace(/\s*\$\s*=[^$]*\$\s*$/, "").trim(); if (t.length > 3) s = t; } // 꼬리 "$= …$" 정의
    if (vis(s) > 24) { // 보이는 길이 ~22에서 수식 바깥 공백 기준으로 자름
      let v = 0, best = -1, firstMathEnd = -1;
      for (let i = 0; i < s.length;) {
        if (s[i] === "$") {
          const j = s.indexOf("$", i + 1);
          if (j < 0) break;
          v += Math.min(6, Math.ceil((j - i - 1) / 3));
          i = j + 1;
          if (firstMathEnd < 0) firstMathEnd = i;
          continue;
        }
        if (s.startsWith("**", i)) { i += 2; continue; }
        if (s[i] === " " && v <= 22 && i > 0) best = i;
        v++; i++;
      }
      const at = best > 0 ? best : firstMathEnd > 0 ? firstMathEnd : 22;
      s = s.slice(0, at).trim() + "…";
    }
    let depth = 0, cut = -1;
    const m = mask(s);
    for (let i = 0; i < m.length; i++) {
      if (m[i] === "(") { if (depth === 0) cut = i; depth++; }
      else if (m[i] === ")") depth = Math.max(0, depth - 1);
    }
    if (depth > 0 && cut > 1) s = s.slice(0, cut);
    s = s.replace(/[,:·=\s]+$/, "").trim();
    if (!s || s.replace(/\$[^$]*\$/g, "").trim().length === 0 && s.length < 3) s = first.slice(0, 40);
    return s;
  }

  // ---------------------------------------------------------------- LaTeX 토큰화
  const ARG_ATOMIC = new Set(("hat bar tilde dot ddot vec widehat widetilde overline underline overrightarrow " +
    "mathbf boldsymbol bm mathbb mathcal mathrm mathit mathsf mathtt mathfrak mathscr text textbf textit textrm " +
    "operatorname operatorname* begin end").split(" ").map(x => "\\" + x));
  const RM = new Set(["\\mathrm", "\\text", "\\textrm", "\\operatorname", "\\operatorname*"]);
  const SIZING = new Set(["\\big", "\\Big", "\\bigg", "\\Bigg", "\\bigl", "\\bigr", "\\Bigl", "\\Bigr",
    "\\biggl", "\\biggr", "\\Biggl", "\\Biggr", "\\bigm", "\\Bigm"]);
  const IGNORE = new Set(["\\,", "\\;", "\\:", "\\!", "\\ ", "\\quad", "\\qquad", "~", "\\enspace", "\\thinspace",
    "\\big", "\\Big", "\\bigg", "\\Bigg", "\\bigl", "\\bigr", "\\Bigl", "\\Bigr", "\\biggl", "\\biggr", "\\Biggl", "\\Biggr",
    "\\displaystyle", "\\textstyle", "\\limits", "\\nolimits"]);
  const ALIAS = { "\\le": "\\leq", "\\ge": "\\geq", "\\lt": "<", "\\gt": ">", "\\ne": "\\neq", "\\to": "\\rightarrow",
    "\\gets": "\\leftarrow", "\\lvert": "|", "\\rvert": "|", "\\vert": "|", "\\lVert": "\\|", "\\rVert": "\\|", "\\Vert": "\\|",
    "\\intercal": "\\top", "\\ast": "*", "\\bm": "\\boldsymbol", "\\dfrac": "\\frac", "\\tfrac": "\\frac", "\\lbrace": "\\{", "\\rbrace": "\\}" };
  const REL = new Set(["=", "<", ">", "\\leq", "\\geq", "\\approx", "\\sim", "\\simeq", "\\propto", "\\in", "\\notin",
    "\\subset", "\\subseteq", "\\rightarrow", "\\leftarrow", "\\mapsto", "\\iff", "\\Rightarrow", "\\Leftarrow", "\\implies",
    "\\equiv", "\\neq", "\\coloneqq", "\\triangleq", "\\ll", "\\gg", "\\perp", "\\Leftrightarrow"]);
  const BIN = new Set(["+", "-", "\\pm", "\\mp", "\\cdot", "\\times", "\\odot", "\\otimes", "\\oplus", "\\circ", "*",
    "\\star", "\\cup", "\\cap", "\\setminus", "\\div"]);
  const OP_LIMITS = new Set(["\\sum", "\\prod", "\\max", "\\min", "\\lim", "\\sup", "\\inf", "\\argmax", "\\argmin",
    "\\bigcup", "\\bigcap", "\\coprod"]);
  const OP_NOLIMITS = new Set(["\\log", "\\exp", "\\ln", "\\det", "\\tr", "\\int", "\\iint", "\\oint", "\\sin", "\\cos",
    "\\tan", "\\tanh", "\\sinh", "\\cosh", "\\arg", "\\Pr", "\\lg"]);

  // 토큰: {k: 매칭 키, s, e: 원문 범위, br: +1 { / -1 }, lr: +1 \left / -1 \right, ign, bad}
  function tokenize(src) {
    const toks = [];
    let i = 0;
    const n = src.length;
    const skipWs = () => { while (i < n && /\s/.test(src[i])) i++; };
    function readGroupRaw() { // src[i] === '{' → 짝 맞는 } 까지 원문
      let d = 0, j = i;
      for (; j < n; j++) {
        if (src[j] === "\\") { j++; continue; }
        if (src[j] === "{") d++;
        else if (src[j] === "}") { d--; if (d === 0) break; }
      }
      const raw = src.slice(i + 1, j);
      i = j + 1;
      return raw;
    }
    while (i < n) {
      const c = src[i];
      if (/\s/.test(c)) { i++; continue; }
      const s = i;
      if (c === "\\") {
        let m = /^\\([A-Za-z]+\*?|.)/.exec(src.slice(i));
        let name = "\\" + m[1];
        if (name.endsWith("*") && name !== "\\operatorname*") name = name.slice(0, -1);
        i += 1 + name.length - 1;
        if (name === "\\operatorname" && src[i] === "*") { name = "\\operatorname*"; i++; }
        name = ALIAS[name] || name;
        if (name === "\\left" || name === "\\right" || name === "\\middle") {
          skipWs();
          let d;
          if (src[i] === "\\") { const mm = /^\\([A-Za-z]+|.)/.exec(src.slice(i)); d = "\\" + mm[1]; i += d.length; d = ALIAS[d] || d; }
          else { d = src[i]; i++; }
          toks.push({ k: d === "." ? "" : d, s, e: i, lr: name === "\\left" ? 1 : name === "\\right" ? -1 : 0,
            ign: d === ".", bad: name === "\\middle", mid: name === "\\middle" });
          continue;
        }
        if (ARG_ATOMIC.has(name)) {
          skipWs();
          let arg = "";
          if (src[i] === "{") arg = readGroupRaw();
          else if (src[i] === "\\") { const mm = /^\\([A-Za-z]+|.)/.exec(src.slice(i)); arg = "\\" + mm[1]; i += arg.length; }
          else if (i < n) { arg = src[i]; i++; }
          const fam = RM.has(name) ? "\\rm" : name;
          const key = fam + "{" + arg.replace(/\s+|\\[,;:!]/g, "") + "}";
          toks.push({ k: key, cmd: name, s, e: i, bad: name === "\\begin" || name === "\\end" });
          continue;
        }
        if (name === "\\\\") { toks.push({ k: name, s, e: i, bad: true }); continue; }
        toks.push({ k: name, s, e: i, ign: IGNORE.has(name) });
        continue;
      }
      i++;
      if (c === "{") toks.push({ k: "{", s, e: i, br: 1 });
      else if (c === "}") toks.push({ k: "}", s, e: i, br: -1 });
      else if (c === "&") toks.push({ k: "&", s, e: i, bad: true });
      else if (c === "~") toks.push({ k: "~", s, e: i, ign: true });
      else toks.push({ k: c, s, e: i });
    }
    // 토큰 하나짜리 그룹 {x} → x 로 접기 (x_{t} == x_t 매칭용)
    const out = [];
    for (let j = 0; j < toks.length; j++) {
      const t = toks[j];
      if (t.br === 1) {
        let k = j + 1;
        const inner = [];
        while (k < toks.length && toks[k].ign) k++;
        if (k < toks.length && !toks[k].br) { inner.push(toks[k]); k++; while (k < toks.length && toks[k].ign) k++; }
        if (inner.length === 1 && k < toks.length && toks[k].br === -1 && !inner[0].lr) {
          out.push({ k: inner[0].k, s: t.s, e: toks[k].e, folded: true });
          j = k;
          continue;
        }
      }
      out.push(t);
    }
    return out;
  }

  // 그룹 끝 인덱스 (toks[j]가 { 이면 짝 }의 인덱스, 아니면 j)
  function groupEnd(toks, j) {
    if (toks[j].br !== 1) return j;
    let d = 0;
    for (let k = j; k < toks.length; k++) {
      d += toks[k].br || 0;
      if (d === 0) return k;
    }
    return toks.length - 1;
  }

  // 매칭 구간 [a, b] (토큰 인덱스)가 독립적으로 감쌀 수 있는 덩어리인지
  function wellFormed(toks, a, b) {
    let br = 0, lr = 0;
    for (let k = a; k <= b; k++) {
      const t = toks[k];
      if (t.bad) return false;
      br += t.br || 0;
      lr += t.lr || 0;
      if (br < 0 || lr < 0) return false;
    }
    if (br !== 0 || lr !== 0) return false;
    const first = toks[a].k, last = toks[b].k;
    if (first === "_" || first === "^" || first === "'" || last === "_" || last === "^") return false;
    if (a !== b && a > 0 && (toks[a - 1].k === "_" || toks[a - 1].k === "^")) return false; // x_t - 1 의 t-1 같은 가짜 매칭
    return true;
  }

  function sig(toks) { return toks.map((t, i) => i).filter(i => !toks[i].ign); }

  // 기호 뒤에 붙은 아래첨자(+ 전치·별표·layer 위첨자)·프라임까지 확장
  const SUP_KEEP = new Set(["\\top", "T", "*", "\\star", "\\prime", "\\dagger"]);
  function extend(toks, b) {
    let j = b;
    for (let guard = 0; guard < 4; guard++) {
      const nx = j + 1;
      if (nx >= toks.length) break;
      const t = toks[nx];
      if (t.k === "'") { j = nx; continue; }
      if ((t.k === "_" || t.k === "^") && nx + 1 < toks.length) {
        const argI = nx + 1;
        const arg = toks[argI];
        if (t.k === "^") {
          const keep = SUP_KEEP.has(arg.k) || (arg.br === 1 && argI + 1 < toks.length && ["(", "["].includes(toks[argI + 1].k));
          if (!keep) break;
        }
        j = groupEnd(toks, argI);
        continue;
      }
      break;
    }
    return j;
  }

  const isOp = t => OP_LIMITS.has(t.k) || OP_NOLIMITS.has(t.k) || t.cmd === "\\operatorname" || t.cmd === "\\operatorname*";
  const OPEN = new Set(["(", "[", "\\{", "\\langle"]), CLOSE = new Set([")", "]", "\\}", "\\rangle"]);
  const WILD_L = new Set(["(", "[", "\\{", "|", "\\|", ",", "\\mid", "\\langle"]);
  const WILD_R = new Set([")", "]", "\\}", "|", "\\|", ",", "\\mid", "\\rangle"]);

  // 패턴 → [{k, wild}] — `h(\cdot)`, `\lVert \cdot \rVert^2` 의 \cdot 은 "아무 식" 자리표시
  function compile(pat) {
    const P = pat.filter(t => !t.ign).map(t => ({ k: t.k }));
    P.forEach((t, i) => {
      if (t.k !== "\\cdot") return;
      const l = i === 0 || WILD_L.has(P[i - 1].k), r = i === P.length - 1 || WILD_R.has(P[i + 1].k);
      if (l && r && P.length > 1) t.wild = true;
    });
    return P;
  }

  // 반환: hits = [[ [a,b], [a,b], ... ]] — 조각(piece) 목록. 자리표시 없으면 조각 1개
  function findAll(toks, pat) {
    const S = sig(toks), P = compile(pat);
    const pos = new Map(S.map((ti, x) => [ti, x]));
    const hits = [];
    if (!P.length || P.every(t => t.wild)) return hits;
    // 패턴에 없는 아래·위첨자는 건너뜀 (\mathbb{E}[\cdot] ↔ \mathbb{E}_\pi[...])
    const skipScript = x => {
      const ti = S[x];
      if (x <= 0 || x >= S.length || !["_", "^"].includes(toks[ti].k) || ti + 1 >= toks.length) return x;
      const end = groupEnd(toks, ti + 1);
      let nx = x + 1;
      while (nx < S.length && S[nx] <= end) nx++;
      return nx;
    };
    function go(x, y, pieces, run) {
      if (y === P.length) return run ? [...pieces, run] : pieces;
      const p = P[y];
      if (p.wild) {
        let d = 0;
        for (let z = x; z < S.length; z++) {
          const t = toks[S[z]];
          if (t.bad && !t.mid) return null;
          d += (t.br || 0) + (t.lr || 0);
          if (!t.br && !t.lr) { if (OPEN.has(t.k)) d++; else if (CLOSE.has(t.k)) d--; }
          if (d < 0) return null;
          if (d === 0) {
            const r = go(z + 1, y + 1, run ? [...pieces, run] : pieces, null);
            if (r) return r;
          }
        }
        return null;
      }
      if (run && p.k !== "_" && p.k !== "^") {
        const nx = skipScript(x);
        if (nx !== x) {
          const r = go(nx, y, pieces, [run[0], S[nx - 1]]);
          if (r) return r;
        }
      }
      if (x >= S.length || toks[S[x]].k !== p.k) return null;
      return go(x + 1, y + 1, pieces, run ? [run[0], S[x]] : [S[x], S[x]]);
    }
    for (let x = 0; x < S.length; x++) {
      if (toks[S[x]].k !== (P[0].wild ? toks[S[x]].k : P[0].k)) continue;
      const pieces = go(x, 0, [], null);
      if (!pieces) continue;
      const wild = P.some(t => t.wild);
      const ok = [];
      for (let [a, b] of pieces) {
        if (wild) { // 자리표시 패턴: 조각 양끝의 \left( \right) 는 떼고 감쌈
          while (a <= b && toks[a].lr) a++;
          while (b >= a && toks[b].lr) b--;
          if (a > b) continue;
        }
        if (!wellFormed(toks, a, b)) { if (wild) continue; ok.length = 0; break; }
        if (!(a === b && isOp(toks[a]))) b = extend(toks, b);
        while (a > 0 && SIZING.has(toks[a - 1].k)) a--; // \big( 의 \big 는 떼면 렌더 깨짐
        ok.push([a, b]);
      }
      if (ok.length) { hits.push(ok); x = pos.get(ok[ok.length - 1][0]) ?? x; }
    }
    return hits;
  }

  // 정의식(`t = g(x)`, `y \in \{0,1\}`) → 좌변, 콤마 나열(`x_1, x_2`) → 각각
  function variants(latex) {
    const toks = tokenize(latex);
    const out = [[toks]];
    const top = [];
    let d = 0;
    toks.forEach((t, i) => { d += (t.br || 0) + (t.lr || 0); if (d === 0) top.push(i); });
    const splitAt = (pred) => {
      const parts = [];
      let cur = [];
      toks.forEach((t, i) => { if (top.includes(i) && pred(t)) { parts.push(cur); cur = []; } else cur.push(t); });
      parts.push(cur);
      return parts.filter(p => p.length);
    };
    const commas = splitAt(t => t.k === ",");
    if (commas.length > 1) out.push(commas);
    const DEF = new Set(["=", "\\in", "\\sim", "\\approx", "\\equiv", "\\triangleq", "\\coloneqq", ":"]);
    const defI = toks.findIndex((t, i) => top.includes(i) && DEF.has(t.k));
    if (defI > 0) {
      const lhs = toks.slice(0, defI);
      out.push([lhs]);
      const lc = [];
      let cur = [];
      lhs.forEach(t => { if (t.k === ",") { lc.push(cur); cur = []; } else cur.push(t); });
      lc.push(cur);
      if (lc.length > 1) out.push(lc.filter(p => p.length));
    }
    return out;
  }

  // 메인: question LaTeX + bullets → {tex, annos}
  //   annos[k] = {k, li, pats, text, label, hits} — hits = 수식 안에서 찾은 위치 수 (0이면 목록에만 있음)
  //   check(tex) → true면 렌더 OK (KaTeX로 실제 렌더 시도), 실패하는 주석은 하나씩 빼고 재시도
  function annotate(qtex, bullets, check) {
    const toks = tokenize(qtex);
    const annos = bullets.map((b, k) => ({ k, li: b.li, pats: b.pats, text: b.text, label: shortLabel(b.text), hits: 0 }));
    // 후보: bullet마다 전체 → 콤마 분해 → 좌변 순으로 처음 찾히는 변형 사용. 후보 = 조각 목록(같은 색)
    const cands = [];
    annos.forEach(an => {
      for (const p of an.pats) {
        for (const parts of variants(p)) {
          const found = [];
          for (const part of parts) for (const h of findAll(toks, part)) found.push(h);
          if (found.length) {
            const len = Math.max(...parts.map(pp => pp.filter(t => !t.ign).length));
            found.forEach(pieces => cands.push({ k: an.k, pieces, len, span: Math.max(...pieces.map(([a, b]) => b - a)) }));
            break;
          }
        }
      }
    });
    // 긴 패턴 먼저 차지. 같은 구간 중복 X, 부분 겹침 X, 완전 포함(중첩)은 OK
    cands.sort((x, y) => y.span - x.span || y.len - x.len || x.k - y.k);
    const taken = [];
    const clash = (c, t) => (t[0] === c[0] && t[1] === c[1]) ||
      (c[0] < t[0] && c[1] >= t[0] && c[1] < t[1]) || (c[0] > t[0] && c[0] <= t[1] && c[1] > t[1]);
    for (const c of cands) {
      if (!c.pieces.some(pc => taken.some(t => clash(pc, t.r)))) c.pieces.forEach(r => taken.push({ k: c.k, r }));
    }
    let picked = taken.map(t => ({ k: t.k, a: t.r[0], b: t.r[1] }));
    // 감싸기: 구간을 트리로 만든 뒤(완전 포함 = 자식) 바깥부터 재귀 조립
    const build = (list) => {
      const nodes = list.map(c => ({ c, s: toks[c.a].s, e: toks[c.b].e, folded: c.a === c.b && !!toks[c.a].folded, kids: [] }))
        .sort((x, y) => x.s - y.s || y.e - x.e);
      const roots = [], stack = [];
      for (const nd of nodes) {
        while (stack.length && stack[stack.length - 1].e <= nd.s) stack.pop();
        (stack.length ? stack[stack.length - 1].kids : roots).push(nd);
        stack.push(nd);
      }
      const emit = (s, e, kids) => {
        let out = "", pos = s;
        for (const ch of kids) { out += qtex.slice(pos, ch.s) + wrap(ch); pos = ch.e; }
        return out + qtex.slice(pos, e);
      };
      const wrap = (nd) => {
        const inner = nd.folded ? emit(nd.s + 1, nd.e - 1, nd.kids) : emit(nd.s, nd.e, nd.kids);
        const t = toks[nd.c.a], single = nd.c.a === nd.c.b;
        const box = `\\htmlData{sym=${nd.c.k}}{${inner}}`;
        if (single && REL.has(t.k)) return `\\mathrel{${box}}`;
        if (single && BIN.has(t.k)) return `\\mathbin{${box}}`;
        if (single && (OP_LIMITS.has(t.k) || t.cmd === "\\operatorname*")) return `\\mathop{${box}}\\limits`;
        if (single && isOp(t)) return `\\mathop{${box}}\\nolimits`;
        return `{${box}}`;
      };
      return emit(0, qtex.length, roots);
    };
    let tex = build(picked);
    if (check && picked.length && !check(tex)) {
      // 하나씩 넣어보며 렌더 되는 것만 유지
      const ok = [];
      for (const c of picked) if (check(build([...ok, c]))) ok.push(c);
      picked = ok;
      tex = build(picked);
    }
    picked.forEach(c => annos[c.k].hits++);
    return { tex, annos };
  }

  const API = { parseBullets, shortLabel, tokenize, annotate };
  if (typeof module !== "undefined" && module.exports) module.exports = API;
  else root.FormulaAnnotate = API;
})(typeof window !== "undefined" ? window : this);
