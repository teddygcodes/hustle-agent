#!/bin/bash
cd "$(dirname "$0")"
PYTHON_BIN="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
while true; do
    echo "[$(date)] Starting bot..." >> bot/logs/watchdog.log
    "$PYTHON_BIN" -m bot.main
    echo "[$(date)] Bot exited (code $?), restarting in 5s..." >> bot/logs/watchdog.log
    sleep 5
done
