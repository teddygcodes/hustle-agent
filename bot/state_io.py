"""
Bot State I/O — Thread-safe JSON read/write

All bot state files go through here. A single threading.Lock serializes
concurrent access between the async main loop and Telegram command callbacks,
preventing read-modify-write races on positions.json and trade_history.json.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

_lock = threading.Lock()


def load_json(path: Path) -> list | dict:
    """Load a JSON file under the state lock. Returns [] or {} if missing."""
    with _lock:
        if path.exists():
            return json.loads(path.read_text())
        return [] if "positions" in str(path) or "history" in str(path) else {}


def save_json(path: Path, data):
    """Atomically write data to a JSON file under the state lock."""
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str))
        tmp.rename(path)
