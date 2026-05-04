#!/usr/bin/env python3
"""Weekly report — daily-shape sections (7d) + 6 weekly-only sections.

Renamed from ``tools/weekly_digest.py`` (Session 24) and restructured per
Session 35 to match the daily report's "health pulse first" layout. The 10
shared sections (§§1–10) come from ``tools/_report_helpers.py`` and are run
with a 7-day window. Weekly adds 6 cross-cutting sections:

    §11. Week-over-week deltas (vs prior weekly_report file)
    §12. Bucket analysis (live_momentum_buckets)
    §13. Research dataset rebuild summary (live_momentum_dataset)
    §14. Excursion + exit-replay
    §15. Calibration findings (mis-tuned gates)
    §16. Retuning candidates (derived from §15)

Discipline (Session 35):
    - First I/O writes the header so a partial report survives a mid-run crash.
    - Each section wrapped in _safe_section.
    - Imports throughout (no subprocess) — every called tool exposes a clean
      render function.
    - Read-only.

Usage:
    python3 tools/weekly_report.py [--week-end YYYY-MM-DD] [--regime-by AXIS]

``--week-end`` is the Sunday whose week the report covers. Default = the most
recent Sunday in ET (today if today is Sunday).
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools import _report_helpers as helpers  # noqa: E402

NEXT_SESSION_PLACEHOLDER = 36  # surface in §16 retuning candidates; edit manually if a session ships first.

WEEKLY_ONLY_TITLES = (
    "11. Week-over-week deltas",
    "12. Bucket analysis",
    "13. Research dataset rebuild",
    "14. Excursion + exit-replay",
    "15. Calibration findings",
    "16. Retuning candidates",
)


# ─────────────────────────────────────────────────────────────── §11 deltas

# Per-section headline-metric extractors. Each returns (label, line) pairs from a
# weekly_report markdown blob. Used by §11 to compute week-over-week deltas
# without depending on a structured machine-readable form.
_HEADLINE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("Decisions volume", r"\| Decisions volume \|"),
    ("Trades fired", r"\| Trades fired \|"),
    ("Errors", r"\| Errors \|"),
    ("Scans observed", r"Scans observed: \*\*[\d,]+\*\*"),
    ("Total ticks", r"Total ticks in window: \*\*[\d,]+\*\*"),
)


def _extract_headlines(md: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for label, pattern in _HEADLINE_PATTERNS:
        line = helpers.extract_headline_metric(md, pattern)
        if line:
            found[label] = line
    return found


def render_week_over_week_deltas(week_end: datetime, current_md: str) -> str:
    """§11. Compare current week's headlines to the prior week's report."""
    out = ["# 11. Week-over-week deltas", ""]
    prior_md = helpers.read_prior_weekly_report(week_end)
    if prior_md is None:
        out.append("_No baseline yet._ This is either the first weekly report or no prior file exists at "
                   "`bot/state/reports/weekly/weekly_report_YYYY-WNN.md`.")
        return "\n".join(out)
    this_headlines = _extract_headlines(current_md)
    last_headlines = _extract_headlines(prior_md)
    if not this_headlines and not last_headlines:
        out.append("_No headline metrics extractable from either report._")
        return "\n".join(out)

    out.append("Headline lines extracted by regex from each section. Mismatches = the regex didn't match — "
               "either a section was unavailable or the renderer changed shape since last week.")
    out.append("")
    out.append("| Section | This week | Last week |")
    out.append("|---|---|---|")
    keys = sorted(set(this_headlines) | set(last_headlines))
    for k in keys:
        a = this_headlines.get(k, "_(missing)_").replace("|", "\\|")
        b = last_headlines.get(k, "_(missing)_").replace("|", "\\|")
        out.append(f"| {k} | {a[:80]} | {b[:80]} |")
    return "\n".join(out)


# ──────────────────────────────────────────────────────────── §12, §13, §14

def render_buckets() -> str:
    """§12. live_momentum_buckets.render_report against the current dataset."""
    from tools import live_momentum_buckets  # noqa: PLC0415
    dataset_path = helpers.STATE_DIR / "research" / "live_momentum_dataset.csv"
    if not dataset_path.exists():
        return ("# 12. Bucket analysis\n\n"
                f"_Dataset not found at {dataset_path}. Run `tools/live_momentum_dataset.py` first "
                f"(or wait for §13 to rebuild it)._")
    rows = live_momentum_buckets.load_dataset(str(dataset_path))
    if not rows:
        return "# 12. Bucket analysis\n\n_Dataset is empty._"
    body = live_momentum_buckets.render_report(rows)
    return helpers._demote_h1(body, "12. Bucket analysis")


def render_dataset_rebuild_summary(window_start: datetime, window_end: datetime) -> str:
    """§13. Rebuild the dataset (~30s); render only the row-count summary."""
    from tools import live_momentum_dataset as lmd  # noqa: PLC0415

    days = max(1, int((window_end - window_start).total_seconds() / 86400 + 0.5))
    out_path = helpers.STATE_DIR / "research" / "live_momentum_dataset.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    pre_count = 0
    if out_path.exists():
        try:
            with out_path.open() as f:
                pre_count = sum(1 for _ in f) - 1
                pre_count = max(0, pre_count)
        except OSError:
            pre_count = 0

    # Run the dataset main with --quiet so its own stdout doesn't pollute the
    # report. Capture anyway in case of warnings.
    captured = helpers.capture_main_stdout(lmd.main, ["--days", str(days), "--quiet"])

    post_count = 0
    if out_path.exists():
        try:
            with out_path.open() as f:
                post_count = max(0, sum(1 for _ in f) - 1)
        except OSError:
            post_count = 0

    out = ["# 13. Research dataset rebuild", "",
           f"Window: {days} days. Output: `{out_path.relative_to(_REPO_ROOT)}`."]
    out.append("")
    out.append(f"- Rows before rebuild: **{pre_count:,}**")
    out.append(f"- Rows after rebuild:  **{post_count:,}**")
    out.append(f"- Δ: **{post_count - pre_count:+,}**")
    if captured.strip():
        out.append("")
        out.append("Tool output:")
        out.append("")
        out.append("```")
        out.append(captured.strip()[-2000:])
        out.append("```")
    return "\n".join(out)


def render_excursion_and_exit_replay(regime_by: str | None) -> str:
    """§14. Concatenate excursion_report.generate_report + exit_replay output."""
    from tools import excursion_report  # noqa: PLC0415
    parts = ["# 14. Excursion + exit-replay", "",
             "Two sub-reports concatenated. Excursion = per-strategy median(MFE − exit) gap. "
             "Exit-replay = MOMENTUM_DQS_TRAIL_STOP sweep on settled live_momentum trades."]
    parts.append("")

    try:
        excursion_md = excursion_report.generate_report(flag_threshold=5, regime_by=regime_by)
        parts.append("## Excursion")
        parts.append("")
        parts.append(helpers._demote_h1(excursion_md, "Excursion (sanity check, not for decisions)"))
    except Exception as exc:  # noqa: BLE001
        parts.append("## Excursion")
        parts.append("")
        parts.append(f"_Excursion unavailable: {type(exc).__name__}: {exc}_")
    parts.append("")

    try:
        from tools import exit_replay  # noqa: PLC0415
        captured = helpers.capture_main_stdout(exit_replay.main, [])
        parts.append("## Exit-replay")
        parts.append("")
        parts.append(captured.rstrip() if captured.strip() else "_Exit-replay produced no output._")
    except Exception as exc:  # noqa: BLE001
        parts.append("## Exit-replay")
        parts.append("")
        parts.append(f"_Exit-replay unavailable: {type(exc).__name__}: {exc}_")
    return "\n".join(parts).rstrip()


# ───────────────────────────────────────────────────────── §15, §16 derivations

def _calibration_findings(window_start: datetime, window_end: datetime, regime_by: str | None) -> list[dict]:
    """Re-aggregate cohort decisions/CFs to find mis-tuned gates.

    A finding is a (opp_type, gate) row with reject_rate > 50% AND
    mean_clv_on_rejects > 0. Sorted by mean CLV descending.
    """
    from tools import cohort_report  # noqa: PLC0415
    decisions = cohort_report.load_decisions(window_start)
    cfs = cohort_report.load_cf_records(window_start)
    decisions = [d for d in decisions if (helpers._parse_iso(d.get("ts")) or window_start) < window_end]
    cfs = [c for c in cfs if (helpers._parse_iso(c.get("recorded_at")) or window_start) < window_end]
    dec_bins = cohort_report.aggregate_decisions(decisions, regime_by=regime_by)
    cf_bins = cohort_report.aggregate_cf(cfs, regime_by=regime_by)

    # Aggregate decisions by (opp_type, gate) ignoring regime axis for §15.
    by_gate: dict[tuple[str, str], dict] = defaultdict(lambda: {"invocations": 0, "rejects": 0})
    for key, vals in dec_bins.items():
        # cohort_report's aggregate_decisions uses (opp_type, regime_value, gate) keys.
        if len(key) != 3:
            continue
        opp, _regime, gate = key
        slot = by_gate[(opp, gate)]
        slot["invocations"] += vals.get("invocations", 0)
        slot["rejects"] += vals.get("rejects", 0)

    # cohort_report.aggregate_cf bins carry {total, settled, pending, sum_clv_rel}.
    cf_agg: dict[tuple[str, str], dict] = defaultdict(lambda: {"settled": 0, "sum_clv_rel": 0.0})
    for key, vals in cf_bins.items():
        if len(key) != 3:
            continue
        opp, _regime, gate = key
        slot = cf_agg[(opp, gate)]
        slot["settled"] += vals.get("settled", 0)
        slot["sum_clv_rel"] += vals.get("sum_clv_rel", 0.0)

    findings: list[dict] = []
    for (opp, gate), slot in by_gate.items():
        invocations = slot["invocations"]
        rejects = slot["rejects"]
        if invocations == 0:
            continue
        reject_rate = rejects / invocations
        cf_slot = cf_agg.get((opp, gate))
        if not cf_slot or cf_slot["settled"] == 0:
            continue
        # clv_relative is dollar-units in cohort_report; multiply by 100 for cents
        # for cohort-style display. Some CF records carry cent-magnitude values
        # already — the mean is what's compared to 0, so units only affect rendering.
        mean_clv_rel = cf_slot["sum_clv_rel"] / cf_slot["settled"]
        if reject_rate > 0.5 and mean_clv_rel > 0:
            findings.append({
                "opp_type": opp,
                "gate": gate,
                "reject_rate": reject_rate,
                "mean_clv_relative": mean_clv_rel,
                "n_settled_cfs": cf_slot["settled"],
            })
    findings.sort(key=lambda r: -r["mean_clv_relative"])
    return findings


def render_calibration_findings(window_start: datetime, window_end: datetime, regime_by: str | None) -> tuple[str, list[dict]]:
    """§15. Returns (markdown, findings) so §16 can reuse the findings list."""
    findings = _calibration_findings(window_start, window_end, regime_by)
    out = ["# 15. Calibration findings", "",
           "Mis-tuned (opp_type, gate) pairs: **reject rate > 50% AND mean CLV on rejects > 0**. "
           "Sorted by mean CLV descending."]
    out.append("")
    if not findings:
        out.append("_No calibration concerns this week._")
        return "\n".join(out), findings
    out.append("| opp_type | gate | reject rate | mean CLV (rel) | n settled CFs |")
    out.append("|---|---|---:|---:|---:|")
    for f in findings:
        out.append(
            f"| `{f['opp_type']}` | `{f['gate']}` | "
            f"{100 * f['reject_rate']:.0f}% | {f['mean_clv_relative']:+.4f} | {f['n_settled_cfs']} |"
        )
    return "\n".join(out), findings


def render_retuning_candidates(findings: list[dict]) -> str:
    """§16. Derived from §15 — one bullet per finding."""
    out = ["# 16. Retuning candidates", ""]
    if not findings:
        out.append("_No retuning candidates this week._")
        return "\n".join(out)
    out.append(f"Each row from §15 phrased as a concrete next-session investigation. "
               f"Replace `Session {NEXT_SESSION_PLACEHOLDER}` with the actual next session number when planning.")
    out.append("")
    for f in findings:
        out.append(
            f"- Consider Session {NEXT_SESSION_PLACEHOLDER}+ to investigate `{f['gate']}` on "
            f"`{f['opp_type']}` (reject rate {100 * f['reject_rate']:.0f}%, "
            f"mean rel-CLV {f['mean_clv_relative']:+.4f} at n={f['n_settled_cfs']} settled CFs)."
        )
    return "\n".join(out)


# ──────────────────────────────────────────────────────────────── orchestrator

def _parse_date(s: str) -> datetime:
    try:
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=helpers.ET)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid --week-end {s!r}: expected YYYY-MM-DD"
        ) from exc


def build_report(
    week_end: datetime,
    *,
    regime_by: str | None = None,
    now_utc: datetime | None = None,
    out_dir: Path | None = None,
) -> Path:
    """Generate the weekly report file. Returns the path written."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    if out_dir is None:
        out_dir = helpers.REPORTS_DIR / "weekly"

    week_start_local = week_end - timedelta(days=6)
    window_start = week_start_local.astimezone(timezone.utc)
    window_end = (week_end + timedelta(days=1)).astimezone(timezone.utc)

    iso_year, iso_week, _ = week_end.isocalendar()
    out_path = out_dir / f"weekly_report_{iso_year}-W{iso_week:02d}.md"

    extra_lines = (
        f"_Window: {window_start.isoformat()} → {window_end.isoformat()} (UTC; 7-day)_",
    )
    if regime_by:
        extra_lines = (*extra_lines, f"_Regime axis: `{regime_by}`_")

    helpers.write_header(
        out_path,
        f"Weekly Report — Week ending {week_end.date().isoformat()} ET",
        now_utc,
        extra_lines=extra_lines,
    )

    skipped: list[str] = helpers.render_shared_sections(
        out_path, now_utc, window_start, window_end, regime_by,
    )

    # findings_holder lets §16 reuse the findings list computed in §15.
    findings_holder: list[list[dict]] = []
    weekly_renderers = (
        ("11. Week-over-week deltas",
         lambda: render_week_over_week_deltas(week_end, out_path.read_text())),
        ("12. Bucket analysis", render_buckets),
        ("13. Research dataset rebuild",
         lambda: render_dataset_rebuild_summary(window_start, window_end)),
        ("14. Excursion + exit-replay", lambda: render_excursion_and_exit_replay(regime_by)),
        ("15. Calibration findings",
         lambda: _render_calibration_capture(window_start, window_end, regime_by, findings_holder)),
        ("16. Retuning candidates",
         lambda: render_retuning_candidates(findings_holder[0] if findings_holder else [])),
    )

    for title, fn in weekly_renderers:
        body, reason = helpers._safe_section(fn)
        if reason is None:
            helpers.append_section(out_path, body)
        else:
            helpers.append_section(out_path, f"# {title}\n\n{body}")
            skipped.append(f"{title} ({reason})")

    total_sections = len(helpers.SHARED_SECTIONS) + len(WEEKLY_ONLY_TITLES)
    helpers.append_footer(out_path, now_utc, skipped=skipped, total_sections=total_sections)
    return out_path


def _render_calibration_capture(window_start, window_end, regime_by, findings_holder):
    body, findings = render_calibration_findings(window_start, window_end, regime_by)
    findings_holder.append(findings)
    return body


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--week-end", type=_parse_date, default=None,
        help="Sunday whose week the report covers (YYYY-MM-DD ET). Default: most recent Sunday.",
    )
    ap.add_argument(
        "--regime-by", choices=list(helpers.REGIME_AXES), default=None,
        help="Regime axis passed through to component reports that support it.",
    )
    args = ap.parse_args(argv)

    now_utc = datetime.now(timezone.utc)
    week_end = args.week_end or helpers.last_sunday_in_et(now_utc)

    out_path = build_report(week_end, regime_by=args.regime_by, now_utc=now_utc)
    sys.stdout.write(out_path.read_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
