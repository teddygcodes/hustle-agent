#!/usr/bin/env python3
"""Daily report — comprehensive markdown report covering 24h of bot state.

Generates ``bot/state/reports/daily/daily_report_YYYY-MM-DD.md`` covering 10
sections: health pulse, scanner activity, decision audit (cohort), trade
activity, CF coverage, live momentum events (journal), DQS + regime
distribution, cadence health, errors, state file growth.

Discipline (Session 35):
    - First I/O writes the header so a partial report survives a mid-run crash.
    - Each section wrapped in _safe_section: a single source's failure renders
      ``_[section unavailable: REASON]_`` and the script keeps going.
    - Read-only — no bot state mutation.

Usage:
    python3 tools/daily_report.py [--date YYYY-MM-DD] [--regime-by AXIS]

``--date`` is the report-FOR date in ET (e.g. ``--date 2026-04-29`` reports
on Apr 29's data, typically run on Apr 30 morning). Default = yesterday in ET.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools import _report_helpers as helpers  # noqa: E402


def _parse_date(s: str) -> datetime:
    try:
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=helpers.ET)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid --date {s!r}: expected YYYY-MM-DD") from exc


def build_report(
    report_date: datetime,
    *,
    regime_by: str | None = None,
    now_utc: datetime | None = None,
    out_dir: Path | None = None,
) -> Path:
    """Generate the daily report file. Returns the path written."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    if out_dir is None:
        out_dir = helpers.REPORTS_DIR / "daily"

    window_start, window_end = helpers.parse_window(report_date, days=1)
    out_path = out_dir / f"daily_report_{report_date.date().isoformat()}.md"

    extra_lines = (
        f"_Window: {window_start.isoformat()} → {window_end.isoformat()} (UTC)_",
    )
    if regime_by:
        extra_lines = (*extra_lines, f"_Regime axis: `{regime_by}`_")

    helpers.write_header(
        out_path,
        f"Daily Report — {report_date.date().isoformat()} ET",
        now_utc,
        extra_lines=extra_lines,
    )

    skipped = helpers.render_shared_sections(out_path, now_utc, window_start, window_end, regime_by)
    helpers.append_footer(out_path, now_utc, skipped=skipped, total_sections=len(helpers.SHARED_SECTIONS))
    return out_path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--date", type=_parse_date, default=None,
        help="Report-FOR date in ET (YYYY-MM-DD). Defaults to yesterday in ET.",
    )
    ap.add_argument(
        "--regime-by", choices=list(helpers.REGIME_AXES), default=None,
        help="Regime axis passed through to component reports that support it.",
    )
    args = ap.parse_args(argv)

    now_utc = datetime.now(timezone.utc)
    report_date = args.date or helpers.yesterday_in_et(now_utc)

    out_path = build_report(report_date, regime_by=args.regime_by, now_utc=now_utc)
    sys.stdout.write(out_path.read_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
