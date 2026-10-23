// 화살표 주석 커버리지 검사 — 카드마다 기호 풀이 bullet 중 몇 개가 문제 수식에서 찾히는지 + 주석 넣은 LaTeX가 렌더되는지
// 사용: node check_annotate.mjs <deck.md | deck_dir> [-v]   (-v: 카드별 매칭/라벨 전부 출력)
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
let katex;
try { katex = require("katex"); }
catch { katex = require(path.join(os.homedir(), ".cache/formula-quiz/node_modules/katex")); }
const A = require(path.join(path.dirname(new URL(import.meta.url).pathname), "annotate.js"));

const args = process.argv.slice(2);
const verbose = args.includes("-v");
const files = [];
for (const a of args.filter(x => x !== "-v")) {
  if (fs.statSync(a).isDirectory()) {
    for (const f of fs.readdirSync(a).sort()) if (f.startsWith("formula-") && f.endsWith(".md")) files.push(path.join(a, f));
  } else files.push(a);
}

const OPTS = { displayMode: true, throwOnError: true, strict: "ignore", trust: c => c.command === "\\htmlData" };
const renders = t => { try { katex.renderToString(t, OPTS); return true; } catch { return false; } };

let cards = 0, bullets = 0, matched = 0, dropped = 0, broken = 0, none = 0;
const misses = [];
for (const f of files) {
  const text = fs.readFileSync(f, "utf8");
  const parts = text.split(/^<!--\s*formula:\s*([\w-]+)\s*-->[ \t]*$/m);
  for (let i = 1; i < parts.length; i += 2) {
    const id = parts[i], body = parts[i + 1];
    const q = body.split("<details>")[0];
    const fm = /```math[ \t]*\r?\n([\s\S]*?)\r?\n[ \t]*```/.exec(q);
    const ans = (/<details>\s*<summary>[\s\S]*?<\/summary>([\s\S]*?)<\/details>/.exec(body) || [])[1] || "";
    if (!fm) continue;
    cards++;
    const bl = A.parseBullets(ans);
    const naive = A.annotate(fm[1], bl, null);
    const res = A.annotate(fm[1], bl, renders);
    const nm = res.annos.filter(a => a.hits).length;
    const nNaive = naive.annos.filter(a => a.hits).length;
    if (!renders(res.tex)) broken++;
    dropped += nNaive - nm;
    bullets += bl.length; matched += nm;
    if (bl.length && !nm) none++;
    const name = `${path.basename(f)} [${id}]`;
    if (verbose) {
      console.log(`\n■ ${name}  ${nm}/${bl.length}${nNaive > nm ? `  (렌더 실패로 ${nNaive - nm}개 뺌)` : ""}`);
      for (const a of res.annos) console.log(`   ${a.hits ? "●" : "○"} ${a.pats.join(" | ").padEnd(28)} → ${a.label}`);
    }
    for (const a of res.annos) if (!a.hits) misses.push(`${name}  ${a.pats.join(" | ")}`);
  }
}
if (!verbose && misses.length) {
  console.log("수식에서 못 찾은 bullet (목록·호버에만 나옴):");
  misses.slice(0, 400).forEach(m => console.log("  ○ " + m));
}
console.log(`\n카드 ${cards} · 수식 기호 bullet ${bullets} · 화살표 연결 ${matched} (${(100 * matched / Math.max(1, bullets)).toFixed(0)}%)` +
  ` · 렌더 실패로 뺀 것 ${dropped} · 주석 0개 카드 ${none} · 렌더 깨진 카드 ${broken}`);
process.exit(broken ? 1 : 0);
