"""Helpers for marking autonomous report routines complete in REPORT_CALENDAR."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CALENDAR_PATH = _REPO_ROOT / "REPORT_CALENDAR.md"


def _utc_stamp(now_utc: datetime | None) -> str:
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    return now_utc.astimezone(timezone.utc).isoformat()


def update_report_calendar_last_run(
    routine_name: str,
    *,
    calendar_path: Path | None = None,
    now_utc: datetime | None = None,
) -> bool:
    """Update a recurring routine's Last Run cell in REPORT_CALENDAR.md.

    Returns True when a matching row was updated. Missing files, missing rows,
    and table-shape mismatches return False so report generation stays non-fatal.
    """
    path = calendar_path or DEFAULT_CALENDAR_PATH
    try:
        lines = path.read_text().splitlines(keepends=True)
    except OSError:
        return False

    stamp = _utc_stamp(now_utc)
    in_recurring = False
    target_prefix = f"| {routine_name} |"

    for idx, line in enumerate(lines):
        stripped = line.rstrip("\n")
        if stripped.startswith("## "):
            in_recurring = stripped == "## Recurring routines"
            continue
        if not in_recurring or not stripped.startswith(target_prefix):
            continue

        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 4:
            return False
        cells[3] = stamp
        newline = "\n" if line.endswith("\n") else ""
        lines[idx] = "| " + " | ".join(cells) + " |" + newline
        try:
            path.write_text("".join(lines))
        except OSError:
            return False
        return True

    return False
