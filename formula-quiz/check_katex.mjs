// 수식 덱 KaTeX 렌더 검증기 — 퀴즈 앱(quiz.html)과 같은 규칙으로 수식을 뽑아서 렌더해 봄
// katex 설치: npm install katex@0.16.11 --prefix ~/.cache/formula-quiz
// 사용: node check_katex.mjs <deck.md | deck_dir> [...]
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
let katex;
try { katex = require("katex"); }
catch { katex = require(path.join(os.homedir(), ".cache/formula-quiz/node_modules/katex")); }

// quiz.html의 MATH_RE와 동일하게 유지할 것
const MATH_RE = /^([ \t]*)```math[ \t]*\r?\n([\s\S]*?)\r?\n[ \t]*```[ \t]*$|(```[\s\S]*?```|`[^`\n]+`)|\$\$([\s\S]+?)\$\$|(?<![\\$])\$(?![\s$])((?:\\\$|[^$\n])+?)(?<![\s\\])\$(?!\d)/gm;

function files(args) {
  const out = [];
  for (const a of args) {
    const st = fs.statSync(a);
    if (st.isDirectory()) {
      for (const f of fs.readdirSync(a).sort())
        if (f.startsWith("formula-") && f.endsWith(".md")) out.push(path.join(a, f));
    } else out.push(a);
  }
  return out;
}

function cardAt(text, pos) {
  const before = text.slice(0, pos);
  const m = [...before.matchAll(/<!--\s*formula:\s*([\w-]+)\s*-->/g)].pop();
  return m ? m[1] : "(header)";
}

let errors = 0, warns = 0, total = 0;
for (const f of files(process.argv.slice(2))) {
  const text = fs.readFileSync(f, "utf8");
  const name = path.basename(f);
  for (const m of text.matchAll(MATH_RE)) {
    if (m[3] !== undefined) continue; // code span/fence
    let tex, display, kind;
    if (m[2] !== undefined) { tex = m[2]; display = true; kind = "fence"; }
    else if (m[4] !== undefined) { tex = m[4]; display = true; kind = "$$"; }
    else { tex = m[5]; display = false; kind = "$"; }
    total++;
    const where = `${name} [${cardAt(text, m.index)}]`;
    const snip = tex.replace(/\s+/g, " ").slice(0, 90);
    const strictMsgs = [];
    try {
      katex.renderToString(tex, {
        displayMode: display, throwOnError: true,
        strict: (code, msg) => { strictMsgs.push(code); return "ignore"; },
      });
    } catch (e) {
      errors++;
      console.log(`✗ ERROR ${where} (${kind}): ${e.message.split("\n")[0]}\n     ${snip}`);
      continue;
    }
    const w = [];
    if (kind === "$" && tex.includes("\\\\")) w.push("인라인 $...$ 안에 \\\\ (금지)");
    if (kind === "$$" && /\n/.test(tex)) w.push("$$ 멀티라인 블록 (금지 → ```math 펜스)");
    if (/\\begin\{(align\*?|eqnarray)\}/.test(tex)) w.push("align/eqnarray (→ aligned)");
    if (/[<>]/.test(tex)) w.push("raw < > (→ \\lt \\gt)");
    if (strictMsgs.includes("unicodeTextInMathMode")) w.push("수식 안 한글/유니코드 (→ \\text{})");
    if (w.length) { warns++; console.log(`△ WARN  ${where} (${kind}): ${w.join(", ")}\n     ${snip}`); }
  }
}
console.log(`\n수식 ${total}개 렌더 · 에러 ${errors} · 경고 ${warns}`);
process.exit(errors ? 1 : 0);
