"""Session 119: sample MATCH_HIGH_CONFIDENCE pairs for Codex labeling.

Loads the cross-platform labeling queue, runs the S117 matcher over every row,
filters to unlabeled rows the matcher classifies as MATCH_HIGH_CONFIDENCE, and
stratified-samples 40 pairs: 20 boundary (lowest matcher Jaccard, the matcher's
most-borderline positive classifications) + 20 interior (random from the rest).

Writes the sampled pairs to bot/state/codex_sampling_match_high_confidence.jsonl
as the Codex labeling input. Does NOT modify the main queue; a separate step
writes Codex's labels back to the queue.
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.cross_platform_matcher import MatchResult, match_markets


DEFAULT_QUEUE_PATH = ROOT / "bot" / "state" / "cross_platform_labeling_queue.jsonl"
DEFAULT_OUTPUT_PATH = ROOT / "bot" / "state" / "codex_sampling_match_high_confidence.jsonl"
DEFAULT_SEED = 42
DEFAULT_BOUNDARY_N = 20
DEFAULT_INTERIOR_N = 20


def load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open() as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def write_jsonl(records: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in records:
            f.write(json.dumps(r, sort_keys=True) + "\n")


def row_to_markets(row: dict) -> tuple[dict, dict]:
    kalshi = {
        "venue": "kalshi",
        "ticker": row.get("kalshi_ticker"),
        "question_text": row.get("kalshi_question"),
        "close_date": row.get("kalshi_close_date"),
        "resolved_outcome": row.get("kalshi_result"),
        "category": row.get("kalshi_category"),
        "resolution_source": row.get("kalshi_resolution_source"),
    }
    polymarket = {
        "venue": "polymarket",
        "ticker": row.get("polymarket_ticker"),
        "question_text": row.get("polymarket_question"),
        "close_date": row.get("polymarket_close_date"),
        "resolved_outcome": row.get("polymarket_result"),
        "category": row.get("polymarket_category"),
        "resolution_source": row.get("polymarket_resolution_source"),
    }
    return kalshi, polymarket


def classify_high_confidence_unlabeled(rows: list[dict]) -> list[tuple[dict, float]]:
    """Return [(row, matcher_jaccard), ...] for unlabeled MATCH_HIGH_CONFIDENCE rows."""
    out: list[tuple[dict, float]] = []
    for row in rows:
        if row.get("operator_label") is not None:
            continue
        kalshi, polymarket = row_to_markets(row)
        decision = match_markets(kalshi, polymarket)
        if decision.result == MatchResult.MATCH_HIGH_CONFIDENCE:
            out.append((row, decision.jaccard))
    return out


def stratified_sample(
    pool: list[tuple[dict, float]],
    boundary_n: int,
    interior_n: int,
    seed: int,
) -> list[dict]:
    """Boundary-N (lowest jaccard, deterministic) + Interior-N (random).

    Each sample row is annotated with stratum + matcher_jaccard.
    Composite tiebreaker on (jaccard, kalshi_ticker, polymarket_ticker) keeps
    boundary selection deterministic when many rows share the same jaccard.
    """
    if len(pool) < boundary_n + interior_n:
        raise ValueError(
            f"pool has {len(pool)} eligible rows; "
            f"need at least {boundary_n + interior_n}"
        )

    def sort_key(item: tuple[dict, float]) -> tuple:
        row, jacc = item
        return (jacc, row.get("kalshi_ticker") or "", row.get("polymarket_ticker") or "")

    sorted_pool = sorted(pool, key=sort_key)
    boundary = sorted_pool[:boundary_n]
    rest = sorted_pool[boundary_n:]
    rng = random.Random(seed)
    interior = rng.sample(rest, interior_n)

    samples: list[dict] = []
    for row, jacc in boundary:
        out = dict(row)
        out["stratum"] = "boundary"
        out["matcher_jaccard"] = jacc
        samples.append(out)
    for row, jacc in interior:
        out = dict(row)
        out["stratum"] = "interior"
        out["matcher_jaccard"] = jacc
        samples.append(out)
    return samples


def _stratum_summary(samples: list[dict], stratum: str) -> str:
    jaccards = [s["matcher_jaccard"] for s in samples if s["stratum"] == stratum]
    if not jaccards:
        return f"{stratum}: 0 samples"
    return (
        f"{stratum}: n={len(jaccards)} jaccard "
        f"min={min(jaccards):.4f} median={statistics.median(jaccards):.4f} "
        f"max={max(jaccards):.4f}"
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--queue-path", default=str(DEFAULT_QUEUE_PATH))
    parser.add_argument("--output-path", default=str(DEFAULT_OUTPUT_PATH))
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--boundary-n", type=int, default=DEFAULT_BOUNDARY_N)
    parser.add_argument("--interior-n", type=int, default=DEFAULT_INTERIOR_N)
    args = parser.parse_args(argv)

    queue_path = Path(args.queue_path)
    output_path = Path(args.output_path)

    rows = load_jsonl(queue_path)
    print(f"Loaded {len(rows)} rows from {queue_path}")

    pool = classify_high_confidence_unlabeled(rows)
    if not pool:
        print("No unlabeled MATCH_HIGH_CONFIDENCE rows found; nothing to sample.")
        return 1

    jaccards = [j for _, j in pool]
    print(
        f"Unlabeled MATCH_HIGH_CONFIDENCE pool: n={len(pool)} "
        f"jaccard min={min(jaccards):.4f} median={statistics.median(jaccards):.4f} "
        f"max={max(jaccards):.4f}"
    )

    samples = stratified_sample(pool, args.boundary_n, args.interior_n, args.seed)
    write_jsonl(samples, output_path)

    print(f"Wrote {len(samples)} samples to {output_path}")
    print(_stratum_summary(samples, "boundary"))
    print(_stratum_summary(samples, "interior"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
