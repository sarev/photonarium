#!/bin/bash
# Stop the test instance and optionally wipe data.
# Usage: tools/test-stop.sh [--port PORT] [--wipe]

PORT=5151
WIPE=0

while [[ $# -gt 0 ]]; do
    case $1 in
        --port) PORT="$2"; shift 2 ;;
        --wipe) WIPE=1; shift ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

lsof -ti :"$PORT" 2>/dev/null | xargs -r kill 2>/dev/null || true
echo "Stopped test instance on port $PORT"

if [[ $WIPE -eq 1 ]]; then
    rm -rf /tmp/photonarium-test
    echo "Wiped /tmp/photonarium-test"
fi
