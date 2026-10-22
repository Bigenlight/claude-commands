"""Team 3 — install_timer.sh: dry run is the default and touches nothing.

Never runs --install/--uninstall. HOME and SYSTEMD_USER_DIR both point into tmp so even
a regression could not reach ~/.config/systemd/user.
"""
from __future__ import annotations

import os
import subprocess

import config as C

SK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(SK, "scripts", "install_timer.sh")


def _run(args, tmp_path, unit_dir=None):
    env = dict(os.environ, HOME=str(tmp_path / "home"),
               SYSTEMD_USER_DIR=str(unit_dir or tmp_path / "units"), PYTHONDONTWRITEBYTECODE="1")
    env.pop("US_ADVISOR_STATE", None)      # the unit must not depend on it
    return subprocess.run(["bash", SCRIPT, *args], capture_output=True, text=True, env=env, timeout=60)


def _tree(p):
    return sorted(os.path.relpath(os.path.join(r, f), p) for r, _, fs in os.walk(p) for f in fs) \
        if os.path.exists(p) else []


def test_install_timer_dry_run_touches_nothing(tmp_path):
    (tmp_path / "units").mkdir()
    (tmp_path / "home").mkdir()
    for args in ([], ["--dry-run"]):
        p = _run(args, tmp_path)
        assert p.returncode == 0, p.stderr
        out = p.stdout
        assert "DRY RUN" in out
        assert "Persistent=true" in out
        assert f"OnCalendar={C.WATCHDOG_ONCALENDAR}" in out
        assert "OnCalendar=*-*-* 07:30:00 Asia/Seoul" in out
        assert "Type=oneshot" in out and "RandomizedDelaySec=300" in out
        assert f"{SK}/scripts/watchdog.py" in out and "Environment=PYTHONDONTWRITEBYTECODE=1" in out
        assert "US_ADVISOR_STATE" not in out
        assert f"{C.SYSTEMD_UNIT}.service" in out and f"{C.SYSTEMD_UNIT}.timer" in out
        assert f"systemctl --user enable --now {C.SYSTEMD_UNIT}.timer" in out
        assert "WantedBy=timers.target" in out
    assert _tree(str(tmp_path / "units")) == [] and _tree(str(tmp_path / "home")) == []


def test_install_timer_rejects_bad_options_and_paths(tmp_path):
    p = _run(["--bogus"], tmp_path)
    assert p.returncode == 2 and "unknown option" in p.stderr
    p = _run(["--dry-run", "--install"], tmp_path)
    assert p.returncode == 2
    p = _run([], tmp_path, unit_dir=tmp_path / "bad%dir")
    assert p.returncode == 1 and "%" in p.stderr
    assert not (tmp_path / "bad%dir").exists()
    p = _run([], tmp_path, unit_dir=tmp_path / "with space")
    assert p.returncode == 1 and not (tmp_path / "with space").exists()
    p = _run(["--help"], tmp_path)
    assert p.returncode == 0 and "--install" in p.stdout
