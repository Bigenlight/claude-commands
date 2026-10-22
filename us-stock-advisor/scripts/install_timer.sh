#!/usr/bin/env bash
# us-stock-advisor v6.1 — daily watchdog systemd USER timer installer.
#
# Install path: ~/.claude/skills/us-stock-advisor/scripts/install_timer.sh
#
#   install_timer.sh               DRY RUN (default): print both unit files and the exact
#   install_timer.sh --dry-run     systemctl commands. Touches nothing.
#   install_timer.sh --install     write the units, daemon-reload, enable --now the timer.
#   install_timer.sh --uninstall   disable --now the timer, remove both units, daemon-reload.
#   install_timer.sh --status      systemctl --user list-timers <unit>.timer
#
# Why a systemd user timer and not cron: no crontab here and Linger=no, so a sleeping
# laptop would silently skip runs; Persistent=true runs a missed slot on wake.
# The schedule (OnCalendar) and unit name live in config.py only and are read from it.
# The service runs WITHOUT US_ADVISOR_STATE, i.e. against the live state dir.
# SYSTEMD_USER_DIR overrides ~/.config/systemd/user (tests).
set -euo pipefail

SK="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${SYSTEMD_USER_DIR:-$HOME/.config/systemd/user}"
PY="$(command -v python3 || true)"
if [[ -z "$PY" ]]; then
    echo "ERROR: python3 not on PATH" >&2
    exit 1
fi

MODE="${1:-}"
if [[ $# -gt 1 ]]; then
    echo "ERROR: one option at most (got: $*)" >&2
    exit 2
fi
case "$MODE" in
    ""|--dry-run) MODE="dry" ;;
    --install) MODE="install" ;;
    --uninstall) MODE="uninstall" ;;
    --status) MODE="status" ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
    *) echo "ERROR: unknown option '$MODE' (use --dry-run, --install, --uninstall, --status)" >&2; exit 2 ;;
esac

# systemd treats '%' as a specifier, a newline breaks the unit file, and quotes/backslashes/
# spaces need escaping that differs per directive (WorkingDirectory= does not unquote)
# -> refuse them outright rather than risk a wrong unit.
for p in "$SK" "$PY" "$UNIT_DIR"; do
    if [[ "$p" == *$'\n'* || "$p" == *%* || "$p" == *\"* || "$p" == *\\* || "$p" == *" "* ]]; then
        echo "ERROR: path contains a newline, space, '%', '\"' or '\\', which a unit file cannot carry safely: $p" >&2
        exit 1
    fi
done

CFG="$(cd "$SK/scripts" && PYTHONDONTWRITEBYTECODE=1 "$PY" -c \
    'import config as C; print(C.SYSTEMD_UNIT); print(C.WATCHDOG_ONCALENDAR)' 2>/dev/null || true)"
UNIT="$(printf '%s\n' "$CFG" | sed -n 1p)"
ONCAL="$(printf '%s\n' "$CFG" | sed -n 2p)"
if [[ ! "$UNIT" =~ ^[A-Za-z0-9_.-]+$ || -z "$ONCAL" || "$ONCAL" == *%* ]]; then
    echo "ERROR: could not read SYSTEMD_UNIT / WATCHDOG_ONCALENDAR from $SK/scripts/config.py (got '$UNIT' / '$ONCAL')" >&2
    exit 1
fi

q() { printf '%s' "$1"; }   # paths are pre-validated (no space/quote/backslash/%/newline)

SERVICE_FILE="$UNIT_DIR/$UNIT.service"
TIMER_FILE="$UNIT_DIR/$UNIT.timer"

SERVICE="[Unit]
Description=us-stock-advisor daily watchdog (marks, kill checks, triggers.json)

[Service]
Type=oneshot
WorkingDirectory=$(q "$SK")
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=$(q "$PY") $(q "$SK/scripts/watchdog.py")
TimeoutStartSec=900
Nice=10
"

TIMER="[Unit]
Description=us-stock-advisor daily watchdog timer

[Timer]
OnCalendar=$ONCAL
Persistent=true
RandomizedDelaySec=300
Unit=$UNIT.service

[Install]
WantedBy=timers.target
"

ENABLE_CMD="systemctl --user daemon-reload && systemctl --user enable --now $UNIT.timer"

case "$MODE" in
dry)
    echo "DRY RUN — nothing written, nothing enabled. Re-run with --install to apply."
    echo
    echo "# would write: $SERVICE_FILE"
    printf '%s' "$SERVICE"
    echo
    echo "# would write: $TIMER_FILE"
    printf '%s' "$TIMER"
    echo
    echo "# then run:"
    echo "$ENABLE_CMD"
    if [[ -e "$SERVICE_FILE" || -e "$TIMER_FILE" ]]; then
        echo "# note: unit file(s) already present and would be overwritten"
    fi
    exit 0
    ;;
install)
    mkdir -p "$UNIT_DIR"
    printf '%s' "$SERVICE" > "$SERVICE_FILE.tmp"
    mv -f "$SERVICE_FILE.tmp" "$SERVICE_FILE"
    printf '%s' "$TIMER" > "$TIMER_FILE.tmp"
    mv -f "$TIMER_FILE.tmp" "$TIMER_FILE"
    systemctl --user daemon-reload
    systemctl --user enable --now "$UNIT.timer"
    echo "installed: $SERVICE_FILE"
    echo "installed: $TIMER_FILE"
    systemctl --user list-timers "$UNIT.timer" --no-pager || true
    ;;
uninstall)
    systemctl --user disable --now "$UNIT.timer" 2>/dev/null || true
    rm -f "$SERVICE_FILE" "$TIMER_FILE"
    systemctl --user daemon-reload
    echo "uninstalled: $SERVICE_FILE $TIMER_FILE"
    ;;
status)
    systemctl --user list-timers "$UNIT.timer" --no-pager
    ;;
esac
