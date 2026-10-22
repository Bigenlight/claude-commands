"""us-stock-advisor v6.0 test harness.

SAFETY FIRST. config.STATE_DIR is resolved at import time from US_ADVISOR_STATE, so
this file points it at a fresh temp dir BEFORE any script module is imported, fails
fast if it would still resolve to the live state dir, and asserts at session end that
the live state/ directory (listing + sha256 of ledger.jsonl) is exactly what it was at
session start. The live state/ dir does not exist before the first real run; absence
is a valid, unchanged state.

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
TESTS = os.path.join(SK, "tests")
LIVE_STATE = os.path.realpath(os.path.join(SK, "state"))

# --- 1. redirect state BEFORE importing any script (never trust an inherited value)
_SESSION_STATE = tempfile.mkdtemp(prefix="usadv6_test_state_")
os.environ["US_ADVISOR_STATE"] = _SESSION_STATE
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.dont_write_bytecode = True

SCRIPT_MODULES = ("config", "book", "fetch_indicators", "aggregate", "render", "ledger", "watchdog")
for _m in SCRIPT_MODULES:
    if _m in sys.modules:
        raise RuntimeError(f"{_m} was imported before the test harness redirected "
                           "US_ADVISOR_STATE; refusing to run (it may point at live state)")
for _p in (TESTS, SCRIPTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import config as C  # noqa: E402


def _inside(path, root):
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root + os.sep)


for _attr in ("STATE_DIR", "LEDGER_JSONL", "LEDGER_LOCK", "RUNS_DIR", "TRIGGERS_JSON"):
    if _inside(getattr(C, _attr), LIVE_STATE):
        raise RuntimeError(f"config.{_attr} resolves inside the LIVE state dir "
                           f"({getattr(C, _attr)}); abort")

import pytest  # noqa: E402


def _sha(p):
    if not os.path.isfile(p):
        return None
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _live_snapshot():
    """(exists, sorted relative listing, sha256 of ledger.jsonl). Absent dir -> (False, (), None)."""
    if not os.path.exists(LIVE_STATE):
        return (False, (), None)
    listing = []
    for root, dirs, files in os.walk(LIVE_STATE):
        dirs.sort()
        for name in sorted(dirs + files):
            listing.append(os.path.relpath(os.path.join(root, name), LIVE_STATE))
    return (True, tuple(sorted(listing)), _sha(os.path.join(LIVE_STATE, "ledger.jsonl")))


_LIVE_AT_START = _live_snapshot()


@pytest.fixture(scope="session", autouse=True)
def _live_state_guard():
    yield
    after = _live_snapshot()
    assert after == _LIVE_AT_START, (
        f"LIVE STATE CHANGED DURING THE TEST SESSION: {_LIVE_AT_START} -> {after}")


def patch_state(monkeypatch, d):
    """Point every config path at state dir `d` (in-process) and export it to subprocesses."""
    d = str(d)
    assert not _inside(d, LIVE_STATE)
    monkeypatch.setattr(C, "STATE_DIR", d)
    monkeypatch.setattr(C, "LEDGER_JSONL", os.path.join(d, "ledger.jsonl"))
    monkeypatch.setattr(C, "LEDGER_LOCK", os.path.join(d, "ledger.lock"))
    monkeypatch.setattr(C, "RUNS_DIR", os.path.join(d, "run"))
    monkeypatch.setattr(C, "TRIGGERS_JSON", os.path.join(d, "triggers.json"))
    monkeypatch.setattr(C, "REPORTS_DIR", os.path.join(d, "reports"))
    monkeypatch.setenv("US_ADVISOR_STATE", d)
    return d


@pytest.fixture
def state(tmp_path, monkeypatch):
    """A fresh, empty state dir for one test: in-process config paths are patched and
    subprocesses inherit US_ADVISOR_STATE pointing at it. Returns a pathlib.Path."""
    d = tmp_path / "state"
    d.mkdir()
    patch_state(monkeypatch, d)
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
