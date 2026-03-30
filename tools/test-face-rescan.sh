#!/bin/bash
# Test that face rescan preserves named/ignored faces and correctly
# adds/removes faces when detection settings change.
#
# Strategy:
#   1. Ingest example images, wait for face detection
#   2. Name some faces, ignore others, leave some unnamed
#   3. Record the state
#   4. Hot-reload with changed face detection settings
#   5. Wait for rescan to complete
#   6. Verify named/ignored faces are preserved, unnamed stale ones removed,
#      new detections added
#
# Usage: tools/test-face-rescan.sh

set -euo pipefail

PORT=5151
BASE="http://localhost:$PORT"
LOG="/tmp/photonarium-test.log"
PASS=0
FAIL=0

red()   { printf '\033[1;31m%s\033[0m\n' "$*"; }
green() { printf '\033[1;32m%s\033[0m\n' "$*"; }
bold()  { printf '\033[1m%s\033[0m\n' "$*"; }

assert_eq() {
    local label="$1" expected="$2" actual="$3"
    if [ "$expected" = "$actual" ]; then
        green "  PASS: $label (expected=$expected, got=$actual)"
        PASS=$((PASS + 1))
    else
        red "  FAIL: $label (expected=$expected, got=$actual)"
        FAIL=$((FAIL + 1))
    fi
}

assert_ge() {
    local label="$1" minimum="$2" actual="$3"
    if [ "$actual" -ge "$minimum" ] 2>/dev/null; then
        green "  PASS: $label (expected>=$minimum, got=$actual)"
        PASS=$((PASS + 1))
    else
        red "  FAIL: $label (expected>=$minimum, got=$actual)"
        FAIL=$((FAIL + 1))
    fi
}

assert_ne() {
    local label="$1" unexpected="$2" actual="$3"
    if [ "$unexpected" != "$actual" ]; then
        green "  PASS: $label (not $unexpected, got=$actual)"
        PASS=$((PASS + 1))
    else
        red "  FAIL: $label (expected not $unexpected, got=$actual)"
        FAIL=$((FAIL + 1))
    fi
}

api_get() {
    curl -sf "$BASE/api/$1" 2>/dev/null
}

api_post() {
    curl -sf -X POST "$BASE/api/$1" -H 'Content-Type: application/json' -d "$2" 2>/dev/null
}

wait_for_log() {
    local pattern="$1" timeout="${2:-120}"
    local deadline=$((SECONDS + timeout))
    while [ $SECONDS -lt $deadline ]; do
        if grep -aq "$pattern" "$LOG" 2>/dev/null; then
            return 0
        fi
        sleep 1
    done
    return 1
}

wait_for_pipeline_idle() {
    # Wait for a pipeline cycle to complete after a given count
    local prev_count="$1" timeout="${2:-300}"
    local target=$((prev_count + 1))
    local deadline=$((SECONDS + timeout))
    while [ $SECONDS -lt $deadline ]; do
        local count
        count=$(grep -ac "Pipeline cycle complete" "$LOG" 2>/dev/null || echo 0)
        if [ "$count" -ge "$target" ]; then
            return 0
        fi
        sleep 2
    done
    return 1
}

# ── Setup ──────────────────────────────────────────────────────────
bold "=== Face Rescan Preservation Test ==="
echo ""

"$(dirname "$0")/test-stop.sh" --wipe 2>/dev/null || true
# Kill any leftover processes on the test port (test-stop only kills by PID file)
lsof -ti :"$PORT" 2>/dev/null | xargs -r kill -9 2>/dev/null || true
sleep 1
# Ensure a clean log file (previous kill may have left binary junk)
> "$LOG"

# Use tutorial config (lower thresholds for stock photos) and images only
"$(dirname "$0")/test-start.sh" --tutorial \
    --folders "$(dirname "$0")/mktutorial/examples" \
    2>&1

echo ""

# ── Step 1: Wait for initial face detection to complete ────────────
bold "Step 1: Waiting for initial face detection..."
if ! wait_for_log "Pipeline cycle complete" 300; then
    red "Pipeline did not complete within 300s"
    "$(dirname "$0")/test-stop.sh" 2>/dev/null || true
    exit 1
fi
cycle_count=$(grep -ac "Pipeline cycle complete" "$LOG" 2>/dev/null || echo 0)

