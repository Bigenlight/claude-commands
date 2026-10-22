"""Team 4 — SKILL.md consistency tests (us-stock-advisor v6.0).

Offline. Reads SKILL.md, imports config (state already redirected by conftest), and runs each
referenced Python script with --help (argparse prints and exits; nothing is written).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
SKILL_MD = SKILL_DIR / "SKILL.md"
SCRIPTS = SKILL_DIR / "scripts"

ALLOWED_SUBAGENTS = {"stock-news-researcher", "stock-advisor-analyst"}
SLACK_DM = "U0AD7V4SWD9"
FLAG_RE = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]*")
SCRIPT_RE = re.compile(r"\b([a-z_][a-z0-9_]*)\.(py|sh)\b")
# A script's flags on a line run until the next command separator / code-span end / next script.
SEGMENT_END_RE = re.compile(r"`|;|&&|\|\||\s\|\s|→")


def _text() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


def _frontmatter(text: str) -> dict[str, str]:
    assert text.startswith("---\n"), "SKILL.md must start with YAML frontmatter"
    end = text.index("\n---\n", 4)
    out = {}
    for line in text[4:end].splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def _config():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import config  # noqa: PLC0415  (conftest has already redirected US_ADVISOR_STATE)
    return config


# ------------------------------------------------------------------------------------------ 31
def test_skill_frontmatter_version_and_tools():
    text = _text()
    fm = _frontmatter(text)
    assert fm.get("name") == "us-stock-advisor"
    assert fm.get("version") == "6.0.0"
    assert fm.get("version") == _config().VERSION
    tools = {t.strip() for t in fm["allowed-tools"].strip("[]").split(",")}
    assert not tools & {"WebSearch", "WebFetch"}, "the top-level never researches; agents do"
    assert {"Agent", "Bash", "Write", "ToolSearch"} <= tools
    assert "last" in fm.get("argument-hint", "")

    types = re.findall(r"subagent_type:\s*\"([^\"]+)\"", text)
    assert types, "no Agent calls found"
    assert set(types) <= ALLOWED_SUBAGENTS, set(types) - ALLOWED_SUBAGENTS
    assert set(types) == ALLOWED_SUBAGENTS
    assert "general-purpose" not in types
    # 4 scouts + 1 clerk + 3 deciders
    assert types.count("stock-news-researcher") == 4
    assert types.count("stock-advisor-analyst") == 4

    # The Slack DM id appears, and only inside the Slack delivery section.
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if re.match(r"^#+ .*Slack delivery", ln))
    level = len(lines[start]) - len(lines[start].lstrip("#"))
    end = next((i for i in range(start + 1, len(lines))
                if re.match(r"^#{1,%d} " % level, lines[i])), len(lines))
    section = "\n".join(lines[start:end])
    assert section.count(SLACK_DM) >= 1
    assert text.count(SLACK_DM) == section.count(SLACK_DM), "Slack id leaked outside the Slack section"
    assert re.search(r"DO NOT ASK", lines[start])


# ------------------------------------------------------------------------------------------ 32
def _known_flags(name: str, tmp_path: Path) -> set[str]:
    path = SCRIPTS / name
    assert path.exists(), f"SKILL.md references scripts/{name}, which does not exist"
    if name.endswith(".sh"):
        return set(FLAG_RE.findall(path.read_text(encoding="utf-8")))
    env = dict(os.environ, US_ADVISOR_STATE=str(tmp_path / "state"), PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run([sys.executable, str(path), "--help"], capture_output=True, text=True,
                          timeout=120, env=env, cwd=str(tmp_path))
    assert proc.returncode == 0, f"{name} --help failed: {proc.stderr[-500:]}"
    return set(FLAG_RE.findall(proc.stdout))


def test_skill_references_only_existing_scripts_and_flags(tmp_path):
    text = _text()
    scripts = {f"{m.group(1)}.{m.group(2)}" for m in SCRIPT_RE.finditer(text)}
    assert {"book.py", "fetch_indicators.py", "ledger.py", "aggregate.py", "render.py",
            "watchdog.py", "install_timer.sh"} <= scripts
    known = {s: _known_flags(s, tmp_path) for s in sorted(scripts)}

    # 1) flags written after a script on the same command must exist in THAT script.
    bad = []
    for ln in text.splitlines():
        hits = list(SCRIPT_RE.finditer(ln))
        for k, m in enumerate(hits):
            stop = hits[k + 1].start() if k + 1 < len(hits) else len(ln)
            seg = ln[m.end():stop]
            cut = SEGMENT_END_RE.search(seg)
            if cut:
                seg = seg[:cut.start()]
            name = f"{m.group(1)}.{m.group(2)}"
            for flag in FLAG_RE.findall(seg):
                if flag not in known[name]:
                    bad.append(f"{name} {flag}  <- {ln.strip()[:100]}")
    assert not bad, "flags not in the script's argparse/help:\n" + "\n".join(bad)

    # 2) every flag mentioned anywhere (prose, argument-hint) belongs to some referenced script.
    union = set().union(*known.values())
    orphan = sorted(set(FLAG_RE.findall(text)) - union)
    assert not orphan, f"flags mentioned in SKILL.md that no script accepts: {orphan}"


# ------------------------------------------------------------------------------------------ 33
def test_skill_enum_matches_config_and_no_hardcoded_caps():
    text = _text()
    C = _config()

    def present(token: str) -> bool:
        return re.search(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(token), text) is not None

    enums = {"REASON_CODES": [c for codes in C.REASON_CODES.values() for c in codes],
             "KILL_TYPES": list(C.KILL_TYPES), "EVENT_TYPES": list(C.EVENT_TYPES),
             "DECIDER_IDS": list(C.DECIDER_IDS), "SCOUT_IDS": list(C.SCOUT_IDS),
             "MATERIALITY": list(C.MATERIALITY), "AGE_TAGS": list(C.AGE_TAGS)}
    missing = {k: [t for t in v if not present(t)] for k, v in enums.items()}
    assert not any(missing.values()), missing

    # The closed enum is copied verbatim: each code tuple appears, in config order, on one line.
    lines = text.splitlines()
    for name in ("ENTRY_CODES", "EXIT_CODES", "DERISK_CODES", "EVIDENCE_REQUIRED_CODES", "RS_FLAG_CODES"):
        joined = ", ".join(getattr(C, name))
        assert any(joined in ln for ln in lines), f"{name} not listed verbatim: {joined}"

    # No sleeve/SGOV cap literal (user-overridden or spec values) and no cap percentages.
    caps = re.findall(r"(?<![\d.])0?\.(?:35|20|50|25|30|60)(?!\d)", text)
    pcts = re.findall(r"(?<![\d.])(?:35|20|50|25|30|60|15)\s?%", text)
    assert not caps, f"hardcoded cap-like fractions in SKILL.md: {caps}"
    assert not pcts, f"hardcoded cap-like percentages in SKILL.md: {pcts}"
    for key in ("sleeve_max_pct", "sleeve_position_max_pct", "sleeve_max_positions", "sgov_max_pct"):
        assert "{caps.%s}" % key in text, f"decider prompt must interpolate caps_in_effect.{key}"

    # Every capitalised placeholder names a real config constant.
    for ph in set(re.findall(r"\{([A-Z][A-Z0-9_]+)\}", text)):
        assert hasattr(C, ph), f"placeholder {{{ph}}} is not a config constant"
