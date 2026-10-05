"""Ledger: append-only across every writer, duplicate-run guard, halts, tamper
evidence, the scorer's override_score appends, and the cron installer's dry run."""
from __future__ import annotations

import json
import os
import stat
from datetime import timedelta

import pandas as pd
import pytest

import config as C
import score_recs as S
import validate as V
from helpers import (build_ledger, make_baseline, mark, portfolio, read_bytes, run, utc_today,
                     write_json)


def _confirm_final(tmp_path, b):
    out = V.validate(json.loads(json.dumps(b)),
                     {"decision": "CONFIRM_BASELINE", "final_allocation": {"QQQ": 100.0},
                      "satellite": [], "override": None, "vetoes": []},
                     str(tmp_path / "none.jsonl"), str(tmp_path / "none.json"))
    assert out["verdict"] == "PASS", out["violations"]
    return write_json(tmp_path / "final.json", out)


def _log(state, tmp_path, b=None, *extra):
    b = b or make_baseline()
    bp = write_json(tmp_path / "baseline.json", b)
    fp = _confirm_final(tmp_path, b)
    return run("report.py", "--log", "--baseline", bp, "--final", fp, *extra, state=state)


# ----------------------------------------------------------- duplicate guard
def test_duplicate_run_guard_exit_4(state, tmp_path):
    r1 = _log(state, tmp_path)
    assert r1.returncode == 0, r1.stderr
    recs, lr = state / "recommendations.jsonl", state / "last_run.json"
    b_recs, b_lr = read_bytes(recs), read_bytes(lr)
    r2 = _log(state, tmp_path)
    assert r2.returncode == 4, (r2.returncode, r2.stderr)
    assert "DUPLICATE" in r2.stderr
    assert read_bytes(recs) == b_recs and read_bytes(lr) == b_lr      # nothing touched
    assert not [x for x in S.load_recs(str(recs)) if x["type"] == "pipeline_halt"]  # not a halt


def test_force_relog_supersedes(state, tmp_path):
    assert _log(state, tmp_path).returncode == 0
    recs = state / "recommendations.jsonl"
    before = read_bytes(recs)
    r0 = _log(state, tmp_path, None, "--force-relog")               # no --reason: refused
    assert r0.returncode == 2 and read_bytes(recs) == before, r0.stderr
    r = _log(state, tmp_path, None, "--force-relog", "--reason", "broker fill corrected")
    assert r.returncode == 0, r.stderr
    assert read_bytes(recs).startswith(before)
    loaded = S.load_recs(str(recs))
    marks = [x for x in loaded if x["type"] == "portfolio_mark"]
    assert len(marks) == 2 and marks[-1]["supersedes_same_date"] is True
    assert marks[0]["supersedes_same_date"] is False
    assert marks[-1]["relog_reason"] == "broker fill corrected"
    eff = S.effective_recs(loaded)
    assert len([x for x in eff if x["type"] == "portfolio_mark"]) == 1
    assert S.superseded_run_count(loaded) == 1
    assert S.ledger_intact(str(recs), str(state / "last_run.json"))


def test_run_logs_orders_with_whole_shares(state, tmp_path):
    assert _log(state, tmp_path).returncode == 0
    loaded = S.load_recs(str(state / "recommendations.jsonl"))
    assert [x["type"] for x in loaded] == ["portfolio_mark", "order"]
    o = loaded[1]
    assert (o["ticker"], o["action"], o["shares"], o["sleeve"], o["whole_shares"]) == \
        ("QQQM", "BUY", 1.0, "core", True)
    m = loaded[0]
    assert m["schema"] == "mark/v5.1" and m["execution_residual"]["unavoidable"] is True
    lr = json.load(open(state / "last_run.json"))
    assert lr["core_tickers"] == list(C.CORE_TICKERS) and lr["ledger_len"] == 2


# ---------------------------------------------------- acceptance test 6
def test_noop_run_appends_one_mark_and_one_noop(state, tmp_path):
    b = make_baseline(pf=portfolio(88.42, QQQ=2, QQQM=1))
    assert b["orders"] == []
    r = _log(state, tmp_path, b)
    assert r.returncode == 0, r.stderr
    loaded = S.load_recs(str(state / "recommendations.jsonl"))
    assert [x["type"] for x in loaded] == ["portfolio_mark", "noop"]
    assert "no whole core share fits" in loaded[1]["reason"]     # residual reason surfaced