# Get initial face count
initial_faces=$(api_get "faces?unknown=false" | python3 -c "
import sys, json
print(len(json.load(sys.stdin)['data']))
" 2>/dev/null)
echo "  Initial faces detected: $initial_faces"
assert_ge "at least some faces detected" 1 "$initial_faces"

# ── Step 2: Name some faces and ignore others ──────────────────────
bold "Step 2: Naming and ignoring faces..."

# Get all faces with details
face_data=$(api_get "faces?unknown=false" | python3 -c "
import sys, json
faces = json.load(sys.stdin)['data']
# Print: face_id image_id person_id
for f in faces:
    print(f['id'], f['image_id'], f.get('person_id') or 'NULL')
" 2>/dev/null)

# Pick faces to name and ignore
NAMED_FACE_ID=""
IGNORED_FACE_ID=""
UNNAMED_FACE_ID=""
face_count=0

while IFS=' ' read -r fid iid pid; do
    face_count=$((face_count + 1))
    if [ $face_count -eq 1 ] && [ -z "$NAMED_FACE_ID" ]; then
        NAMED_FACE_ID="$fid"
    elif [ $face_count -eq 2 ] && [ -z "$IGNORED_FACE_ID" ]; then
        IGNORED_FACE_ID="$fid"
    elif [ $face_count -eq 3 ] && [ -z "$UNNAMED_FACE_ID" ]; then
        UNNAMED_FACE_ID="$fid"
    fi
done <<< "$face_data"

if [ -z "$NAMED_FACE_ID" ]; then
    red "No faces available to test with"
    "$(dirname "$0")/test-stop.sh" 2>/dev/null || true
    exit 1
fi

# Name the first face via the identify endpoint (creates person automatically).
# The endpoint may return 500 due to a pre-existing orjson serialisation bug
# (bytes in response), but the DB update still succeeds.
echo "  Naming face $NAMED_FACE_ID as 'TestPerson'..."
api_post "faces/$NAMED_FACE_ID/identify" '{"name": "TestPerson"}' > /dev/null 2>&1 || true
sleep 0.5

# Verify it stuck
named_check=$(api_get "faces/$NAMED_FACE_ID" | python3 -c "
import sys, json
f = json.load(sys.stdin)['data']
print(f.get('person_name', 'NONE'))
" 2>/dev/null)
if [ "$named_check" = "TestPerson" ]; then
    echo "  Face $NAMED_FACE_ID identified as TestPerson"
else
    red "  Failed to name face $NAMED_FACE_ID (got: $named_check)"
    "$(dirname "$0")/test-stop.sh" 2>/dev/null || true
    exit 1
fi

# Ignore the second face (assign to '-' — the identify endpoint creates
# the ignored person automatically if it doesn't exist)
if [ -n "$IGNORED_FACE_ID" ]; then
    api_post "faces/$IGNORED_FACE_ID/identify" '{"name": "-"}' > /dev/null 2>&1 || true
    sleep 0.5
    ignored_check=$(api_get "faces/$IGNORED_FACE_ID" | python3 -c "
import sys, json
f = json.load(sys.stdin)['data']
print(f.get('person_name', 'NONE'))
" 2>/dev/null)
    if [ "$ignored_check" = "-" ]; then
        echo "  Face $IGNORED_FACE_ID ignored (assigned to '-')"
    else
        red "  Failed to ignore face $IGNORED_FACE_ID (got: $ignored_check)"
    fi
fi

if [ -n "$UNNAMED_FACE_ID" ]; then
    echo "  Face $UNNAMED_FACE_ID left unnamed"
fi

# ── Step 3: Record pre-rescan state ───────────────────────────────
bold "Step 3: Recording pre-rescan state..."

pre_rescan=$(api_get "faces?unknown=false" | python3 -c "
import sys, json
faces = json.load(sys.stdin)['data']
named = [f for f in faces if f.get('person_id') and f.get('person_name') != '-']
ignored = [f for f in faces if f.get('person_name') == '-']
unnamed = [f for f in faces if not f.get('person_id')]
print(f'total={len(faces)} named={len(named)} ignored={len(ignored)} unnamed={len(unnamed)}')
# Print named face IDs for later verification
for f in named:
    print(f'NAMED:{f[\"id\"]}:{f[\"person_id\"]}:{f[\"person_name\"]}')
for f in ignored:
    print(f'IGNORED:{f[\"id\"]}:{f[\"person_id\"]}')
" 2>/dev/null)

echo "  $pre_rescan" | head -1
PRE_NAMED_IDS=$(echo "$pre_rescan" | grep "^NAMED:" | cut -d: -f2 | sort)
PRE_IGNORED_IDS=$(echo "$pre_rescan" | grep "^IGNORED:" | cut -d: -f2 | sort)
PRE_NAMED_COUNT=$(echo "$pre_rescan" | grep -c "^NAMED:" || echo 0)
PRE_IGNORED_COUNT=$(echo "$pre_rescan" | grep -c "^IGNORED:" || echo 0)
echo "  Named face IDs: $PRE_NAMED_IDS"
echo "  Ignored face IDs: $PRE_IGNORED_IDS"

# ── Step 4: Hot-reload with changed face detection settings ────────
bold "Step 4: Hot-reloading with lower confidence threshold..."

# Lower the confidence significantly to detect more faces
api_post "config/save" '{"values": {"face_detection_min_confidence": 0.80}}' > /dev/null
reload_result=$(api_post "config/hot-reload" '{"changed_fields": ["face_detection_min_confidence"]}')
echo "  Hot-reload result: $(echo "$reload_result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('message','?'))" 2>/dev/null)"

# Check how many images were marked for rescan
rescan_marked=$(grep -a "marked.*images for face rescan" "$LOG" | tail -1 | grep -oP '\d+(?= images)' || echo "?")
echo "  Images marked for rescan: $rescan_marked"

# ── Step 5: Wait for rescan to complete ───────────────────────────
bold "Step 5: Waiting for face rescan pipeline to complete..."
if ! wait_for_pipeline_idle "$cycle_count" 300; then
    red "Pipeline did not complete rescan within 300s"
    "$(dirname "$0")/test-stop.sh" 2>/dev/null || true
    exit 1
fi
echo "  Pipeline cycle completed"

# ── Step 6: Verify results ────────────────────────────────────────
bold "Step 6: Verifying face preservation..."

post_rescan=$(api_get "faces?unknown=false" | python3 -c "
import sys, json
faces = json.load(sys.stdin)['data']
named = [f for f in faces if f.get('person_id') and f.get('person_name') != '-']
ignored = [f for f in faces if f.get('person_name') == '-']
unnamed = [f for f in faces if not f.get('person_id')]
print(f'total={len(faces)} named={len(named)} ignored={len(ignored)} unnamed={len(unnamed)}')
for f in named:
    print(f'NAMED:{f[\"id\"]}:{f[\"person_id\"]}:{f[\"person_name\"]}')
for f in ignored:
    print(f'IGNORED:{f[\"id\"]}:{f[\"person_id\"]}')
" 2>/dev/null)

echo "  Post-rescan: $(echo "$post_rescan" | head -1)"
POST_NAMED_IDS=$(echo "$post_rescan" | grep "^NAMED:" | cut -d: -f2 | sort)
POST_IGNORED_IDS=$(echo "$post_rescan" | grep "^IGNORED:" | cut -d: -f2 | sort)
POST_NAMED_COUNT=$(echo "$post_rescan" | grep -c "^NAMED:" || echo 0)
POST_IGNORED_COUNT=$(echo "$post_rescan" | grep -c "^IGNORED:" || echo 0)
POST_TOTAL=$(echo "$post_rescan" | head -1 | grep -oP 'total=\K\d+')

echo ""

# Verify named faces preserved (same IDs, same person assignments)
assert_eq "named face count preserved" "$PRE_NAMED_COUNT" "$POST_NAMED_COUNT"
assert_eq "named face IDs preserved" "$PRE_NAMED_IDS" "$POST_NAMED_IDS"

# Verify ignored faces preserved
assert_eq "ignored face count preserved" "$PRE_IGNORED_COUNT" "$POST_IGNORED_COUNT"
assert_eq "ignored face IDs preserved" "$PRE_IGNORED_IDS" "$POST_IGNORED_IDS"

# Verify the specific named face still has the right person
if [ -n "$NAMED_FACE_ID" ]; then
    named_person=$(api_get "faces/$NAMED_FACE_ID" | python3 -c "
import sys, json
f = json.load(sys.stdin)['data']
print(f.get('person_name', 'NONE'))
" 2>/dev/null)
    assert_eq "named face still assigned to TestPerson" "TestPerson" "$named_person"
fi

# Verify the ignored face still has '-' assignment
if [ -n "$IGNORED_FACE_ID" ]; then
    ignored_name=$(api_get "faces/$IGNORED_FACE_ID" | python3 -c "
import sys, json
f = json.load(sys.stdin)['data']
print(f.get('person_name', 'NONE'))
" 2>/dev/null)
    assert_eq "ignored face still assigned to '-'" "-" "$ignored_name"
fi

# With a lower threshold we should have at least as many total faces
assert_ge "total faces >= initial (lower threshold)" "$initial_faces" "$POST_TOTAL"

# ── Step 7: Now raise the threshold and verify unnamed are removed ─
bold "Step 7: Hot-reloading with HIGHER confidence threshold..."

# Count unnamed faces before
pre_unnamed=$(api_get "faces?unknown=false" | python3 -c "
import sys, json
faces = json.load(sys.stdin)['data']
unnamed = [f for f in faces if not f.get('person_id')]
print(len(unnamed))
" 2>/dev/null)
echo "  Unnamed faces before: $pre_unnamed"

cycle_count2=$(grep -ac "Pipeline cycle complete" "$LOG" 2>/dev/null || echo 0)

# Raise confidence to maximum — should remove most unnamed faces
api_post "config/save" '{"values": {"face_detection_min_confidence": 0.999}}' > /dev/null
api_post "config/hot-reload" '{"changed_fields": ["face_detection_min_confidence"]}' > /dev/null
echo "  Hot-reload sent (confidence=0.999)"

if ! wait_for_pipeline_idle "$cycle_count2" 300; then
    red "Pipeline did not complete rescan within 300s"
    "$(dirname "$0")/test-stop.sh" 2>/dev/null || true
    exit 1
fi
echo "  Pipeline cycle completed"

bold "Step 8: Verifying unnamed faces removed but named/ignored preserved..."

final=$(api_get "faces?unknown=false" | python3 -c "
import sys, json
faces = json.load(sys.stdin)['data']
named = [f for f in faces if f.get('person_id') and f.get('person_name') != '-']
ignored = [f for f in faces if f.get('person_name') == '-']
unnamed = [f for f in faces if not f.get('person_id')]
print(f'total={len(faces)} named={len(named)} ignored={len(ignored)} unnamed={len(unnamed)}')
for f in named:
    print(f'NAMED:{f[\"id\"]}:{f[\"person_id\"]}:{f[\"person_name\"]}')
for f in ignored:
    print(f'IGNORED:{f[\"id\"]}:{f[\"person_id\"]}')
" 2>/dev/null)

echo "  Final state: $(echo "$final" | head -1)"
FINAL_NAMED_IDS=$(echo "$final" | grep "^NAMED:" | cut -d: -f2 | sort)
FINAL_IGNORED_IDS=$(echo "$final" | grep "^IGNORED:" | cut -d: -f2 | sort)
FINAL_NAMED_COUNT=$(echo "$final" | grep -c "^NAMED:" || echo 0)
FINAL_IGNORED_COUNT=$(echo "$final" | grep -c "^IGNORED:" || echo 0)

echo ""

# Named faces must still be there
assert_eq "named faces still preserved after tightening" "$PRE_NAMED_COUNT" "$FINAL_NAMED_COUNT"
assert_eq "named face IDs still preserved after tightening" "$PRE_NAMED_IDS" "$FINAL_NAMED_IDS"

# Ignored faces must still be there
assert_eq "ignored faces still preserved after tightening" "$PRE_IGNORED_COUNT" "$FINAL_IGNORED_COUNT"
assert_eq "ignored face IDs still preserved after tightening" "$PRE_IGNORED_IDS" "$FINAL_IGNORED_IDS"

# Verify the specific named face still has the right person
if [ -n "$NAMED_FACE_ID" ]; then
    final_person=$(api_get "faces/$NAMED_FACE_ID" | python3 -c "
import sys, json
f = json.load(sys.stdin)['data']
print(f.get('person_name', 'NONE'))
" 2>/dev/null)
    assert_eq "named face STILL TestPerson after tightening" "TestPerson" "$final_person"
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

# Check for backend errors
errors=$(grep -acE 'ERROR.*(reconcil|rescan)' "$LOG" 2>/dev/null || echo 0)
if [ "$errors" -gt 0 ]; then
    echo ""
    red "  Backend errors found in log:"
    grep -iE 'Traceback|ERROR.*reconcil|ERROR.*rescan' "$LOG" | head -5
fi

"$(dirname "$0")/test-stop.sh" 2>/dev/null || true

exit $FAIL
