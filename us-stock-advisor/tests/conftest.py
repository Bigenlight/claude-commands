"""us-stock-advisor v5.1 test harness.

SAFETY FIRST. config.STATE_DIR is resolved at import time from US_ADVISOR_STATE, so
this file points it at a fresh temp dir BEFORE any script module is imported, fails
fast if it would still resolve to the live state dir, and asserts at session end that
the live ledger and anchor are byte-identical to what they were at session start.

Run (offline, one command, from the skill dir):
    PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/ -q
Opt-in network tests (yfinance): US_ADVISOR_NETWORK_TESTS=1
"""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile

SK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(SK, "scripts")
LIVE_STATE = os.path.realpath(os.path.join(SK, "state"))

# --- 1. redirect state BEFORE importing any script (never trust an inherited value)
_SESSION_STATE = tempfile.mkdtemp(prefix="usadv_test_state_")
os.environ["US_ADVISOR_STATE"] = _SESSION_STATE
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.dont_write_bytecode = True

for _m in ("config", "core", "validate", "report", "score_recs"):
    if _m in sys.modules:
        raise RuntimeError(f"{_m} was imported before the test harness redirected "
                           "US_ADVISOR_STATE; refusing to run (it may point at live state)")
sys.path.insert(0, SCRIPTS)
import config as C  # noqa: E402


def _inside(path, root):
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root + os.sep)


if _inside(C.STATE_DIR, LIVE_STATE) or _inside(C.RECS_JSONL, LIVE_STATE):
    raise RuntimeError(f"config.STATE_DIR resolves to the LIVE state dir ({C.STATE_DIR}); abort")

import pytest  # noqa: E402

_LIVE_FILES = ("recommendations.jsonl", "last_run.json")


def _sha(p):
    if not os.path.exists(p):
        return None
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


_LIVE_HASHES_AT_START = {f: _sha(os.path.join(LIVE_STATE, f)) for f in _LIVE_FILES}


@pytest.fixture(scope="session", autouse=True)
def _live_state_guard():
    yield
    after = {f: _sha(os.path.join(LIVE_STATE, f)) for f in _LIVE_FILES}
    assert after == _LIVE_HASHES_AT_START, (
        f"LIVE STATE CHANGED DURING THE TEST SESSION: {_LIVE_HASHES_AT_START} -> {after}")


@pytest.fixture
def state(tmp_path, monkeypatch):
    """A fresh, empty state dir for one test: in-process config paths are patched and
    subprocesses inherit US_ADVISOR_STATE pointing at it."""
    d = tmp_path / "state"
    d.mkdir()
    assert not _inside(str(d), LIVE_STATE)
    monkeypatch.setattr(C, "STATE_DIR", str(d))
    monkeypatch.setattr(C, "RECS_JSONL", str(d / "recommendations.jsonl"))
    monkeypatch.setattr(C, "SCORECARD_CSV", str(d / "scorecard.csv"))
    monkeypatch.setattr(C, "BASELINE_JSON", str(d / "baseline_plan.json"))
    monkeypatch.setattr(C, "LAST_RUN_JSON", str(d / "last_run.json"))
    monkeypatch.setenv("US_ADVISOR_STATE", str(d))
    return d


def pytest_configure(config):
    config.addinivalue_line("markers", "network: needs yfinance/network (opt-in: "
                                       "US_ADVISOR_NETWORK_TESTS=1)")


def pytest_collection_modifyitems(config, items):
    if os.environ.get("US_ADVISOR_NETWORK_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="network test; set US_ADVISOR_NETWORK_TESTS=1 to run")
    for it in items:
        if "network" in it.keywords:
            it.add_marker(skip)
