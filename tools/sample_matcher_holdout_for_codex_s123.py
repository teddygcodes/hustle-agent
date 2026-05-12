"""Session 123: sample fresh v3 matcher holdout-2 pairs for Codex labeling.

Mirrors the S121 sampler protocol exactly so the two holdouts are comparable.
Runs the deterministic v3 matcher over the full S116 queue (already stamped
post-S122), partitions unlabeled rows into HIGH_CONFIDENCE / NEEDS_REVIEW /
NO_MATCH pools, and samples 40 previously unlabeled pairs via the same
boundary-weighted stratified strategy as S121 (seed=123 instead of 121).
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
DEFAULT_OUTPUT_PATH = ROOT / "bot" / "state" / "codex_sampling_matcher_holdout_s123.jsonl"
DEFAULT_SEED = 123


def load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open() as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def write_jsonl(records: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        for record in records:
            f.write(json.dumps(record, sort_keys=True) + "\n")
    tmp.replace(path)


def row_to_markets(row: dict) -> tuple[dict, dict]:
    kalshi = {
        "venue": "kalshi",
        "ticker": row.get("kalshi_ticker"),
        "question_text": row.get("kalshi_question"),
        "close_date": row.get("kalshi_close_date"),
        "resolved_outcome": row.get("kalshi_result"),
        "category": row.get("kalshi_category"),
        "resolution_source": row.get("kalshi_resolution_source"),
        "url": row.get("kalshi_url"),
    }
    polymarket = {
        "venue": "polymarket",
        "ticker": row.get("polymarket_ticker"),
        "question_text": row.get("polymarket_question"),
        "close_date": row.get("polymarket_close_date"),
        "resolved_outcome": row.get("polymarket_result"),
        "category": row.get("polymarket_category"),
        "resolution_source": row.get("polymarket_resolution_source"),
        "url": row.get("polymarket_url"),
    }
    return kalshi, polymarket


def annotate_with_matcher(rows: list[dict]) -> dict[str, list[tuple[dict, object]]]:
    pools: dict[str, list[tuple[dict, object]]] = {
        MatchResult.MATCH_HIGH_CONFIDENCE.name: [],
        MatchResult.MATCH_NEEDS_REVIEW.name: [],
        MatchResult.NO_MATCH.name: [],
    }
    for row in rows:
        kalshi, polymarket = row_to_markets(row)
        decision = match_markets(kalshi, polymarket)
        classification = decision.result.name
        row["matcher_classification"] = classification
        row["matcher_jaccard"] = decision.jaccard
        row["matcher_reason"] = decision.reason
        if row.get("operator_label") is None and classification in pools:
            pools[classification].append((row, decision))
    return pools


def _sort_key(item: tuple[dict, object]) -> tuple:
    row, decision = item
    return (
        decision.jaccard,
        row.get("kalshi_ticker") or "",
        row.get("polymarket_ticker") or "",
    )


def _sample_random(pool: list[tuple[dict, object]], n: int, seed: int) -> list[tuple[dict, object]]:
    if len(pool) < n:
        raise ValueError(f"pool has {len(pool)} eligible rows; need {n}")
    return random.Random(seed).sample(pool, n)


def _sample_holdout(pools: dict[str, list[tuple[dict, object]]], seed: int) -> list[dict]:
    high_pool = sorted(pools[MatchResult.MATCH_HIGH_CONFIDENCE.name], key=_sort_key)
    if len(high_pool) < 15:
        raise ValueError(f"HIGH_CONFIDENCE pool has {len(high_pool)} eligible rows; need 15")
    boundary = high_pool[:8]
    interior = _sample_random(high_pool[8:], 7, seed + 1)
    needs_review = _sample_random(pools[MatchResult.MATCH_NEEDS_REVIEW.name], 15, seed + 2)
    no_match = _sample_random(pools[MatchResult.NO_MATCH.name], 10, seed + 3)

    samples: list[tuple[str, tuple[dict, object]]] = []
    samples.extend(("high_confidence_boundary", item) for item in boundary)
    samples.extend(("high_confidence_interior", item) for item in interior)
    samples.extend(("needs_review_random", item) for item in needs_review)
    samples.extend(("no_match_random", item) for item in no_match)

    output: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for index, (stratum, (row, decision)) in enumerate(samples, start=1):
        key = (row.get("kalshi_ticker"), row.get("polymarket_ticker"))
        if key in seen:
            raise ValueError(f"duplicate sample key {key}")
        seen.add(key)
        sampled = dict(row)
        sampled["sample_session"] = "S123"
        sampled["sample_index"] = index
        sampled["sample_stratum"] = stratum
        sampled["matcher_classification"] = decision.result.name
        sampled["matcher_jaccard"] = decision.jaccard
        sampled["matcher_reason"] = decision.reason
        output.append(sampled)
    return output


def _summary_line(name: str, pool: list[tuple[dict, object]]) -> str:
    values = [decision.jaccard for _, decision in pool]
    if not values:
        return f"{name}: n=0"
    return (
        f"{name}: n={len(values)} jaccard "
        f"min={min(values):.4f} median={statistics.median(values):.4f} max={max(values):.4f}"
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--queue-path", default=str(DEFAULT_QUEUE_PATH))
    parser.add_argument("--output-path", default=str(DEFAULT_OUTPUT_PATH))
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args(argv)

    queue_path = Path(args.queue_path)
    output_path = Path(args.output_path)
    rows = load_jsonl(queue_path)
    pools = annotate_with_matcher(rows)
    samples = _sample_holdout(pools, args.seed)

    labeled_keys = {
        (r.get("kalshi_ticker"), r.get("polymarket_ticker"))
        for r in rows
        if r.get("operator_label") is not None
    }
    sample_keys = {
        (s.get("kalshi_ticker"), s.get("polymarket_ticker")) for s in samples
    }
    overlap = labeled_keys & sample_keys
    if overlap:
        raise RuntimeError(
            f"holdout integrity violation: {len(overlap)} sampled pairs overlap with "
            f"existing labeled rows: {sorted(overlap)[:3]}..."
        )

    write_jsonl(rows, queue_path)
    write_jsonl(samples, output_path)

    print(f"Loaded and stamped {len(rows)} rows in {queue_path}")
    for key in (
        MatchResult.MATCH_HIGH_CONFIDENCE.name,
        MatchResult.MATCH_NEEDS_REVIEW.name,
        MatchResult.NO_MATCH.name,
    ):
        print(_summary_line(key, pools[key]))
    print(f"Wrote {len(samples)} samples to {output_path}")
    print(f"Holdout integrity: 0 overlap with {len(labeled_keys)} existing labeled rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
