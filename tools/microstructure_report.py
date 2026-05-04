"""Per-strategy slippage / latency / fill-rate report. Local, .gitignored.

Reads bot/state/order_microstructure.jsonl + last 7 archives. Joins to
clv.json via (ticker, ts_placed) ±60s window using bot.calibration._within_window
— same join pattern Session 13b uses; do NOT reinvent.

CLV math discipline (per Session 13b): clv.json already stores pre-computed
clv_cents via bot.clv.compute_clv_cents (the canonical function). This report
READS clv_cents directly. If we ever need to recompute, use the canonical
function. NO parallel codepath.

slippage_adjusted_clv = clv.clv_cents - microstructure.slippage_cents
  positive slippage (adverse) reduces CLV
  negative slippage (favorable) boosts CLV
  null slippage (rejected / never filled) → null adjusted CLV
  null clv_cents (unsettled) → null adjusted CLV

Flags:
  - slippage > 2¢ median per strategy → execution-quality issue
  - latency > 5s p95 per strategy → execution-quality issue
  - fill rate < 90% per strategy → potential rejection issue
  - (paper-mode CLV - slippage-adjusted CLV) > 2¢ → "paper-mode over-optimistic
    for this strategy" → bake slippage into paper-trade simulation (Session 16+)
"""
from __future__ import annotations

import argparse
import gzip
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

MICROSTRUCTURE_FILE = REPO_ROOT / "bot" / "state" / "order_microstructure.jsonl"
ARCHIVE_DIR = REPO_ROOT / "bot" / "state" / "archive"
CLV_FILE = REPO_ROOT / "bot" / "state" / "clv.json"


def load_microstructure(days: int = 7) -> list[dict]:
    """Load microstructure rows from live + last `days` daily archives."""
    recs: list[dict] = []
    sources: list[tuple[Path, callable]] = []
    if MICROSTRUCTURE_FILE.exists():
        sources.append((MICROSTRUCTURE_FILE, open))
    if ARCHIVE_DIR.exists():
        for gz in sorted(ARCHIVE_DIR.glob("order_microstructure-*.jsonl.gz"))[-days:]:
            sources.append((gz, gzip.open))
    for path, opener in sources:
        try:
            with opener(path, "rt") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        recs.append(json.loads(line))
                    except Exception:
                        continue
        except Exception:
            continue
    return recs


def compute_slippage_adjusted_clv(micro: dict, clv: dict) -> float | None:
    """Adjust paper-CLV for execution slippage. Returns None when slippage
    is unmeasurable (synchronous rejection / never filled) or CLV is unsettled."""
    slip = micro.get("slippage_cents")
    if slip is None:
        return None
    clv_c = clv.get("clv_cents")
    if clv_c is None:
        return None
    return round(clv_c - slip, 2)


def join_microstructure_to_clv(micros: list[dict], clvs: list[dict]) -> list[tuple[dict, dict | None]]:
    """For each microstructure row, find the matching clv record by
    (ticker, ts_placed within ±60s of recorded_at). REUSES bot.calibration._within_window
    and _parse_iso (Session 13b precedent — do NOT reinvent the helper)."""
    from bot.calibration import _parse_iso, _within_window

    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for c in clvs:
        if c.get("status") not in ("settled", "counterfactual_settled"):
            continue
        by_ticker[c.get("ticker", "")].append(c)
    pairs: list[tuple[dict, dict | None]] = []
    for m in micros:
        anchor = _parse_iso(m.get("ts_placed"))
        match = None
        if anchor is not None:
            for c in by_ticker.get(m.get("ticker", ""), []):
                if _within_window(c.get("recorded_at"), anchor):
                    match = c
                    break
        pairs.append((m, match))
    return pairs


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    idx = max(0, min(len(sorted_values) - 1, int(round(pct * (len(sorted_values) - 1)))))
    return sorted_values[idx]


def report(days: int = 7) -> str:
    micros = load_microstructure(days=days)
    if not micros:
        return (
            "No microstructure rows found.\n"
            "Verification deferred until PAPER_MODE=False — paper-mode trades "
            "do not write to order_microstructure.jsonl by design."
        )
    try:
        clvs = json.loads(CLV_FILE.read_text()) if CLV_FILE.exists() else []
    except Exception:
        clvs = []
    pairs = join_microstructure_to_clv(micros, clvs)

    by_strategy: dict[str, list[tuple[dict, dict | None]]] = defaultdict(list)
    for m, c in pairs:
        by_strategy[m.get("strategy_name", "unknown")].append((m, c))

    lines = [f"# Microstructure Report — last {days} days", ""]
    lines.append(f"Total orders: {len(micros)}")
    lines.append("")
    for strat in sorted(by_strategy):
        rows = by_strategy[strat]
        lines.append(f"## {strat} (n={len(rows)})")
        slips = [m["slippage_cents"] for m, _ in rows if m.get("slippage_cents") is not None]
        lats = [m["latency_ms"] for m, _ in rows if m.get("latency_ms") is not None]
        filled = [m for m, _ in rows if m.get("terminal_status") == "filled"]
        partials = [m for m, _ in rows if (m.get("partial_fill_count", 0) > 0)]
        fill_rate = len(filled) / max(len(rows), 1)
        partial_rate = len(partials) / max(len(rows), 1)

        if slips:
            lines.append(
                f"  slippage_cents: median={statistics.median(slips):+.2f}, "
                f"mean={statistics.mean(slips):+.2f}, n={len(slips)}"
            )
        if lats:
            lats_sorted = sorted(lats)
            p95 = _percentile(lats_sorted, 0.95)
            lines.append(
                f"  latency_ms: median={statistics.median(lats):.0f}, p95={p95:.0f}"
            )
        lines.append(f"  fill_rate: {fill_rate:.1%} ({len(filled)}/{len(rows)})")
        lines.append(f"  partial_fill_rate: {partial_rate:.1%}")

        # Slippage-adjusted CLV
        adj_clvs: list[float] = []
        raw_clvs: list[float] = []
        for m, c in rows:
            if c is None:
                continue
            raw = c.get("clv_cents")
            adj = compute_slippage_adjusted_clv(m, c)
            if raw is not None:
                raw_clvs.append(raw)
            if adj is not None:
                adj_clvs.append(adj)
        if raw_clvs and adj_clvs:
            mean_raw = statistics.mean(raw_clvs)
            mean_adj = statistics.mean(adj_clvs)
            lines.append(
                f"  CLV: paper={mean_raw:+.2f}¢, "
                f"slippage-adjusted={mean_adj:+.2f}¢ (n={len(adj_clvs)})"
            )
            if mean_raw - mean_adj > 2:
                lines.append(
                    f"  ⚠️  PAPER-MODE OVER-OPTIMISTIC for {strat}: "
                    f"adjustment >2¢. Bake slippage into paper-trade simulation."
                )

        # Flags
        if slips and statistics.median(slips) > 2:
            lines.append("  ⚠️  Median slippage > 2¢ — execution-quality issue")
        if lats and len(lats) >= 20:
            lats_sorted = sorted(lats)
            p95 = _percentile(lats_sorted, 0.95)
            if p95 > 5000:
                lines.append("  ⚠️  P95 latency > 5s — execution-quality issue")
        if fill_rate < 0.9 and len(rows) >= 20:
            lines.append("  ⚠️  Fill rate < 90% — potential rejection issue")
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()
    print(report(days=args.days))
