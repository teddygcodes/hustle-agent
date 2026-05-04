#!/usr/bin/env python3
"""Hypothetical-variant report (Session 13c).

Sweeps a single parameter across N values for one Strategy class,
runs the back-tester for each variant, and prints a comparison table
sorted by sum CLV descending. Calls out the best variant up top.

Local-only (gitignored via tools/ entry).

Usage:
    python3 tools/hypothetical_report.py \\
        --strategy vig_stack_series \\
        --param min_relative_edge \\
        --values 0.05,0.10,0.15,0.20,0.25 \\
        --days 7

    python3 tools/hypothetical_report.py \\
        --strategy nba_game_momentum_strawman \\
        --param min_relative_edge \\
        --values 0.05,0.10,0.20 \\
        --days 7 --include-history
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "tools"))

from backtest import run_backtest  # noqa: E402  — sibling tool


# Strategy class registry. Add a new class here when you want to
# parameter-sweep it. Distinct from bot.strategies.REGISTERED_STRATEGIES,
# which is the LIVE registry — strawman strategies live here for back-
# testing without going live.
#
# Imports are lazy + guarded so that a missing strategy module (e.g.
# during 13c PART 3 → PART 4 staging) doesn't brick the sweep tool for
# strategies that ARE available.
def _strategy_classes() -> dict[str, type]:
    classes: dict[str, type] = {}
    try:
        from bot.strategies.vig_stack_series import VigStackSeries
        classes["vig_stack_series"] = VigStackSeries
    except ImportError:
        pass
    try:
        from bot.strategies.nba_game_momentum_strawman import (
            NbaGameMomentumStrawman,
        )
        classes["nba_game_momentum_strawman"] = NbaGameMomentumStrawman
    except ImportError:
        pass
    return classes


def run_sweep(
    *,
    strategy_name: str,
    param_name: str,
    values: list[float],
    start: date,
    end: date,
    include_history: bool,
) -> list[dict]:
    """Run one back-test per value and return rows sorted by sum CLV desc."""
    classes = _strategy_classes()
    if strategy_name not in classes:
        print(
            f"error: unknown strategy '{strategy_name}'. Known: "
            f"{sorted(classes)}",
            file=sys.stderr,
        )
        sys.exit(2)
    cls = classes[strategy_name]

    rows: list[dict] = []
    for v in values:
        strategy = cls(**{param_name: v})
        summary = run_backtest(
            strategy,
            start=start,
            end=end,
            include_history=include_history,
        )
        # Aggregate across all opp_types this strategy emits.
        opp_count = sum(t["opp_count"] for t in summary["totals"].values())
        matched_count = sum(
            t["matched_count"] for t in summary["totals"].values()
        )
        sum_clv = sum(
            t["sum_clv_cents"] for t in summary["totals"].values()
        )
        # Weighted means by matched_count for clv/winrate, by opp_count for edge
        weighted_edge = 0.0
        weighted_clv = 0.0
        weighted_winrate = 0.0
        n_matched = 0
        for t in summary["totals"].values():
            mc = t["matched_count"]
            if mc > 0 and t["mean_clv_cents"] is not None:
                weighted_clv += mc * t["mean_clv_cents"]
                weighted_winrate += mc * (t["win_rate_pct"] or 0.0)
                n_matched += mc
            if t["opp_count"] > 0 and t["mean_edge"] is not None:
                weighted_edge += t["opp_count"] * t["mean_edge"]
        opp_total = max(opp_count, 1)
        rows.append({
            "param_value": v,
            "opp_count": opp_count,
            "matched_count": matched_count,
            "match_pct": (
                100 * matched_count / opp_count) if opp_count else 0.0,
            "mean_edge": (
                weighted_edge / opp_total) if opp_count else None,
            "mean_clv_cents": (
                weighted_clv / n_matched) if n_matched else None,
            "win_rate_pct": (
                weighted_winrate / n_matched) if n_matched else None,
            "sum_clv_cents": sum_clv,
        })

    rows.sort(key=lambda r: r["sum_clv_cents"], reverse=True)
    return rows


def format_sweep_table(*, strategy_name: str, param_name: str,
                       rows: list[dict]) -> str:
    if not rows:
        return f"# Sweep — {strategy_name} {param_name}\n\n(no rows produced)"

    best = rows[0]
    best_value_str = f"{best['param_value']:g}"
    lines: list[str] = []
    lines.append(f"# Sweep — {strategy_name} / {param_name}\n")
    lines.append(
        f"**Best:** {param_name}={best_value_str} → "
        f"Σ CLV {best['sum_clv_cents']:.2f}¢ across {best['opp_count']} opps "
        f"({best['matched_count']} matched).\n"
    )
    lines.append("| " + param_name + " | Opps | Matched | Match % | "
                 "Mean Edge | Mean CLV ¢ | Σ CLV ¢ | Win % |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in rows:
        def f(x, fmt):
            return format(x, fmt) if x is not None else "—"
        lines.append(
            f"| {r['param_value']:g} | {r['opp_count']} | "
            f"{r['matched_count']} | "
            f"{r['match_pct']:.0f}% | {f(r['mean_edge'], '.4f')} | "
            f"{f(r['mean_clv_cents'], '.2f')} | "
            f"{r['sum_clv_cents']:.2f} | "
            f"{f(r['win_rate_pct'], '.1f')}% |"
        )
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Hypothetical-variant report — sweep one parameter "
                    "across N values, compare back-test outcomes.")
    parser.add_argument("--strategy", required=True,
                        help="Strategy name (vig_stack_series, "
                             "nba_game_momentum_strawman, ...)")
    parser.add_argument("--param", required=True,
                        help="Constructor kwarg name to sweep")
    parser.add_argument("--values", required=True,
                        help="Comma-separated values, e.g. 0.05,0.10,0.15")
    parser.add_argument("--days", type=int, default=None,
                        help="Look back N days from today")
    parser.add_argument("--start", type=str, default=None,
                        help="ISO date — explicit window start")
    parser.add_argument("--end", type=str, default=None,
                        help="ISO date — explicit window end (inclusive)")
    parser.add_argument("--include-history", action="store_true",
                        help="Fall back to Kalshi history on clv miss")
    args = parser.parse_args(argv)

    try:
        values = [float(v.strip()) for v in args.values.split(",")
                  if v.strip()]
    except ValueError:
        print(f"error: --values must be comma-separated floats, got "
              f"{args.values!r}", file=sys.stderr)
        return 2
    if not values:
        print("error: --values produced no parseable numbers",
              file=sys.stderr)
        return 2

    today = datetime.now(timezone.utc).date()
    if args.start and args.end:
        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end)
    elif args.days is not None:
        start = today - timedelta(days=args.days - 1)
        end = today
    else:
        print("error: pass --days N or --start/--end", file=sys.stderr)
        return 2

    rows = run_sweep(
        strategy_name=args.strategy,
        param_name=args.param,
        values=values,
        start=start,
        end=end,
        include_history=args.include_history,
    )
    print(format_sweep_table(
        strategy_name=args.strategy,
        param_name=args.param,
        rows=rows,
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
