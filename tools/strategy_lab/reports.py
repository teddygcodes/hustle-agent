"""Markdown report rendering for Strategy Lab.

Lab limitations are loud-documented in the report header so a human
reading the file cold understands the numbers are upper-bound
hypotheticals, not P&L forecasts.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from .evaluator import UNRESOLVED, ScoredOpportunity

REPORTS_DIR = Path(__file__).resolve().parent / "reports_out"

LAB_LIMITATIONS = """**LAB LIMITATIONS — read before drawing conclusions:**

- Hypothetical P&L is **settlement-anchored** and ignores slippage,
  exit-side logic, partial fills, and order-book dynamics.
- Only candidates with a matching settled `clv.json` record (`settled` or
  `counterfactual_settled`) within the configured ±N hour join window
  contribute to the resolved subset; everything else is `UNRESOLVED` (we
  literally don't have data to judge it).
- The ±N hour clv-match window is a heuristic. Long-dated futures may
  miss matches; tune via `candidate.clv_match_window_hours`.
- Use lab P&L to **filter ideas** (any candidate that's net-negative here
  is dead), NOT to forecast production P&L. Live execution will be
  20–30%+ worse on slippage alone.
"""


def _fmt_cents(value: Optional[float], width: int = 6) -> str:
    if value is None:
        return "—".rjust(width)
    return f"{value:+.2f}".rjust(width)


def _fmt_pct(value: Optional[float], width: int = 5) -> str:
    if value is None:
        return "—".rjust(width)
    return f"{value:.1f}%".rjust(width)


def _fmt_dollars(cents: Optional[float]) -> str:
    if cents is None:
        return "—"
    return f"${cents / 100.0:+.2f}"


def render_markdown(
    candidate_name: str,
    start: date,
    end: date,
    scored: list[ScoredOpportunity],
    summary: dict,
    *,
    days: int,
    universe_rows_seen: int,
    clv_match_window_hours: float,
) -> str:
    """Build a markdown string. Caller writes to disk."""
    lines: list[str] = []
    lines.append(f"# Strategy Lab — {candidate_name}\n")
    lines.append(f"_Generated {datetime.now(timezone.utc).isoformat()}_\n")
    lines.append(LAB_LIMITATIONS)
    lines.append("")

    # Run metadata
    lines.append("## Run metadata\n")
    lines.append(f"- Window: `{start.isoformat()}` → `{end.isoformat()}` ({days} days)")
    lines.append(f"- CLV match window: ±{clv_match_window_hours:.1f} hours")
    lines.append(f"- Universe rows scanned: **{universe_rows_seen:,}**")
    lines.append(f"- Would-have-bets emitted: **{summary['n_total']:,}**")
    lines.append(f"- Resolved (settled clv match): **{summary['n_resolved']:,}**")
    lines.append(f"- Unresolved: **{summary['n_unresolved']:,}**")
    lines.append("")

    if summary["n_total"] == 0:
        lines.append("## Result\n")
        lines.append("**0 would-have-bets.** Candidate's `evaluate()` returned `None` for every market in the window. Nothing to score.\n")
        lines.append("Possible causes: the candidate's filters are too tight, or the market family it targets isn't in the captured universe (e.g., tickers outside `_active_series_tickers()` from `bot/universe.py` or pre-Apr-25 windows before `universe.jsonl` shipped). Check `tools/universe_report.py` for ignored families that might match.\n")
        return "\n".join(lines)

    # Aggregate stats — Session 73: per-unique-pair-key is the headline.
    lines.append("## Aggregate (resolved subset)\n")
    lines.append("**Per unique pair-key (HEADLINE):**\n")
    lines.append(
        f"- Mean CLV: **{_fmt_cents(summary['mean_clv_cents_per_pair_key']).strip()}¢**"
    )
    lines.append(
        f"- Win rate: **{_fmt_pct(summary['win_rate_pct_per_pair_key']).strip()}**"
    )
    lines.append(
        f"- Total hypothetical P&L: "
        f"**{_fmt_dollars(summary['total_pnl_cents_per_pair_key'])}**"
    )
    lines.append(
        f"- n unique pair-keys: **{summary['n_unique_pair_keys']:,}** "
        f"(resolved: {summary['n_resolved_pair_keys']:,})"
    )
    lines.append("")
    lines.append(
        "**Per emit (DIAGNOSTIC — catches stateful candidate amplification):**\n"
    )
    lines.append(
        f"- Mean CLV: {_fmt_cents(summary['mean_clv_cents']).strip()}¢"
    )
    lines.append(
        f"- Win rate: {_fmt_pct(summary['win_rate_pct']).strip()}"
    )
    lines.append(
        f"- Total hypothetical P&L: {_fmt_dollars(summary['total_pnl_cents'])}"
    )
    lines.append(
        f"- n emits: {summary['n_total']:,} "
        f"(median {summary['median_emits_per_pair_key']:.1f} emits per unique pair-key)"
    )
    lines.append(f"- Settle rate: {summary['settle_rate_pct']:.1f}%")
    lines.append("")
    lines.append(
        "> Stateful candidates re-emit on every scan while a divergence "
        "persists. The per-emit number is inflated by amplification; "
        "per-unique-pair-key is the headline because it matches \"you'd "
        "enter the trade once\" real semantics. A large gap between the two "
        "is itself a signal worth investigating (see Session 73 in CLAUDE.md)."
    )
    lines.append("")

    # Per-sport breakdown
    if summary["per_sport"]:
        lines.append("## Per-sport breakdown\n")
        lines.append("| Sport | n | resolved | Mean CLV ¢ | Σ P&L |")
        lines.append("|---|---|---|---|---|")
        sports = sorted(
            summary["per_sport"].items(),
            key=lambda kv: (-(kv[1]["total_pnl_cents"] or 0), kv[0]),
        )
        for sport, s in sports:
            lines.append(
                f"| {sport} | {s['n']} | {s['n_resolved']} | "
                f"{_fmt_cents(s['mean_clv_cents']).strip()} | "
                f"{_fmt_dollars(s['total_pnl_cents'])} |"
            )
        lines.append("")

    # Per-confidence-decile (only when candidate varies confidence)
    if summary["per_confidence_decile"]:
        lines.append("## Per-confidence-decile breakdown\n")
        lines.append("| Decile | n | resolved | Mean CLV ¢ | Σ P&L |")
        lines.append("|---|---|---|---|---|")
        for bucket in sorted(summary["per_confidence_decile"]):
            s = summary["per_confidence_decile"][bucket]
            lines.append(
                f"| {bucket} | {s['n']} | {s['n_resolved']} | "
                f"{_fmt_cents(s['mean_clv_cents']).strip()} | "
                f"{_fmt_dollars(s['total_pnl_cents'])} |"
            )
        lines.append("")

    # Top winners and losers
    resolved = [s for s in scored if s.status != UNRESOLVED and s.pnl_cents is not None]
    if resolved:
        winners = sorted(resolved, key=lambda s: s.pnl_cents or 0, reverse=True)[:5]
        losers = sorted(resolved, key=lambda s: s.pnl_cents or 0)[:5]
        lines.append("## Top 5 winners\n")
        lines.append("| Ticker | Side | Entry ¢ | Result | CLV ¢ | P&L |")
        lines.append("|---|---|---|---|---|---|")
        for s in winners:
            lines.append(
                f"| `{s.opp.ticker}` | {s.opp.side} | "
                f"{s.opp.target_price_cents:.0f} | {s.market_result or '—'} | "
                f"{_fmt_cents(s.clv_cents).strip()} | {_fmt_dollars(s.pnl_cents)} |"
            )
        lines.append("")
        lines.append("## Top 5 losers\n")
        lines.append("| Ticker | Side | Entry ¢ | Result | CLV ¢ | P&L |")
        lines.append("|---|---|---|---|---|---|")
        for s in losers:
            lines.append(
                f"| `{s.opp.ticker}` | {s.opp.side} | "
                f"{s.opp.target_price_cents:.0f} | {s.market_result or '—'} | "
                f"{_fmt_cents(s.clv_cents).strip()} | {_fmt_dollars(s.pnl_cents)} |"
            )
        lines.append("")

    # Reasons histogram
    reasons = Counter(s.opp.reason for s in scored)
    if reasons:
        lines.append("## Reason histogram\n")
        lines.append("| Reason | Count |")
        lines.append("|---|---|")
        for reason, n in reasons.most_common():
            lines.append(f"| {reason} | {n} |")
        lines.append("")

    return "\n".join(lines)


def write_report(
    candidate_name: str,
    markdown: str,
    *,
    out_dir: Optional[Path] = None,
    today: Optional[date] = None,
) -> Path:
    """Write the report markdown to ``out_dir`` and return the path."""
    if out_dir is None:
        out_dir = REPORTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    if today is None:
        today = datetime.now(timezone.utc).date()
    path = out_dir / f"strategy_lab_{candidate_name}_{today.isoformat()}.md"
    path.write_text(markdown)
    return path
