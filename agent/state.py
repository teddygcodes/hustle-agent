"""
Hustle Agent — State Management

Shared paths, state file I/O, and atomic write helpers.
Used by engine.py, tool_executors.py, and other modules.
"""

import json
import datetime
import shutil
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = BASE_DIR / "state"
CONFIG_DIR = BASE_DIR / "config"
TOOLS_DIR = BASE_DIR / "tools"

STATE_FILE = STATE_DIR / "agent_state.json"
LEDGER_FILE = STATE_DIR / "ledger.json"
JOURNAL_FILE = STATE_DIR / "journal.md"
CONVERSATIONS_FILE = STATE_DIR / "conversations.json"
UI_REQUESTS_FILE = STATE_DIR / "ui_requests.json"
INBOX_FILE = STATE_DIR / "inbox.json"
ACTIONS_FILE = STATE_DIR / "actions.json"

REQUIRED_STATE_FILES = {
    "agent_state.json": "json",
    "ledger.json": "json",
    "journal.md": "markdown",
    "conversations.json": "json",
    "inbox.json": "json",
    "ui_requests.json": "json",
    "memory.json": "json",
    "projections.json": "json",
    "pipeline.json": "json",
    "proposals.json": "json",
    "audits.json": "json",
    "watches.json": "json",
    "api_costs.json": "json",
    "actions.json": "json",
}

# ---------------------------------------------------------------------------
# State Management
# ---------------------------------------------------------------------------

BACKUP_DIR = STATE_DIR / "backups"

def atomic_write_json(path: Path, data):
    """Write JSON atomically: write to .tmp, then rename."""
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    tmp.rename(path)

def backup_state(cycle: int):
    """Backup state and ledger before each cycle. Keep last 20."""
    BACKUP_DIR.mkdir(exist_ok=True)
    for src in [STATE_FILE, LEDGER_FILE]:
        if src.exists():
            dst = BACKUP_DIR / f"{src.stem}_cycle{cycle}{src.suffix}"
            shutil.copy2(src, dst)
    for prefix in ["agent_state_cycle", "ledger_cycle"]:
        backups = sorted(BACKUP_DIR.glob(f"{prefix}*"), key=lambda p: p.stat().st_mtime)
        for old in backups[:-20]:
            old.unlink()

def load_state() -> dict:
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(state: dict):
    state["last_updated"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    atomic_write_json(STATE_FILE, state)

def load_ledger() -> list:
    with open(LEDGER_FILE, "r") as f:
        return json.load(f)

def save_ledger(ledger: list):
    atomic_write_json(LEDGER_FILE, ledger)

def append_journal(entry: str):
    with open(JOURNAL_FILE, "a") as f:
        f.write(f"\n{entry}\n")

def load_conversations() -> list:
    if CONVERSATIONS_FILE.exists():
        with open(CONVERSATIONS_FILE, "r") as f:
            return json.load(f)
    return []

def save_conversations(convos: list):
    atomic_write_json(CONVERSATIONS_FILE, convos)

def load_ui_requests() -> list:
    if UI_REQUESTS_FILE.exists():
        with open(UI_REQUESTS_FILE, "r") as f:
            return json.load(f)
    return []

def save_ui_requests(requests: list):
    atomic_write_json(UI_REQUESTS_FILE, requests)

def load_inbox() -> list:
    if INBOX_FILE.exists():
        with open(INBOX_FILE, "r") as f:
            return json.load(f)
    return []

def atomic_push_inbox(message: str):
    """Add a message to the inbox (safe to call while daemon is running)."""
    inbox = load_inbox()
    inbox.append({
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "content": message
    })
    atomic_write_json(INBOX_FILE, inbox)

def drain_inbox() -> list:
    """Read all inbox messages and clear the inbox. Returns the messages."""
    inbox = load_inbox()
    if inbox:
        atomic_write_json(INBOX_FILE, [])
    return inbox