def test_halt_appends_and_keeps_anchor_valid(state, tmp_path):
    assert _log(state, tmp_path).returncode == 0
    recs = state / "recommendations.jsonl"
    before = read_bytes(recs)
    r = run("report.py", "--halt", "yfinance down", "--stage", "phase0", state=state)
    assert r.returncode == 0, r.stderr
    assert read_bytes(recs).startswith(before)
    last = S.load_recs(str(recs))[-1]
    assert last["type"] == "pipeline_halt" and last["reason"] == "yfinance down"
    assert S.ledger_intact(str(recs), str(state / "last_run.json"))


# ------------------------------------------- append-only across every writer
def _override_ledger(state, n=3, days_ago=40):
    recs = []
    for i in range(n):
        d = str(utc_today() - timedelta(days=days_ago - i))
        recs += [mark(d, 1000.0, 0.0),
                 {"type": "override", "date": d, "ticker": "NVDA", "action": "OVERRIDE",
                  "direction": "de_risk", "spread_vs_baseline_pp": None}]
    build_ledger(state / "recommendations.jsonl", recs)


def _falling_nvda_csv(path):
    idx = pd.date_range(end=pd.Timestamp(utc_today() - timedelta(days=1)), periods=120, freq="D")
    df = pd.DataFrame({"QQQ": [100.0] * len(idx),
                       "QQQM": [41.17] * len(idx),
                       "NVDA": [200.0 * 0.995 ** i for i in range(len(idx))]}, index=idx)
    df.index.name = "Date"
    df.to_csv(path)
    return str(path)


def test_scorer_appends_override_score_never_rewrites(state, tmp_path):
    _override_ledger(state)
    recs = state / "recommendations.jsonl"
    csv = _falling_nvda_csv(tmp_path / "px.csv")
    assert V.override_suspended(str(recs)) is None               # spreads not yet scored
    before = read_bytes(recs)
    r = run("score_recs.py", "--score", "--prices-csv", csv, state=state)
    assert r.returncode == 0, r.stderr
    after = read_bytes(recs)
    assert after.startswith(before)
    loaded = S.load_recs(str(recs))
    scores = [x for x in loaded if x["type"] == "override_score"]
    assert len(scores) == 3 and all(x["spread_vs_baseline_pp"] < 0 for x in scores)
    assert S.ledger_intact(str(recs), str(state / "last_run.json"))
    merged = S.merged_overrides(loaded)
    assert all(m["spread_vs_baseline_pp"] is not None for m in merged)
    assert V.override_suspended(str(recs)) is not None           # the counter now fires
    # idempotent: a second nightly run appends nothing
    assert run("score_recs.py", "--score", "--prices-csv", csv, state=state).returncode == 0
    assert read_bytes(recs) == after
    assert not hasattr(S, "rewrite_recs")


def test_prefix_is_byte_stable_across_every_writer(state, tmp_path):
    recs = state / "recommendations.jsonl"
    csv = _falling_nvda_csv(tmp_path / "px.csv")
    _override_ledger(state, n=1, days_ago=40)
    snaps = [read_bytes(recs)]

    def step(res):
        assert res.returncode == 0, res.stderr
        cur = read_bytes(recs)
        assert cur.startswith(snaps[-1]) and len(cur) > len(snaps[-1])
        snaps.append(cur)
        assert S.ledger_intact(str(recs), str(state / "last_run.json"))

    step(_log(state, tmp_path))                                              # --log
    step(run("report.py", "--halt", "test halt", state=state))               # --halt
    step(run("report.py", "--adjust-flow", "--effective-date", str(utc_today() - timedelta(days=39)),
             "--flow-usd", "12.5", "--reason", "test", state=state))       # --adjust-flow
    step(run("score_recs.py", "--score", "--prices-csv", csv, state=state))  # --score
    step(_log(state, tmp_path, None, "--force-relog", "--reason", "re-run"))  # forced re-log
    r = run("report.py", "--header", "--prices-csv", csv, state=state)      # header: reader
    assert r.returncode == 0, r.stderr
    assert read_bytes(recs).startswith(snaps[-1])
    h = json.loads(r.stdout)
    assert h["수익률_방식"].startswith("TWR") and h["입출금_보정_건수"] == 1
    assert h["중복_기록_제외_건수"] == 1 and h["원장_변조_감지"] is False
    for k in ("섀도우_적중률_vs_QQQ", "섀도우_표본수", "섀도우_평균초과수익_20d_pp", "실전_승격_가능",
              "외부_입출금_순액_USD", "누적_손익_USD", "불가피_잔여현금_USD", "불가피_잔여현금_pct",
              "미확인_입출금_의심"):
        assert k in h


# ------------------------------------------------------------ tamper evidence
def _good(state):
    build_ledger(state / "recommendations.jsonl",
                 [mark(f"2025-01-0{i}", 100.0 + i, 10.0) for i in range(1, 6)])
    return state / "recommendations.jsonl", state / "last_run.json"


