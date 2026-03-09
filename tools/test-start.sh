#!/bin/bash
# Start a clean test instance of Photonarium.
# Usage: tools/test-start.sh [--port PORT] [--folders FOLDER1,FOLDER2,...] [--no-scan]
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

while [[ $# -gt 0 ]]; do
    case $1 in
        --port)    PORT="$2"; shift 2 ;;
        --folders) IFS=',' read -ra FOLDERS <<< "$2"; shift 2 ;;
        --no-scan) SCAN=""; shift ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

# Kill anything on the port
lsof -ti :"$PORT" 2>/dev/null | xargs -r kill 2>/dev/null || true

# Wipe previous test data
rm -rf "$TEST_DIR"

# Build folder args
FOLDER_ARGS=""
for f in "${FOLDERS[@]}"; do
    FOLDER_ARGS="$FOLDER_ARGS --add-folder $f"
done

# Activate venv and start
source "$REPO_ROOT/env/bin/activate"
cd "$REPO_ROOT/app"
exec python app.py --port "$PORT" --debug \
    --config "$TEST_DIR/photonarium.yml" \
    --data-dir "$TEST_DIR" \
    $FOLDER_ARGS $SCAN
