#!/bin/bash
# Start the hustle agent as a background daemon
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PID_FILE="$PROJECT_DIR/state/agent.pid"
LOG_FILE="$PROJECT_DIR/logs/daemon.log"
INTERVAL="${1:-300}"

# Check if already running
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "Agent already running (PID $PID). Use stop_daemon.sh first."
        exit 1
    fi
    rm -f "$PID_FILE"
fi

# Ensure directories exist
mkdir -p "$PROJECT_DIR/logs" "$PROJECT_DIR/state"

echo "Starting hustle agent daemon (interval: ${INTERVAL}s)..."
cd "$PROJECT_DIR"
nohup python3 agent/engine.py loop --interval "$INTERVAL" >> "$LOG_FILE" 2>&1 &
AGENT_PID=$!
echo "$AGENT_PID" > "$PID_FILE"
echo "Agent started (PID $AGENT_PID). Logs: $LOG_FILE"
echo "To stop: ./deploy/stop_daemon.sh"
