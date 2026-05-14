from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import tools._calendar_stamp as calendar_stamp


def _calendar_text() -> str:
    return """# Report Calendar

## Recurring routines

| Routine | Cadence | Next Run | Last Run | Output | Why | Notes |
|---|---|---|---|---|---|---|
| Daily report | Daily 3:00 AM ET | (after first fire) | 2026-05-07T05:42Z | `bot/state/reports/daily/daily_report_YYYY-MM-DD.md` | Verify bot health | old note |
| Weekly report | Sundays 6:00 AM ET | (after first fire) | 2026-05-03T17:39Z | `bot/state/reports/weekly/weekly_report_YYYY-WNN.md` | Synthesis layer | weekly note |
| Discovery agent | Daily 6:00 AM ET | (after first fire) | — | `bot/state/discovery/discovery_report_YYYY-MM-DD.md` | Heuristic scan | discovery note |

## One-off routines

| Routine | Fires | Status | Output | Why |
|---|---|---|---|---|
| Daily report | 2026-05-13 9:00 AM ET | scheduled | inline | should not be touched |
"""


def test_updates_only_named_recurring_row(tmp_path: Path):
    calendar = tmp_path / "REPORT_CALENDAR.md"
    calendar.write_text(_calendar_text())
    stamp_time = datetime(2026, 5, 13, 12, 34, 56, tzinfo=timezone.utc)

    changed = calendar_stamp.update_report_calendar_last_run(
        "Daily report",
        calendar_path=calendar,
        now_utc=stamp_time,
    )

    assert changed is True
    text = calendar.read_text()
    assert "2026-05-13T12:34:56+00:00" in text
    assert "2026-05-03T17:39Z" in text
    assert "| Daily report | 2026-05-13 9:00 AM ET | scheduled | inline | should not be touched |" in text


def test_preserves_unrelated_content(tmp_path: Path):
    calendar = tmp_path / "REPORT_CALENDAR.md"
    before = _calendar_text()
    calendar.write_text(before)

    calendar_stamp.update_report_calendar_last_run(
        "Discovery agent",
        calendar_path=calendar,
        now_utc=datetime(2026, 5, 13, 10, 0, tzinfo=timezone.utc),
    )

    after = calendar.read_text()
    assert "# Report Calendar" in after
    assert "weekly note" in after
    assert "should not be touched" in after
    assert after.count("## Recurring routines") == 1
    assert after.count("## One-off routines") == 1


def test_returns_false_when_row_missing(tmp_path: Path):
    calendar = tmp_path / "REPORT_CALENDAR.md"
    calendar.write_text(_calendar_text())
    before = calendar.read_text()

    changed = calendar_stamp.update_report_calendar_last_run(
        "Not a real routine",
        calendar_path=calendar,
        now_utc=datetime(2026, 5, 13, tzinfo=timezone.utc),
    )

    assert changed is False
    assert calendar.read_text() == before


def test_returns_false_when_calendar_missing(tmp_path: Path):
    changed = calendar_stamp.update_report_calendar_last_run(
        "Daily report",
        calendar_path=tmp_path / "missing.md",
    )

    assert changed is False


def test_naive_datetime_is_treated_as_utc(tmp_path: Path):
    calendar = tmp_path / "REPORT_CALENDAR.md"
    calendar.write_text(_calendar_text())

    calendar_stamp.update_report_calendar_last_run(
        "Weekly report",
        calendar_path=calendar,
        now_utc=datetime(2026, 5, 13, 1, 2, 3),
    )

    assert "2026-05-13T01:02:03+00:00" in calendar.read_text()
