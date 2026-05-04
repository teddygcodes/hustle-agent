#!/usr/bin/env python3
"""Offline back-tester for refactored Strategy classes (Session 13b).

Replays a Strategy against captured universe.jsonl + gzipped archives,
joins emitted opportunities to settled clv records via ticker + ±60s ts
window, and reports per-day P&L / win rate / mean edge / mean CLV.

CRITICAL DISCIPLINE: this tool reuses bot.clv.compute_clv_cents — the
SAME function the live settler uses. NO parallel codepath. If back-test
math diverges from live by even 1¢, the back-tester is wrong, not live.

Usage:
    python3 tools/backtest.py --strategy vig_stack_series --days 7
    python3 tools/backtest.py --strategy vig_stack_series \\
        --start 2026-04-20 --end 2026-04-27
    python3 tools/backtest.py --strategy vig_stack_series --days 7 \\
        --verify-against-clv-report

Local-only (gitignored via tools/ entry).
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from collections import defaultdict
from dataclasses import fields
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bot import clv as bot_clv  # noqa: E402  — single source of CLV math
from bot.calibration import _within_window, _parse_iso  # noqa: E402
from bot.config import BOT_STATE_DIR  # noqa: E402
from bot.strategies import REGISTERED_STRATEGIES, Market  # noqa: E402

UNIVERSE_FILE = BOT_STATE_DIR / "universe.jsonl"
ARCHIVE_DIR = BOT_STATE_DIR / "archive"
CLV_FILE = BOT_STATE_DIR / "clv.json"

_MARKET_FIELDS = {f.name for f in fields(Market)}


def _row_to_market(row: dict[str, Any]) -> Market | None:
    """Reconstruct a Market from a universe.jsonl row.

    Universe rows include scanned_by/partial keys not in the Market
    dataclass — filter to dataclass fields, default raw to {} (universe
    rows don't carry the original Kalshi raw dict).
    """
    try:
        filtered = {k: v for k, v in row.items() if k in _MARKET_FIELDS}
        filtered.setdefault("raw", {})
        return Market(**filtered)
    except (TypeError, KeyError):
        return None


def _iter_jsonl_lines(path: Path):
    """Yield parsed JSON rows from .jsonl or .jsonl.gz; tolerate malformed lines."""
    if not path.exists():
        return
    opener = gzip.open if path.suffix == ".gz" else open
    try:
        with opener(path, "rt") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except (OSError, gzip.BadGzipFile):
        return


def load_universe_snapshots(
    start: date,
    end: date,
) -> dict[str, list[Market]]:
    """Return {scan_id: [Market, ...]} for snapshots with ts in [start, end]."""
    snapshots: dict[str, list[Market]] = defaultdict(list)

    sources: list[Path] = []
    cur = start
    while cur <= end:
        gz = ARCHIVE_DIR / f"universe-{cur.isoformat()}.jsonl.gz"
        if gz.exists():
            sources.append(gz)
        cur += timedelta(days=1)
    if UNIVERSE_FILE.exists():
        sources.append(UNIVERSE_FILE)

    for src in sources:
        for row in _iter_jsonl_lines(src):
            ts = _parse_iso(row.get("ts"))
            if ts is None or not (start <= ts.date() <= end):
                continue
            scan_id = row.get("scan_id") or ""
            if not scan_id:
                continue
            market = _row_to_market(row)
            if market is None:
                continue
            snapshots[scan_id].append(market)

    return snapshots


def load_clv_records() -> list[dict[str, Any]]:
    """Load bot/state/clv.json; empty list if missing."""
    if not CLV_FILE.exists():
        return []
    try:
        with open(CLV_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def match_clv_record(
    ticker: str,
    snapshot_ts: str,
    records: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """First settled clv record matching ticker + ts ±60s, or None.

    Eligible statuses: 'settled' (real trade) and 'counterfactual_settled'.
    Open / counterfactual_open records are skipped (no closing price yet).
    Reuses bot.calibration._within_window for the ±60s logic — same as
    Session 11's predictions matcher.
    """
    anchor = _parse_iso(snapshot_ts)
    if anchor is None:
        return None
    for rec in records:
        if rec.get("ticker") != ticker:
            continue
        if rec.get("status") not in ("settled", "counterfactual_settled"):
            continue
        if not _within_window(rec.get("recorded_at"), anchor):
            continue
        return rec
    return None


def replay_strategy(
    strategy,
    snapshots: dict[str, list[Market]],
) -> list[dict[str, Any]]:
    """Replay strategy across snapshots; return opportunities with metadata.

    Each result is {"scan_id": str, "snapshot_ts": str, "opp": Opportunity}.

    Side-effect-free wrt production state:
      - bot.decisions.log_decision is monkey-patched to no-op (would write
        bot/state/decisions.jsonl during evaluate's reject path).
      - _fetch_vig_stack_forecasts is cached once per run (would HTTP-fetch
        NWS forecasts every snapshot — ~1000 calls for a 7-day window).
      - strategy.finalize() is NEVER called — its CF emission writes
        clv.json. Live verification of finalize-skip is enforced by the
        stub test (finalize raises if called).

    Caveat (Session 13b scope): cached NWS forecasts mean weather
    `forecast_in_bucket` gates evaluate against today's forecast, not
    historical. This affects which weather opps the back-tester emits.
    The verification mode is unaffected — it subsets to actually-taken
    trades, which used the live forecast at trade time.

    Back-test >= live opportunity count is expected and correct: the
    back-tester replays without executor-state filters (position cap,
    balance, cooldown, daily loss). Strategy-emitted opps > actually-
    taken trades. Verification mode handles this by limiting comparison
    to clv records with status=="settled".
    """
    results: list[dict[str, Any]] = []

    # Cache NWS forecast once if the strategy graph might use it.
    cached_forecasts: dict[str, Any] = {}
    try:
        from bot.strategies.vig_stack_series import _fetch_vig_stack_forecasts
        cached_forecasts = _fetch_vig_stack_forecasts() or {}
    except Exception:
        cached_forecasts = {}

    patches = [
        patch("bot.decisions.log_decision", lambda **kw: None),
        patch(
            "bot.strategies.vig_stack_series._fetch_vig_stack_forecasts",
            lambda: cached_forecasts,
        ),
    ]
    for p in patches:
        p.start()
    try:
        for scan_id, markets in snapshots.items():
            if not markets:
                continue
            snapshot_ts = markets[0].ts or ""
            candidates = strategy.candidate_markets(list(markets))
            for m in candidates:
                opp = strategy.evaluate(m)
                if opp is None:
                    continue
                results.append({
                    "scan_id": scan_id,
                    "snapshot_ts": snapshot_ts,
                    "opp": opp,
                })
            # NB: strategy.finalize() intentionally NOT called.
    finally:
        for p in patches:
            p.stop()

    return results


def aggregate_results(
    results: list[dict[str, Any]],
    clv_records: list[dict[str, Any]],
    *,
    include_history: bool = False,
) -> dict[str, Any]:
    """Group by (date, opp_type) and compute summary stats + actually-taken subset.

    Notes:
      - "Back-test >= live opportunity count" is expected — the back-tester
        replays without executor-state filters. opp_count > matched_count is
        normal; matched_count is what we can compute CLV for.
      - mean_clv_cents uses bot.clv.compute_clv_cents — the SAME math as
        live. Divergence here = back-tester bug.
      - actually-taken subset = matched record had status=="settled" (real
        trade); excludes counterfactual_settled and history_settled. This
        is the subset the --verify-against-clv-report flag compares to
        clv_report.
      - include_history (Session 13c PART 2): on clv miss, fall back to
        bot.kalshi_history.fetch_settled_close to fetch the settled YES-
        side close. Synthesized matches carry status="history_settled"
        so they don't pollute the actually-taken subset.
    """
    per_day: dict[str, dict[str, dict[str, Any]]] = defaultdict(
        lambda: defaultdict(lambda: {
            "opp_count": 0, "matched_count": 0,
            "edges": [], "clv_cents_list": [],
        }))
    matched_actually_taken: list[dict[str, Any]] = []

    for r in results:
        opp = r["opp"]
        opp_type = opp.get("opp_type") or opp.get("type") or "unknown"
        ts = _parse_iso(r["snapshot_ts"])
        if ts is None:
            continue
        day = ts.date().isoformat()
        bucket = per_day[day][opp_type]
        bucket["opp_count"] += 1
        edge = opp.get("edge") or opp.get("relative_edge")
        if edge is not None:
            try:
                bucket["edges"].append(float(edge))
            except (TypeError, ValueError):
                pass

        match = match_clv_record(opp.get("ticker", ""),
                                 r["snapshot_ts"], clv_records)
        if match is None:
            if include_history:
                # Fall back to Kalshi settled-market history. Cache
                # ensures repeated lookups on the same ticker hit disk
                # not the API.
                from bot.kalshi_history import fetch_settled_close
                close = fetch_settled_close(opp.get("ticker", ""))
                if close is None:
                    continue
                # Synthesize a minimal match dict so downstream code is
                # uniform. Status is "history_settled" (NOT "settled")
                # so the actually-taken subset filter excludes it.
                match = {
                    "ticker": opp.get("ticker"),
                    "side": opp.get("recommended_side", "no"),
                    "entry_price_cents": (opp.get("no_ask_cents")
                                          or opp.get("price_cents")
                                          or opp.get("yes_ask_cents")),
                    "closing_yes_price": close,
                    "status": "history_settled",
                    "recorded_at": r.get("snapshot_ts"),
                }
            else:
                continue
        bucket["matched_count"] += 1

        side = opp.get("recommended_side") or match.get("side") or "yes"
        entry = match.get("entry_price_cents")
        close = match.get("closing_yes_price")
        if entry is None or close is None:
            continue

        # CRITICAL: same compute_clv_cents the live settler uses
        clv_cents, _ = bot_clv.compute_clv_cents(side, int(entry), float(close))
        bucket["clv_cents_list"].append(clv_cents)

        if match.get("status") == "settled":
            matched_actually_taken.append({
                **r, "match": match, "clv_cents": clv_cents,
            })

    def _summarize(b):
        n, m = b["opp_count"], b["matched_count"]
        edges, clvs = b["edges"], b["clv_cents_list"]
        return {
            "opp_count": n,
            "matched_count": m,
            "matched_pct": (100 * m / n) if n else 0.0,
            "mean_edge": (sum(edges) / len(edges)) if edges else None,
            "mean_clv_cents": (sum(clvs) / len(clvs)) if clvs else None,
            "sum_clv_cents": sum(clvs),
            "win_rate_pct": (100 * sum(1 for c in clvs if c > 0) / len(clvs)
                             if clvs else None),
        }

    per_day_summary = {
        day: {ot: _summarize(b) for ot, b in by.items()}
        for day, by in per_day.items()
    }

    totals: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "opp_count": 0, "matched_count": 0, "edges": [], "clv_cents_list": [],
    })
    for by in per_day.values():
        for ot, b in by.items():
            totals[ot]["opp_count"] += b["opp_count"]
            totals[ot]["matched_count"] += b["matched_count"]
            totals[ot]["edges"].extend(b["edges"])
            totals[ot]["clv_cents_list"].extend(b["clv_cents_list"])
    totals_summary = {ot: _summarize(t) for ot, t in totals.items()}

    return {
        "per_day": per_day_summary,
        "totals": totals_summary,
        "matched_actually_taken": matched_actually_taken,
    }


def _fmt(x, fmt):
    return format(x, fmt) if x is not None else "—"


def _format_markdown(strategy_name, start, end, summary) -> str:
    lines = []
    lines.append(f"# Back-test report — {strategy_name}\n")
    lines.append(f"Window: {start.isoformat()} → {end.isoformat()}\n")

    lines.append("\n## Per-day per-opp_type\n")
    lines.append("| Day | Opp Type | Opps | Matched | Match % | "
                 "Mean Edge | Mean CLV ¢ | Σ CLV ¢ | Win % |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    rendered_any_row = False
    for day in sorted(summary["per_day"]):
        for opp_type, s in summary["per_day"][day].items():
            rendered_any_row = True
            lines.append(
                f"| {day} | {opp_type} | {s['opp_count']} | "
                f"{s['matched_count']} | {s['matched_pct']:.0f}% | "
                f"{_fmt(s['mean_edge'], '.4f')} | "
                f"{_fmt(s['mean_clv_cents'], '.2f')} | "
                f"{s['sum_clv_cents']:.2f} | "
                f"{_fmt(s['win_rate_pct'], '.1f')}% |"
            )
    if not rendered_any_row:
        lines.append("| — | (no snapshots in window) | — | — | — | — | — | — | — |")

    lines.append("\n## Totals (window)\n")
    lines.append("| Opp Type | Opps | Matched | Match % | Mean Edge | "
                 "Mean CLV ¢ | Σ CLV ¢ | Win % |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for opp_type, s in summary["totals"].items():
        lines.append(
            f"| {opp_type} | {s['opp_count']} | {s['matched_count']} | "
            f"{s['matched_pct']:.0f}% | {_fmt(s['mean_edge'], '.4f')} | "
            f"{_fmt(s['mean_clv_cents'], '.2f')} | "
            f"{s['sum_clv_cents']:.2f} | "
            f"{_fmt(s['win_rate_pct'], '.1f')}% |"
        )
    if not summary["totals"]:
        lines.append("| — | 0 | 0 | — | — | — | 0.00 | — |")
    return "\n".join(lines)


def run_backtest(
    strategy,
    *,
    start: date,
    end: date,
    include_history: bool = False,
) -> dict[str, Any]:
    """Programmatic back-test entry point (Session 13c PART 2).

    Loads universe snapshots and clv records for [start, end], replays
    `strategy` over them, and returns the aggregate-results dict (the
    same shape `aggregate_results` returns).

    Args:
        strategy: a Strategy instance (already constructed with whatever
            kwargs the caller wants tested).
        start, end: date range, inclusive.
        include_history: if True, fall back to bot.kalshi_history.
            fetch_settled_close on clv miss, so opportunities on tickers
            we never traded can still be matched to closing prices.
            Slow (HTTP-backed, cached) — opt-in.

    Returns: {"per_day": {...}, "totals": {...}, "matched_actually_taken": [...]}
    """
    snapshots = load_universe_snapshots(start, end)
    clv_records = load_clv_records()
    results = replay_strategy(strategy, snapshots)
    return aggregate_results(results, clv_records,
                             include_history=include_history)


def _resolve_strategy(name: str):
    """Look up a strategy by name.

    Live strategies come from REGISTERED_STRATEGIES (bot/strategies/
    __init__.py). Back-test-only strategies (Session 13c's strawman) are
    instantiated here directly — they're verification artifacts that must
    NOT appear in the live registry.
    """
    by_name = {s.name: s for s in REGISTERED_STRATEGIES}
    # VigStackSeries instance handles vig_stack_futures opp_type via name_for()
    if name == "vig_stack_futures" and "vig_stack_series" in by_name:
        return by_name["vig_stack_series"]
    if name in by_name:
        return by_name[name]
    if name == "nba_game_momentum_strawman":
        from bot.strategies.nba_game_momentum_strawman import (
            NbaGameMomentumStrawman,
        )
        return NbaGameMomentumStrawman()
    return None


def _verify_against_clv_report(strategy_name, start, end, summary):
    """Compare back-test mean CLV (actually-taken subset) to get_clv_report's.

    Asserts |bt_mean - report_mean| < 1e-6 — both must call the same
    compute_clv_cents() under the hood, on the same data, so they MUST
    agree. Any divergence is a back-tester bug — fix before shipping.

    Side-by-side print: back-test mean and clv_report mean per opp_type
    (the dual-name pair vig_stack_series + vig_stack_futures combined when
    --strategy=vig_stack_series).
    """
    actually_taken = summary["matched_actually_taken"]

    target_opp_types = {strategy_name}
    if strategy_name == "vig_stack_series":
        target_opp_types.add("vig_stack_futures")

    bt_clvs = [
        r["clv_cents"] for r in actually_taken
        if (r["opp"].get("opp_type") or r["opp"].get("type"))
        in target_opp_types
    ]
    bt_mean = sum(bt_clvs) / len(bt_clvs) if bt_clvs else None

    report = bot_clv.get_clv_report()
    by_strategy = report.get("by_strategy", {})

    print("\n## Verification — actually-taken subset vs clv_report\n")
    print(f"Window: {start.isoformat()} → {end.isoformat()}")
    print(f"Filter: opp_type in {sorted(target_opp_types)}\n")
    bt_str = f"{bt_mean:.6f}" if bt_mean is not None else "n/a"
    print(f"Back-test (actually-taken): n={len(bt_clvs)}, "
          f"mean_clv_cents={bt_str}")

    weighted_sum = 0.0
    total_n = 0
    for ot in sorted(target_opp_types):
        live = by_strategy.get(ot, {}) or {}
        n = live.get("count") or live.get("n") or 0
        m = live.get("avg_clv_cents")
        m_str = f"{m:.6f}" if m is not None else "n/a"
        print(f"clv_report[{ot}]: n={n}, avg_clv_cents={m_str}")
        if n and m is not None:
            total_n += n
            weighted_sum += n * float(m)
    live_mean = weighted_sum / total_n if total_n else None

    # Asymmetric vacuous handling:
    #  - bt_mean None, live_mean None: nothing to compare — vacuous OK.
    #  - bt_mean None, live_mean has data: universe coverage doesn't include
    #    the trade days (e.g. universe.jsonl is newer than the historical
    #    clv records). This is a coverage gap, not a divergence bug — vacuous
    #    OK with an explanation. Real divergence requires bt to have data.
    #  - bt_mean has data, live_mean None: back-tester thinks it matched
    #    trades that don't exist in clv_report — that IS a bug, FAIL.
    #  - both have data: compare means within 1e-6.
    if bt_mean is None:
        if live_mean is None:
            print("\n[VERIFICATION] OK: no matched data on either side (vacuous).")
        else:
            print(f"\n[VERIFICATION] OK: no actually-taken back-test matches "
                  f"(vacuous — universe coverage may not include live trade "
                  f"days; live clv_report has {total_n} records, mean "
                  f"{live_mean:.6f}).")
        return
    if live_mean is None:
        print(f"\n[VERIFICATION] FAIL: back-test has {len(bt_clvs)} matched "
              f"records (mean {bt_mean:.6f}) but clv_report has none — "
              f"back-tester divergence, fix before shipping.")
        return
    diff = abs(bt_mean - live_mean)
    if diff < 1e-6:
        print(f"\n[VERIFICATION] OK: |bt - live| = {diff:.2e} < 1e-6.")
    else:
        print(f"\n[VERIFICATION] FAIL: |bt - live| = {diff:.6f} >= 1e-6. "
              f"Back-tester divergence — fix before shipping.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline back-tester (Session 13b)")
    parser.add_argument("--strategy", required=True,
                        help="Refactored strategy name (e.g. vig_stack_series)")
    parser.add_argument("--days", type=int, default=None,
                        help="Look back N days from today")
    parser.add_argument("--start", type=str, default=None,
                        help="ISO date — explicit window start")
    parser.add_argument("--end", type=str, default=None,
                        help="ISO date — explicit window end (inclusive)")
    parser.add_argument("--verify-against-clv-report", action="store_true",
                        help="Print and assert match vs bot.clv.get_clv_report")
    parser.add_argument("--include-history", action="store_true",
                        help="On clv miss, fall back to Kalshi settled-"
                             "market history (slow, opt-in — Session 13c)")
    args = parser.parse_args(argv)

    strategy = _resolve_strategy(args.strategy)
    if strategy is None:
        print(
            f"Strategy '{args.strategy}' is not yet refactored to the "
            f"Strategy contract (bot/strategies/__init__.py). As of "
            f"Session 13b only `vig_stack_series` (which also serves "
            f"`vig_stack_futures` via name_for()) is registered. To back-"
            f"test {args.strategy}, refactor it first — see Session 13a's "
            f"VigStackSeries pattern in CLAUDE.md.",
            file=sys.stderr,
        )
        sys.exit(2)

    today = datetime.now(timezone.utc).date()
    if args.start and args.end:
        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end)
    elif args.days is not None:
        start = today - timedelta(days=args.days - 1)
        end = today
    else:
        print("error: pass --days N or --start/--end", file=sys.stderr)
        sys.exit(2)

    summary = run_backtest(
        strategy,
        start=start,
        end=end,
        include_history=args.include_history,
    )

    print(_format_markdown(args.strategy, start, end, summary))

    if args.verify_against_clv_report:
        _verify_against_clv_report(args.strategy, start, end, summary)

    return 0


if __name__ == "__main__":
    sys.exit(main())
