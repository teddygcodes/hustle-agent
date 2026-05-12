"""Validate the deterministic cross-platform matcher against the S116 corpus.

Reads the operator labeling queue, runs bot.cross_platform_matcher on every row,
and reports both real validation metrics (when operator labels exist) and
heuristic disagreement analysis (always available).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.cross_platform_matcher import MatchResult, match_markets


DEFAULT_QUEUE_PATH = Path("bot/state/cross_platform_labeling_queue.jsonl")
DEFAULT_DISAGREEMENT_PATH = Path("bot/state/matcher_heuristic_disagreement_pairs.jsonl")


def load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with Path(path).open() as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _queue_row_to_markets(row: dict) -> tuple[dict, dict]:
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


def _label_from_match_result(result: MatchResult) -> str:
    if result == MatchResult.MATCH_HIGH_CONFIDENCE:
        return "MATCH"
    if result == MatchResult.NO_MATCH:
        return "NO_MATCH"
    return "NEEDS_REVIEW"


def _operator_label(row: dict) -> str | None:
    label = row.get("operator_label")
    if label is None:
        return None
    label = str(label).strip().upper()
    return label or None


def _labeler(row: dict) -> str | None:
    labeler = row.get("labeler")
    if labeler is None:
        return None
    labeler = str(labeler).strip().lower()
    return labeler or None


def _labeler_matches(row: dict, labeler_filter: str | None) -> bool:
    if not labeler_filter:
        return True
    return _labeler(row) == labeler_filter.strip().lower()


def _write_jsonl(records: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    body = "".join(json.dumps(r, sort_keys=True) + "\n" for r in records)
    tmp.write_text(body)
    os.replace(tmp, path)


def validate_queue(
    queue_path: Path = DEFAULT_QUEUE_PATH,
    disagreement_path: Path = DEFAULT_DISAGREEMENT_PATH,
    disagreement_limit: int = 40,
    labeler: str | None = None,
) -> dict:
    rows = load_jsonl(queue_path)
    distribution: Counter[str] = Counter()
    heuristic_agree = 0
    heuristic_disagree = 0
    labeled_count = 0
    labeled_exact = 0
    false_positive_count = 0
    false_negative_count = 0
    confusion: Counter[str] = Counter()
    disagreements: list[dict] = []

    for row in rows:
        kalshi, polymarket = _queue_row_to_markets(row)
        decision = match_markets(kalshi, polymarket)
        result_value = decision.result.value
        distribution[result_value] += 1
        matcher_label = _label_from_match_result(decision.result)

        suggested_label = str(row.get("suggested_label") or "").strip().upper()
        if suggested_label:
            if matcher_label == suggested_label:
                heuristic_agree += 1
            else:
                heuristic_disagree += 1
                disagreements.append({
                    "kalshi_ticker": row.get("kalshi_ticker"),
                    "kalshi_question": row.get("kalshi_question"),
                    "kalshi_result": row.get("kalshi_result"),
                    "polymarket_ticker": row.get("polymarket_ticker"),
                    "polymarket_question": row.get("polymarket_question"),
                    "polymarket_result": row.get("polymarket_result"),
                    "jaccard": row.get("jaccard"),
                    "days_apart": row.get("days_apart"),
                    "suggested_label": suggested_label,
                    "matcher_label": matcher_label,
                    "matcher_result": result_value,
                    "matcher_reason": decision.reason,
                })

        label = _operator_label(row) if _labeler_matches(row, labeler) else None
        if label:
            labeled_count += 1
            confusion[f"{label}->{result_value}"] += 1
            if matcher_label == label:
                labeled_exact += 1
            if decision.result == MatchResult.MATCH_HIGH_CONFIDENCE and label == "NO_MATCH":
                false_positive_count += 1
            if decision.result == MatchResult.NO_MATCH and label == "MATCH":
                false_negative_count += 1

    disagreements.sort(key=lambda r: (float(r.get("jaccard") or 0), -float(r.get("days_apart") or 0)), reverse=True)
    top_disagreements = disagreements[:disagreement_limit]
    _write_jsonl(top_disagreements, disagreement_path)

    heuristic_total = heuristic_agree + heuristic_disagree
    summary = {
        "queue_path": str(queue_path),
        "rows": len(rows),
        "match_result_distribution": dict(distribution),
        "heuristic_agreement": {
            "total": heuristic_total,
            "agree": heuristic_agree,
            "disagree": heuristic_disagree,
            "agree_rate": heuristic_agree / heuristic_total if heuristic_total else None,
            "disagree_rate": heuristic_disagree / heuristic_total if heuristic_total else None,
        },
        "operator_validation": {
            "labeler_filter": labeler,
            "labeled_count": labeled_count,
            "accuracy": labeled_exact / labeled_count if labeled_count else None,
            "false_positive_count": false_positive_count,
            "false_negative_count": false_negative_count,
            "confusion": dict(confusion),
            "status": (
                "validation_available"
                if labeled_count
                else (
                    f"0 labels available for labeler={labeler}, awaiting review."
                    if labeler
                    else "0 labels available, awaiting operator review."
                )
            ),
        },
        "disagreement_report_path": str(disagreement_path),
        "disagreement_report_rows": len(top_disagreements),
    }
    return summary


def print_summary(summary: dict) -> None:
    print("=" * 72)
    print("Cross-platform matcher validation")
    print("=" * 72)
    print(f"Rows evaluated: {summary['rows']}")
    print(f"MatchResult distribution: {summary['match_result_distribution']}")
    heuristic = summary["heuristic_agreement"]
    if heuristic["total"]:
        print(
            "Heuristic agreement: "
            f"{heuristic['agree']}/{heuristic['total']} "
            f"({heuristic['agree_rate']:.1%} agree, {heuristic['disagree_rate']:.1%} disagree)"
        )
    validation = summary["operator_validation"]
    if validation.get("labeler_filter"):
        print(f"Labeler filter: {validation['labeler_filter']}")
    if validation["labeled_count"] == 0:
        print(validation["status"])
    else:
        print(
            "Operator-label validation: "
            f"accuracy={validation['accuracy']:.1%}, "
            f"false_positives={validation['false_positive_count']}, "
            f"false_negatives={validation['false_negative_count']}"
        )
        print(f"Confusion: {validation['confusion']}")
    print(
        "Priority disagreement pairs written: "
        f"{summary['disagreement_report_rows']} -> {summary['disagreement_report_path']}"
    )
    print("=" * 72)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--queue-path", default=str(DEFAULT_QUEUE_PATH))
    parser.add_argument("--disagreement-path", default=str(DEFAULT_DISAGREEMENT_PATH))
    parser.add_argument("--disagreement-limit", type=int, default=40)
    parser.add_argument(
        "--labeler",
        default=None,
        help="Restrict validation metrics to rows labeled by this labeler, e.g. codex or operator.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON summary")
    args = parser.parse_args(argv)

    summary = validate_queue(
        queue_path=Path(args.queue_path),
        disagreement_path=Path(args.disagreement_path),
        disagreement_limit=args.disagreement_limit,
        labeler=args.labeler,
    )
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print_summary(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
