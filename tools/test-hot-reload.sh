#!/bin/bash
# Test hot-reload during each pipeline stage.
#
# Strategy: start fresh, let the pipeline run, and inject a hot-reload
# when each target stage is active.  After each reload the pipeline
# restarts and re-processes, so we wait for the next target stage in
# the new cycle.
#
# Usage: tools/test-hot-reload.sh

set -euo pipefail

PORT=5151
BASE="http://localhost:$PORT"
LOG="/tmp/photonarium-test.log"
PASS=0
FAIL=0
CONFIDENCE=0.95  # toggled each cycle

# Stages to test (grep patterns that appear when the stage is active)
STAGES=(
    "Stage 1: Indexing"
    "Stage 2a: Generating thumbnails"
    "Stage 3a: Computing embeddings"
    "Stage 4a: NIMA scoring"
    "Stage 5: Detecting faces"
    "Stage 6: Running grouping"
)

red()   { printf '\033[1;31m%s\033[0m\n' "$*"; }
green() { printf '\033[1;32m%s\033[0m\n' "$*"; }
bold()  { printf '\033[1m%s\033[0m\n' "$*"; }

wait_for_log() {
    # Wait for a pattern to appear in the log after a given line number.
    # Args: pattern start_line timeout_s
    local pattern="$1" start_line="$2" timeout="${3:-120}"
    local deadline=$((SECONDS + timeout))
    while [ $SECONDS -lt $deadline ]; do
        if tail -n +"$start_line" "$LOG" 2>/dev/null | grep -aq "$pattern"; then
            return 0
        fi
        sleep 0.5
    done
    return 1
}

get_log_lines() {
    wc -l < "$LOG" 2>/dev/null || echo 0
}

toggle_confidence() {
    # Toggle confidence value so there's always an actual change.
    # Called in the parent shell BEFORE hot_reload (which runs in a
    # subshell via $(...) and can't propagate variable changes back).
    if [ "$CONFIDENCE" = "0.95" ]; then
        CONFIDENCE="0.90"
    else
        CONFIDENCE="0.95"
    fi
}

hot_reload() {
    # Save to disk
    curl -sf -X POST "$BASE/api/config/save" \
        -H 'Content-Type: application/json' \
        -d "{\"values\": {\"face_detection_min_confidence\": $CONFIDENCE}}" > /dev/null

    # Hot-reload
    curl -sf -X POST "$BASE/api/config/hot-reload" \
        -H 'Content-Type: application/json' \
        -d '{"changed_fields": ["face_detection_min_confidence"]}' 2>&1 || true
}

check_no_errors() {
    local start_line="$1"
    if tail -n +"$start_line" "$LOG" | grep -aqiE 'Traceback|ERROR.*Hot-reload|ERROR.*pipeline'; then
        return 1
    fi
    return 0
}

# ── Setup ──────────────────────────────────────────────────────────
bold "=== Hot-Reload Stress Test ==="
echo ""

# Stop any existing instance and wipe
"$(dirname "$0")/test-stop.sh" --wipe 2>/dev/null || true
lsof -ti :"$PORT" 2>/dev/null | xargs -r kill -9 2>/dev/null || true
sleep 1
> "$LOG"

# Start fresh with example images + videos
"$(dirname "$0")/test-start.sh" --tutorial \
    --folders "$(dirname "$0")/mktutorial/examples,$(dirname "$0")/mktutorial/videos" \
    2>&1

echo ""
sleep 2

# ── Run tests ──────────────────────────────────────────────────────
for stage_pattern in "${STAGES[@]}"; do
    bold "--- Testing hot-reload during: $stage_pattern ---"
    before=$(get_log_lines)

    # Wait for the target stage to appear in the log
    if ! wait_for_log "$stage_pattern" "$before" 300; then
        red "FAIL: $stage_pattern never started (timeout 300s)"
        FAIL=$((FAIL + 1))
        # Try to continue — the pipeline may have finished all stages
        continue
    fi

    # Small delay to ensure we're mid-stage, not just at the log line
    sleep 1

    reload_before=$(get_log_lines)
    toggle_confidence
    bold "  Sending hot-reload (confidence=$CONFIDENCE)..."
    result=$(hot_reload)

    if echo "$result" | python3 -c "import sys,json; sys.exit(0 if json.load(sys.stdin).get('success') else 1)" 2>/dev/null; then
        # Check that the pipeline stopped and restarted
        if wait_for_log "all threads restarted" "$reload_before" 180; then
            # Check for errors
            if check_no_errors "$reload_before"; then
                green "  PASS: clean hot-reload during $stage_pattern"
                PASS=$((PASS + 1))
            else
                red "  FAIL: errors after hot-reload during $stage_pattern"
                tail -n +"$reload_before" "$LOG" | grep -aiE 'Traceback|ERROR' | head -5
                FAIL=$((FAIL + 1))
            fi
        else
            red "  FAIL: threads did not restart after hot-reload during $stage_pattern"
            FAIL=$((FAIL + 1))
        fi
    else
        red "  FAIL: hot-reload returned error during $stage_pattern"
        echo "  $result"
        FAIL=$((FAIL + 1))
    fi

    echo ""
done

# ── Wait for final pipeline cycle to complete ──────────────────────
bold "--- Waiting for final pipeline cycle to complete ---"
# After the last hot-reload, the pipeline restarts.  Wait for it to
# finish at least one full cycle by counting completions.
completions_before=$(grep -c "Pipeline cycle complete" "$LOG" 2>/dev/null || echo 0)
target=$((completions_before + 1))
deadline=$((SECONDS + 300))
while [ $SECONDS -lt $deadline ]; do
    completions=$(grep -c "Pipeline cycle complete" "$LOG" 2>/dev/null || echo 0)
    if [ "$completions" -ge "$target" ]; then
        break
    fi
    sleep 2
done
completions=$(grep -c "Pipeline cycle complete" "$LOG" 2>/dev/null || echo 0)
if [ "$completions" -ge "$target" ]; then
    green "  Pipeline completed successfully after all hot-reloads"

    # Check final face count
    faces=$(curl -sf "$BASE/api/faces?unknown=false" | python3 -c "
import sys, json
data = json.load(sys.stdin)['data']
print(len(data))
" 2>/dev/null || echo "?")
    echo "  Final face count: $faces"
else
    red "  Pipeline did not complete within 300s"
    FAIL=$((FAIL + 1))
fi

# ── Summary ────────────────────────────────────────────────────────
echo ""
bold "=== Results ==="
green "  Passed: $PASS"
if [ $FAIL -gt 0 ]; then
    red "  Failed: $FAIL"
else
    echo "  Failed: 0"
fi

# Stop
"$(dirname "$0")/test-stop.sh" 2>/dev/null || true

exit $FAIL
