#!/bin/bash
#
# daily-sync.sh — runs inside the cron container.
# Sleeps until the configured time, then runs the sync loop daily.
#
# Environment variables:
#   SYNC_HOUR   — hour to run (0-23, default: 8)
#   SYNC_MINUTE — minute to run (0-59, default: 0)
#   SHARED_FOLDER — optional path to copy reports to after sync
#

set -e
cd /app
if [ -d .venv ]; then
    source scripts/env.sh
else
    # The Docker image installs dependencies into the system Python and
    # ships neither .venv nor scripts/.
    py() { python "$@"; }
fi

HOUR="${SYNC_HOUR:-8}"
MINUTE="${SYNC_MINUTE:-0}"

echo "Expense Tracker daily sync"
echo "Schedule: every day at ${HOUR}:$(printf '%02d' "${MINUTE}")"
echo "Timezone: ${TZ:-UTC}"
echo ""

sync_once() {
    # A step that fails must neither end the script (set -e) nor pass for a
    # success: the other steps still run, the loop goes on to the next day,
    # and the log says ERROR.
    local failed=0

    echo "=== Sync started at $(date) ==="

    # 1. Fetch new statements from email (if configured)
    if [ -f "data/email-config.json" ]; then
        echo "[1/4] Fetching statements from email..."
        if ! py bank_ingest.py --quiet fetch; then
            echo "  ERROR: fetch failed, see the messages above (will retry tomorrow)"
            failed=1
        fi
    else
        echo "[1/4] Skipping email fetch (no email-config.json found)"
    fi

    # 2. Ingest, categorize, and regenerate ODS report
    echo "[2/4] Running auto ingest + report..."
    if ! py bank_ingest.py --quiet auto; then
        # Exits 1 when a statement could not be imported: the others are in
        echo "  ERROR: auto failed, see the messages above"
        failed=1
    fi

    # 3. Generate monthly PDF
    echo "[3/4] Generating PDF report..."
    if ! py bank_ingest.py --quiet pdf; then
        echo "  ERROR: pdf failed, see the messages above"
        failed=1
    fi

    # 4. Copy to shared folder (if configured)
    if [ -n "${SHARED_FOLDER}" ] && [ -d "${SHARED_FOLDER}" ]; then
        echo "[4/4] Copying reports to shared folder..."
        cp -f expense-report.ods "${SHARED_FOLDER}/" 2>/dev/null || true
        mkdir -p "${SHARED_FOLDER}/reports"
        cp -f reports/*.pdf "${SHARED_FOLDER}/reports/" 2>/dev/null || true
        echo "  Reports copied to ${SHARED_FOLDER}"
    else
        echo "[4/4] No shared folder configured, skipping copy"
    fi

    if [ "$failed" -ne 0 ]; then
        echo "=== Sync finished WITH ERRORS at $(date) ==="
    else
        echo "=== Sync complete at $(date) ==="
    fi
    echo ""
}

# Run once immediately on container start
sync_once

# Then loop: sleep until next scheduled time, run, repeat
while true; do
    # Calculate seconds until next run
    now=$(date +%s)
    target=$(date -d "today ${HOUR}:$(printf '%02d' "${MINUTE}")" +%s 2>/dev/null || \
             date -j -f "%H:%M" "${HOUR}:$(printf '%02d' "${MINUTE}")" +%s 2>/dev/null)

    if [ "$target" -le "$now" ]; then
        # Already passed today, schedule for tomorrow
        target=$((target + 86400))
    fi

    sleep_secs=$((target - now))
    next_time=$(date -d "@${target}" 2>/dev/null || date -r "${target}" 2>/dev/null || echo "unknown")
    echo "Next sync in $(( sleep_secs / 3600 ))h $(( (sleep_secs % 3600) / 60 ))m (at ${next_time})"
    sleep "$sleep_secs"

    sync_once
done
