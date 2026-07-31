#!/bin/bash
# Daily Unbooked-Call Analysis — cron wrapper.
# Sources creds, runs the pipeline for yesterday, delivers the brief via SendGrid.
# Prints a one-line status (delivered to Ryan as a heads-up; email is the real deliverable).
set -uo pipefail

APP=/workspace/apps/lex-servicetitan-reporting
PY="$APP/.venv/bin/python"

set -a
# shellcheck disable=SC1091
. /workspace/.secrets/hermes.env 2>/dev/null
. /workspace/context/company/secrets/sendgrid.env 2>/dev/null
set +a

cd "$APP" || exit 1
OUT="$("$PY" -m unbooked_analysis.run_daily --tenant lex_portfolio 2>&1)"
RC=$?

SUBJECT_LINE="$(printf '%s\n' "$OUT" | grep -E '^\s*SUBJECT:' | sed -E 's/^\s*SUBJECT:\s*//')"
SEND_LINE="$(printf '%s\n' "$OUT" | grep -E 'SendGrid:' | tail -1)"

if [ $RC -ne 0 ]; then
    echo "🔴 Unbooked-call brief FAILED (rc=$RC):"
    printf '%s\n' "$OUT" | tail -15
    exit $RC
fi

if printf '%s' "$SEND_LINE" | grep -q "'ok': True"; then
    echo "✅ Unbooked-call brief emailed to Ryan — ${SUBJECT_LINE:-(no subject parsed)}"
else
    echo "⚠️ Unbooked-call pipeline ran but delivery unconfirmed:"
    printf '%s\n' "$OUT" | tail -10
fi
