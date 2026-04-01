#!/bin/bash
# Stop the hustle agent daemon
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PID_FILE="$PROJECT_DIR/state/agent.pid"

if [ ! -f "$PID_FILE" ]; then
    echo "No PID file found. Agent may not be running."
    exit 1
fi

PID=$(cat "$PID_FILE")
if kill -0 "$PID" 2>/dev/null; then
    echo "Stopping agent (PID $PID)..."
    kill "$PID"
    rm -f "$PID_FILE"
    echo "Agent stopped."
else
    echo "Process $PID not found. Cleaning up stale PID file."
    rm -f "$PID_FILE"
fi
