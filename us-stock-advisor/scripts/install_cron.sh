#!/usr/bin/env bash
# us-stock-advisor v5.1 — nightly scorer cron installer.
#
# Install path: ~/.claude/skills/us-stock-advisor/scripts/install_cron.sh
#
#   install_cron.sh               DRY RUN (default): print the exact crontab line and
#                                 what would change. Touches nothing.
#   install_cron.sh --install     idempotent install: keep every existing crontab line,
#                                 drop any prior line carrying the marker, append one.
#   install_cron.sh --uninstall   remove the marker line, keep everything else.
#
# The schedule lives HERE and only here. 14:00 local (the machine is KST) is after
# the US close; Tue-Sat KST covers the Mon-Fri US sessions. The scorer is a full,
# idempotent recompute that only APPENDS (override_score lines), so a night skipped
# by laptop sleep is harmless. flock -n keeps two scorers from racing; the
# scorer itself also holds the ledger flock through every append + anchor write.
# The scorer exits 3 on a price-data outage (logged, nothing written).
set -euo pipefail

SCHEDULE="0 14 * * 2-6"
MARKER="# us-stock-advisor-score"

SK="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE="$SK/state"            # cron runs without US_ADVISOR_STATE -> the live state dir
PY="$(command -v python3 || true)"
FLOCK="$(command -v flock || true)"
if [[ -z "$PY" || -z "$FLOCK" ]]; then
    echo "ERROR: need python3 and flock on PATH (python3='$PY' flock='$FLOCK')" >&2
    exit 1
fi
# absolute path as resolved in the installing shell (not readlink -f: a python3.x
# symlink target would pin the minor version and break on the next upgrade)

# every path is shell-quoted for POSIX /bin/sh (which is what cron runs; bash's
# printf %q can emit $'..' that dash does not understand): a plain path is left
# as is, anything else is single-quoted like python's shlex.quote. cron treats an
# unescaped '%' as a newline, so a path containing '%' is refused outright.
for p in "$SK" "$PY" "$FLOCK"; do
    if [[ "$p" == *%* ]]; then
        echo "ERROR: path contains '%', which crontab cannot carry safely: $p" >&2
        exit 1
    fi
done
LOG="$STATE/score_cron.log"
q() {
    if [[ "$1" =~ ^[A-Za-z0-9_./+:@=,-]+$ ]]; then
        printf '%s' "$1"
    else
        printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
    fi
}
# log cap: before each run, a log over config.CRON_LOG_MAX_KIB is rotated to
# score_cron.log.1 (one generation kept), so the file stays bounded at about twice
# the cap forever. The number lives in config.py only; it is read through
# score_recs.py (which imports config) — never written here.
CAP_KIB="$("$PY" "$SK/scripts/score_recs.py" --cron-log-max-kib 2>/dev/null || true)"
if [[ ! "$CAP_KIB" =~ ^[0-9]+$ ]] || (( CAP_KIB <= 0 )); then
    echo "ERROR: could not read config.CRON_LOG_MAX_KIB via $SK/scripts/score_recs.py (got '$CAP_KIB')" >&2
    exit 1
fi
ROTATE="find $(q "$LOG") -size +${CAP_KIB}k -exec mv -f {} $(q "$LOG.1") ';' 2>/dev/null;"
LINE="$SCHEDULE $ROTATE $(q "$FLOCK") -n $(q "$STATE/.score.lock") $(q "$PY") $(q "$SK/scripts/score_recs.py") --score >> $(q "$LOG") 2>&1 $MARKER"

MODE="${1:-}"
case "$MODE" in
    ""|--dry-run) MODE="dry" ;;
    --install) MODE="install" ;;
    --uninstall) MODE="uninstall" ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "ERROR: unknown option '$MODE' (use --install, --uninstall, or no args for a dry run)" >&2; exit 2 ;;
esac

CURRENT="$(crontab -l 2>/dev/null || true)"
KEPT="$(printf '%s\n' "$CURRENT" | grep -vF -- "$MARKER" || true)"
PRIOR="$(printf '%s\n' "$CURRENT" | grep -F -- "$MARKER" || true)"

if [[ "$MODE" == "dry" ]]; then
    echo "DRY RUN — nothing installed. Re-run with --install to apply."
    echo "would install:"
    echo "  $LINE"
    if [[ -n "$PRIOR" ]]; then
        echo "would replace existing marker line(s):"
        printf '  %s\n' "$PRIOR"
    else
        echo "no existing marker line; would append 1 line"
    fi
    echo "other crontab lines preserved: $(printf '%s\n' "$KEPT" | grep -c . || true)"
    exit 0
fi

if [[ "$MODE" == "install" ]]; then
    mkdir -p "$STATE"
    { [[ -n "$KEPT" ]] && printf '%s\n' "$KEPT"; printf '%s\n' "$LINE"; } | crontab -
    echo "installed: $LINE"
else
    if [[ -n "$KEPT" ]]; then
        printf '%s\n' "$KEPT" | crontab -
    else
        crontab -r 2>/dev/null || true
    fi
    echo "uninstalled marker line(s): ${PRIOR:-none}"
fi