def _relink(lines):
    out, prev = [], None
    for ln in lines:
        r = json.loads(ln)
        r["prev_hash"] = S._line_hash(prev) if prev is not None else None
        prev = json.dumps(r, ensure_ascii=False)
        out.append(prev)
    return out


@pytest.mark.parametrize("kind", ["mid_edit", "truncate_bottom", "truncate_top", "delete",
                                  "relink_forgery"])
def test_tamper_detected_and_fail_safe(state, tmp_path, kind, monkeypatch):
    recs, lr = _good(state)
    lines = recs.read_text().splitlines()
    anchor_before = read_bytes(lr)
    if kind == "mid_edit":
        lines[2] = lines[2].replace('"total_usd": 103.0', '"total_usd": 999.0')
    elif kind == "truncate_bottom":
        lines = lines[:-1]
    elif kind == "truncate_top":
        lines = lines[1:]
    elif kind == "delete":
        lines = []
    elif kind == "relink_forgery":
        lines[2] = lines[2].replace('"total_usd": 103.0', '"total_usd": 999.0')
        lines = _relink(lines)
    recs.write_text("".join(ln + "\n" for ln in lines))
    assert not S.ledger_intact(str(recs), str(lr))
    # a legitimate append does NOT launder the tamper: the anchor moves to the new
    # tip but the detection is stamped STICKY in the anchor and on the appended line
    assert S.append_anchored(str(recs), {"type": "pipeline_halt", "date": "2025-02-01"}) is False
    assert read_bytes(lr) != anchor_before
    a = json.load(open(lr))
    assert a["ledger_tamper_since"] and a["ledger_tamper_events"] == 1
    assert S.load_recs(str(recs))[-1]["ledger_tamper"] is True
    assert not S.ledger_intact(str(recs), str(lr))
    # --adjust-flow refuses on a broken chain (exit 5)
    r = run("report.py", "--adjust-flow", "--effective-date", "2025-01-02", "--flow-usd", "5",
            "--reason", "x", state=state)
    assert r.returncode == 5, r.stderr
    # an OVERRIDE is suspended fail-safe
    monkeypatch.setattr(V, "_url_resolves", lambda u: True)
    from test_validate import good_override
    out = V.validate(make_baseline(), {"decision": "OVERRIDE", "final_allocation": {"QQQ": 100.0},
                                       "override": good_override(), "satellite": [], "vetoes": []},
                     str(recs), str(lr))
    assert out["verdict"] == "FAIL" and out["ledger_tamper"] is True
    assert any(v.startswith("LEDGER_TAMPER_DETECTED") for v in out["violations"])


# ------------------------------------------------------ install_cron.sh
@pytest.fixture
def fake_crontab(tmp_path):
    """A fake `crontab` first on PATH. It records every invocation; a mutating call
    (anything but -l) is recorded and refused. The real crontab is never reached."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    tab, log = tmp_path / "tab", tmp_path / "calls.log"
    tab.write_text("MAILTO=\"\"\n0 1 * * * /usr/bin/true # someone else's job\n")
    sh = bindir / "crontab"
    sh.write_text(f"""#!/usr/bin/env bash
echo "$*" >> "{log}"
if [ "$1" = "-l" ]; then cat "{tab}"; exit 0; fi
echo "fake crontab: refusing mutating call '$*'" >&2
exit 97
""")
    sh.chmod(sh.stat().st_mode | stat.S_IEXEC)
    return bindir, tab, log


@pytest.mark.parametrize("args", [[], ["--dry-run"]])
def test_install_cron_dry_run_changes_nothing(state, fake_crontab, args):
    bindir, tab, log = fake_crontab
    tab_before = read_bytes(tab)
    live_state_listing = sorted(os.listdir(os.path.join(C.SKILL_DIR, "state")))
    r = run("install_cron.sh", *args, state=state, path_prefix=bindir)
    assert r.returncode == 0, r.stderr
    assert "DRY RUN" in r.stdout
    assert "score_recs.py --score" in r.stdout and "flock -n" in r.stdout
    assert "# us-stock-advisor-score" in r.stdout
    assert read_bytes(tab) == tab_before
    calls = log.read_text().split("\n") if log.exists() else []
    assert [c for c in calls if c.strip()] == ["-l"]                 # read-only, exactly once
    assert sorted(os.listdir(os.path.join(C.SKILL_DIR, "state"))) == live_state_listing


def test_install_cron_rejects_unknown_option(state, fake_crontab):
    bindir, tab, log = fake_crontab
    r = run("install_cron.sh", "--instal", state=state, path_prefix=bindir)
    assert r.returncode == 2
    assert not log.exists() or not log.read_text().strip()
