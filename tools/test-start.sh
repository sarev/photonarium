#!/bin/bash
# Start a clean test instance of Photonarium.
# Usage: tools/test-start.sh [--port PORT] [--folders FOLDER1,FOLDER2,...] [--no-scan] [--tutorial]
#
# Defaults:
#   port:    5151
#   folders: none (pass --folders to add)
#   scan:    enabled

set -e

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TEST_DIR="/tmp/photonarium-test"
PORT=5151
SCAN="--scan"
FOLDERS=()
TUTORIAL=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --port)     PORT="$2"; shift 2 ;;
        --folders)  IFS=',' read -ra FOLDERS <<< "$2"; shift 2 ;;
        --no-scan)  SCAN=""; shift ;;
        --tutorial) TUTORIAL=true; shift ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

# Kill anything on the port
lsof -ti :"$PORT" 2>/dev/null | xargs -r kill 2>/dev/null || true

# Wipe previous test data and recreate directory
rm -rf "$TEST_DIR"
mkdir -p "$TEST_DIR"

# Build folder args
FOLDER_ARGS=""
for f in "${FOLDERS[@]}"; do
    FOLDER_ARGS="$FOLDER_ARGS --add-folder $f"
done

# Activate venv and start
source "$REPO_ROOT/env/bin/activate"
cd "$REPO_ROOT/app"

# Generate config if it doesn't exist yet (first run after wipe)
if [ ! -f "$TEST_DIR/photonarium.yml" ]; then
    python -u app.py --init-config "$TEST_DIR" --config "$TEST_DIR/photonarium.yml" 2>/dev/null || true
fi

# --tutorial: patch config to match tutorial.py's face detection settings
# (lower thresholds for the small stock-photo faces in mktutorial/examples)
if [ "$TUTORIAL" = true ] && [ -f "$TEST_DIR/photonarium.yml" ]; then
    sed -i \
        -e 's/face_detection_min_confidence: 0.95/face_detection_min_confidence: 0.94/' \
        -e 's/face_detection_min_size: 60/face_detection_min_size: 20/' \
        -e 's/face_recognition_threshold: 0.7$/face_recognition_threshold: 0.90/' \
        "$TEST_DIR/photonarium.yml"
fi

# Symlink ML model files from the real data dir so NIMA/LAION scoring works.
# Resolve the data_dir from the real (non-test) config; fall back to repo root.
REAL_DATA_DIR=$(python -c "
import sys, os; sys.path.insert(0, '$REPO_ROOT/app')
from config import load_config, get_default_config_path
c = load_config(get_default_config_path())
print(os.path.expanduser(c.data_dir) if c.data_dir else '')
" 2>/dev/null)
REAL_DATA_DIR="${REAL_DATA_DIR:-$REPO_ROOT}"
for model_file in .nima-mobilenetv2-ava.pth .laion-aesthetic-head.pth; do
    if [ -f "$REAL_DATA_DIR/$model_file" ] && [ ! -e "$TEST_DIR/$model_file" ]; then
        ln -s "$REAL_DATA_DIR/$model_file" "$TEST_DIR/$model_file"
    fi
done

LOG_FILE="/tmp/photonarium-test.log"
echo "Logging to $LOG_FILE"

python -u app.py --port "$PORT" --debug \
    --config "$TEST_DIR/photonarium.yml" \
    --data-dir "$TEST_DIR" \
    $FOLDER_ARGS $SCAN \
    >"$LOG_FILE" 2>&1 &

APP_PID=$!
echo "Started (PID $APP_PID) on port $PORT"
echo "$APP_PID" > "$TEST_DIR/pid"
