#!/usr/bin/env bash
# us-stock-advisor v5.1 — one-command acceptance run (offline).
#   bash tests/run_acceptance.sh            # pytest suite + SKILL.md greps 1/7/9
#   US_ADVISOR_NETWORK_TESTS=1 bash tests/run_acceptance.sh   # + yfinance backtests
# State is redirected to a throw-away dir; the suite refuses to run against the live
# state/ and fails if the live ledger/anchor change during the session.
set -euo pipefail
SK="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONDONTWRITEBYTECODE=1
export US_ADVISOR_STATE="$(mktemp -d -t usadv_accept_XXXXXX)"
trap 'rm -rf "$US_ADVISOR_STATE"' EXIT
cd "$SK"

python3 -m pytest -p no:cacheprovider tests/ -q "$@"

echo "--- acceptance 1: general-purpose invocation grep (only the DELETE-list line may appear)"
grep -nE 'subagent_type\s*[=:]\s*"?general-purpose' SKILL.md || true
echo "--- acceptance 7: comparator + digit lines (historical/schema prose only)"
grep -nP '(?<![A-Z_])(>=|<=|≥|≤|>|<|=)\s*[0-9]' SKILL.md || true
echo "--- acceptance 9: slack lines"
grep -n -i slack SKILL.md
