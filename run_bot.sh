#!/bin/bash
cd "$(dirname "$0")"
while true; do
    echo "[$(date)] Starting bot..." >> bot/logs/watchdog.log
    /Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/Resources/Python.app/Contents/MacOS/Python -m bot.main
    echo "[$(date)] Bot exited (code $?), restarting in 5s..." >> bot/logs/watchdog.log
    sleep 5
done
