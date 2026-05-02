"""Strategy Lab driver — CLI entry point.

Usage:
    python3 -m tools.strategy_lab.driver --candidate <module_name> --days 14

Loads ``tools/strategy_lab/candidates/<module>.py``, finds the
``STRATEGY`` (or ``strategy``) attribute, runs it against the historical
universe + clv data, writes a markdown report to
``tools/strategy_lab/reports_out/``, and prints a one-line summary.

Lab is read-only on `bot/state/`; it writes ONLY to ``reports_out/``.
"""
from __future__ import annotations

import argparse
import importlib
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from . import data_loader, evaluator, reports
from .candidate import CandidateOpportunity, CandidateStrategy


def _resolve_candidate(name: str) -> Optional[CandidateStrategy]:
    """Import ``tools.strategy_lab.candidates.<name>`` and find STRATEGY."""
    try:
        module = importlib.import_module(f"tools.strategy_lab.candidates.{name}")
    except ImportError as exc:
        print(
            f"error: could not import candidate '{name}': {exc}\n"
            f"Looked in: tools/strategy_lab/candidates/{name}.py",
            file=sys.stderr,
        )
        return None
    strategy = getattr(module, "STRATEGY", None) or getattr(module, "strategy", None)
    if strategy is None:
        print(
            f"error: candidate module '{name}' has no STRATEGY (or strategy) attribute",
            file=sys.stderr,
        )
        return None
    if not (hasattr(strategy, "name") and hasattr(strategy, "evaluate")):
        print(
            f"error: candidate '{name}' STRATEGY does not satisfy CandidateStrategy "
            f"(must have .name and .evaluate)",
            file=sys.stderr,
        )
        return None
    return strategy


def run(
    candidate: CandidateStrategy,
    *,
    days: int = 14,
    out_dir: Optional[Path] = None,
) -> tuple[Path, dict]:
    """Run the lab end-to-end. Returns ``(report_path, summary_dict)``.

    Side-effect-free wrt bot state — only writes to ``out_dir`` (default
    ``reports_out/``).
    """
    match_window_hours = float(getattr(candidate, "clv_match_window_hours", 2.0))
    universe_iter, clv_lookup, decisions_by_ticker, start, end = data_loader.load_window(
        days=days, clv_match_window_hours=match_window_hours
    )

    context = {
        "existing_decisions_by_ticker": decisions_by_ticker,
        "clv_match_window_hours": match_window_hours,
    }

    opps_with_ts: list[tuple[CandidateOpportunity, str]] = []
    universe_rows_seen = 0
    for row in universe_iter:
        universe_rows_seen += 1
        try:
            opp = candidate.evaluate(row, context)
        except Exception as exc:
            # Defensive: a buggy candidate shouldn't kill the whole run.
            print(
                f"warning: candidate.evaluate() raised on ticker "
                f"{row.get('ticker')!r}: {exc}",
                file=sys.stderr,
            )
            continue
        if opp is None:
            continue
        opps_with_ts.append((opp, row.get("ts") or ""))

    scored = evaluator.score(
        opps_with_ts, clv_lookup, match_window_hours=match_window_hours
    )
    summary = evaluator.aggregate(scored)

    markdown = reports.render_markdown(
        candidate_name=candidate.name,
        start=start,
        end=end,
        scored=scored,
        summary=summary,
        days=days,
        universe_rows_seen=universe_rows_seen,
        clv_match_window_hours=match_window_hours,
    )
    report_path = reports.write_report(
        candidate.name,
        markdown,
        out_dir=out_dir,
        today=datetime.now(timezone.utc).date(),
    )
    return report_path, summary


def _format_summary(candidate_name: str, summary: dict, report_path: Path) -> str:
    n = summary["n_total"]
    if n == 0:
        return (
            f"Candidate {candidate_name}: 0 would-have-bets, 0 settled. "
            f"Report: {report_path}"
        )
    mean_clv = summary["mean_clv_cents"]
    mean_clv_str = f"{mean_clv:+.2f}" if mean_clv is not None else "—"
    pnl_dollars = summary["total_pnl_dollars"]
    return (
        f"Candidate {candidate_name}: {n} would-have-bets, "
        f"{summary['n_resolved']} settled, mean CLV {mean_clv_str}¢, "
        f"hypothetical P&L ${pnl_dollars:+.2f}. Report: {report_path}"
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Strategy Lab v1 — run a candidate strategy against historical data."
    )
    parser.add_argument(
        "--candidate",
        required=True,
        help="Candidate module name under tools/strategy_lab/candidates/ "
        "(e.g. 'example_total_points_under').",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=14,
        help="Lookback window in days (default 14).",
    )
    parser.add_argument(
        "--report-out",
        type=str,
        default=None,
        help="Output directory for the markdown report (default: "
        "tools/strategy_lab/reports_out/).",
    )
    args = parser.parse_args(argv)

    strategy = _resolve_candidate(args.candidate)
    if strategy is None:
        return 2

    out_dir = Path(args.report_out) if args.report_out else None
    report_path, summary = run(strategy, days=args.days, out_dir=out_dir)
    print(_format_summary(strategy.name, summary, report_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
