#!/usr/bin/env python3
"""us-stock-advisor v6.0 — P4 last step: the Python-rendered 5-line Korean report header.

Install path: ~/.claude/skills/us-stock-advisor/scripts/render.py

CLI:
    render.py --run-id R [--json]

Reads  state/run/R/{aggregate_out.json, orders.json} (required) and
       state/run/R/{digest.json, indicators.json, book.json}, state/triggers.json (optional).
Prints the header (or, with --json, the header fields) and writes state/run/R/header.txt.
Exit codes: 0 ok; 2 aggregate_out.json or orders.json missing/unparseable (nothing written).

The header is pasted verbatim at the top of the Slack DM; the LLM never edits it.
Importable: header(run_dir_path) -> str, header_fields(run_dir_path) -> dict.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402

WEEKDAY_KO = ("월", "화", "수", "목", "금", "토", "일")
ACTION_KO = {"ENTER": "진입", "HOLD": "유지", "EXIT": "청산", "TRIM": "축소"}
SIDE_KO = {"BUY": "매수", "SELL": "매도"}
# forced (rule-driven, not voted) exits/trims: label by aggregate's `why`
FORCED_KO = {"KILL_FIRED": "청산(킬)", "HORIZON": "청산(만기)", "SGOV_EXPIRED": "청산(만료)", "CAP_DRIFT": "축소"}


def _action_label(action, why) -> str:
    if action == "EXIT" and why == "CAP_DRIFT":
        return "청산(한도)"
    if action in ("EXIT", "TRIM") and why in FORCED_KO:
        return FORCED_KO[why]
    return ACTION_KO[action]


def _load(path: str, required: bool = True):
    if not os.path.isfile(path):
        if required:
            raise FileNotFoundError(path)
        return None
    with open(path, encoding="utf-8") as f:
        text = f.read().strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rstrip().removesuffix("```")
    return json.loads(text)


def _num(x, default=0.0) -> float:
    if x is None or isinstance(x, bool):
        return default
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _signed(x: float) -> str:
    x = _num(x)
    if abs(x) < 0.05:
        x = 0.0
    return f"{x:+.1f}"


def _md(d: date) -> str:
    return f"{d.month}/{d.day}"


def _run_date(run_id: str) -> date:
    try:
        return datetime.strptime(str(run_id)[:8], "%Y%m%d").date()
    except ValueError:
        return datetime.now(C.KST).date()


def next_regular(run_date: date) -> date:
    """Next weekly-run weekday (KST) strictly after run_date."""
    delta = (C.WEEKLY_RUN_DOW_KST - run_date.weekday()) % 7 or 7
    return run_date + timedelta(days=delta)


def _sample_note(weeks: int) -> str:
    # short form of HONESTY_LINE_KO: its first sentence
    return C.HONESTY_LINE_KO.split(".")[0].format(weeks=weeks)


def header_fields(run_dir_path: str) -> dict:
    rd = os.path.abspath(run_dir_path)
    agg = _load(os.path.join(rd, "aggregate_out.json"))
    orders = _load(os.path.join(rd, "orders.json"))
    digest = _load(os.path.join(rd, "digest.json"), required=False) or {}
    ind = _load(os.path.join(rd, "indicators.json"), required=False) or {}
    book = _load(os.path.join(rd, "book.json"), required=False) or {}
    # state/run/<R>/ -> state/triggers.json (== C.TRIGGERS_JSON for a live run dir)
    trig_path = os.path.join(os.path.dirname(os.path.dirname(rd)), os.path.basename(C.TRIGGERS_JSON))
    try:
        trig = _load(trig_path, required=False) or {}
    except (ValueError, OSError):
        trig = {}
    run_id = agg.get("run_id") or orders.get("run_id") or os.path.basename(rd)
    run_date = _run_date(run_id)
    triggers = digest.get("triggers") or {}

    # ---- line 1: actions
    ol = sorted(orders.get("orders") or [], key=lambda o: _num(o.get("priority"), 99))
    acts = []
    for o in ol:
        side = str(o.get("action") or "").upper()
        n = int(_num(o.get("shares")))
        if side == "SELL":
            acts.append(f"{o.get('ticker')} {n}주 매도")
        else:
            usd = _num(o.get("usd"), n * _num(o.get("limit_ref_price")))
            acts.append(f"{o.get('ticker')} {n}주 매수 (~${usd:,.0f})")
    residual = orders.get("residual") or {}
    post = orders.get("post_trade") or {}
    cash = _num(residual.get("cash_usd"), _num(post.get("cash_usd"), _num(book.get("cash_usd"))))
    unavoidable = bool(residual.get("unavoidable", True))
    act_txt = " → ".join(acts) if acts else "변경 없음 (코어 유지)"
    line1 = (f"👉 이번 주 행동: {act_txt} · 체결 후 현금 ${cash:,.0f} "
             f"({'불가피 잔여' if unavoidable else '배치 가능'})")

    # ---- line 2: scoreboard
    sb = digest.get("scoreboard") or {}
    weeks = int(_num(sb.get("weeks_of_data"))) or 1
    epoch = digest.get("epoch_start") or run_date.isoformat()
    authority = triggers.get("authority") or (agg.get("caps") or {}).get("authority") or "FULL"
    if authority == "T2_SUSPENDED":
        trig_txt = "T2 중단 🔴"
    elif authority == "T1_REDUCED":
        until = ((triggers.get("T1") or {}).get("until"))
        try:
            until_txt = _md(date.fromisoformat(str(until)[:10]))
        except ValueError:
            until_txt = "?"
        trig_txt = f"T1 축소(~{until_txt})"
    else:
        trig_txt = "정상"
    line2 = (f"📊 v6 기준 {epoch}~: 계좌 {_signed(sb.get('account_twr_pct'))}% | "
             f"QQQ {_signed(sb.get('qqq_pct'))}% | "
             f"코어온리 대비 LLM 기여 {_signed(sb.get('value_add_pp'))}%p | "
             f"트리거 {trig_txt} ({_sample_note(weeks)})")

    # ---- line 3: votes
    sleeve = agg.get("sleeve") or {}
    shown = [(t, e) for t, e in sleeve.items() if e.get("action") in ACTION_KO]
    shown.sort(key=lambda te: (te[1].get("action") == "EXIT", -_num(te[1].get("weight")), te[0]))
    parts = []
    for t, e in shown:
        v = e.get("exit_votes", e.get("votes", 0)) if e.get("action") == "EXIT" else e.get("votes", 0)
        parts.append(f"{t} {_action_label(e['action'], e.get('why'))} {int(_num(v))}/3")
    sg = agg.get("sgov") or {}
    sg_label = ""
    sg_exit = next((x for x in agg.get("exits") or [] if x.get("ticker") == C.DERISK_TICKER), None)
    if sg_exit is not None:
        sg_label = _action_label("EXIT", sg_exit.get("why")) + " "
    elif _num(sg.get("weight")) > 0:
        sg_label = "유지 " if (sg.get("rule") == "INCUMBENT" or _num(sg.get("held_shares")) > 0) else "진입 "
    parts.append(f"SGOV {sg_label}{int(_num(sg.get('votes')))}/3")
    same = [v["decider"] for v in agg.get("vote_table") or [] if v.get("unchanged")]
    parts.append(f"변경없음 {', '.join(same) if same else '없음'}")
    reg = ind.get("regime") or {}
    regime = reg.get("regime") or agg.get("regime") or "?"

    def cmp(flag):
        return "?" if flag is None else (">" if flag else "<")
    line3 = (f"🗳️ 투표: {' · '.join(parts)} | 레짐 {regime} "
             f"(QQQ{cmp(reg.get('above_sma200'))}SMA200, 월봉{cmp(reg.get('above_sma10m'))}10M SMA)")
    n_drops = len(agg.get("drops") or [])
    if n_drops:
        line3 += f" · 드롭 {n_drops}건"
    if agg.get("noop_flag"):
        line3 += f" · 노옵 {int(_num(agg.get('unchanged_weeks')))}주 ⚠️"

    # ---- line 4: next run + watchdog
    nxt = next_regular(run_date)
    watch = []
    kept = [(t, e) for t, e in shown if e.get("action") in ("ENTER", "HOLD", "TRIM") and _num(e.get("weight")) > 0]
    for t, e in kept:
        for kc in e.get("kill_conditions") or []:
            if kc.get("type") == "PRICE_BELOW" and kc.get("value") is not None:
                watch.append(f"{t}<${_num(kc['value']):,.0f}")
    if regime == C.REGIME_UP:
        watch.append("QQQ<SMA200")
    elif regime == C.REGIME_DOWN:
        watch.append("QQQ>SMA200")
    for t, _ in kept:
        tk = (ind.get("tickers") or {}).get(t) or {}
        eis = tk.get("earnings_in_sessions")
        if tk.get("next_earnings_date") and isinstance(eis, (int, float)) and eis <= C.EARNINGS_WARN_SESSIONS:
            watch.append(f"실적 D-{C.EARNINGS_WARN_SESSIONS} {t}")
    line4 = f"⏭️ 다음 정기: {_md(nxt)}({WEEKDAY_KO[nxt.weekday()]}) · 워치독: {' / '.join(watch) if watch else '없음'}"
    pending_events = trig.get("events") or [] if not trig.get("consumed_by_run") else []
    if pending_events:
        line4 += f" · 미처리 이벤트 {len(pending_events)}건"

    # ---- line 5: unfilled
    t5 = triggers.get("T5") or {}
    unfilled = t5.get("unfilled") or ((book.get("reconciliation") or {}).get("unfilled") or [])
    uf = []
    for u in unfilled:
        if not isinstance(u, dict):
            continue
        side = SIDE_KO.get(str(u.get("action") or "").upper(), str(u.get("action") or ""))
        runs = int(_num(u.get("runs_unfilled"), 1))
        uf.append(f"{u.get('ticker')} {side} {int(_num(u.get('shares')))}주 ({runs}회차)")
    line5 = f"⚠️ 미체결: {', '.join(uf) if uf else '없음'}"
    if (triggers.get("T4") or {}).get("fire"):
        pct = _num(book.get("cash_pct"), _num(post.get("cash_pct")))
        line5 += f" · 현금 {pct:.1f}% {C.T4_CASH_WEEKS}주 연속 → QQQM 자동 채움"

    lines = [line1, line2, line3, line4, line5]
    return {
        "run_id": run_id, "run_date": run_date.isoformat(), "next_regular": nxt.isoformat(),
        "actions": acts, "cash_after_usd": round(cash, 2), "residual_unavoidable": unavoidable,
        "epoch_start": epoch, "weeks_of_data": weeks, "authority": authority,
        "votes": parts, "regime": regime, "drops": n_drops, "noop_flag": bool(agg.get("noop_flag")),
        "watchdog": watch, "unfilled": uf, "t4_fire": bool((triggers.get("T4") or {}).get("fire")),
        "lines": lines,
    }


def header(run_dir_path: str) -> str:
    return "\n".join(header_fields(run_dir_path)["lines"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="v6 Korean report header")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--json", action="store_true", help="print header fields as JSON")
    a = ap.parse_args(argv)
    rd = C.run_dir(a.run_id)
    try:
        fields = header_fields(rd)
    except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
        print(f"render: failed (exit 2, nothing written): {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    text = "\n".join(fields["lines"])
    tmp = os.path.join(rd, ".header.txt.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    os.replace(tmp, os.path.join(rd, "header.txt"))
    print(json.dumps(fields, ensure_ascii=False, indent=1) if a.json else text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
