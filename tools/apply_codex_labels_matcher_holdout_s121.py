"""Session 121: apply Codex holdout labels to the cross-platform queue."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


DEFAULT_QUEUE_PATH = ROOT / "bot" / "state" / "cross_platform_labeling_queue.jsonl"
DEFAULT_SAMPLING_PATH = ROOT / "bot" / "state" / "codex_sampling_matcher_holdout_s121.jsonl"
DEFAULT_LABELS_PATH = ROOT / "bot" / "state" / "codex_labels_matcher_holdout_s121_raw.json"
EXPECTED_LABELS = 40
VALID_LABELS = {"MATCH", "NO_MATCH", "NEEDS_REVIEW"}
HARD_RULE_NOTE = " [hard rule: outcome divergence overrides text-similarity judgment.]"


def load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open() as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def load_json(path: Path):
    with path.open() as f:
        return json.load(f)


def write_jsonl(records: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        for record in records:
            f.write(json.dumps(record, sort_keys=True) + "\n")
    tmp.replace(path)


def compute_outcome_cross_check(kalshi_result, polymarket_result) -> str:
    if not kalshi_result or not polymarket_result:
        return "INSUFFICIENT_DATA"
    kalshi = str(kalshi_result).strip().lower()
    polymarket = str(polymarket_result).strip().lower()
    if kalshi == "yes" and polymarket == "yes":
        return "BOTH_YES"
    if kalshi == "no" and polymarket == "no":
        return "BOTH_NO"
    if {kalshi, polymarket} == {"yes", "no"}:
        return "DIVERGED"
    return "INSUFFICIENT_DATA"


def compute_outcome_corroborates_label(label: str, cross_check: str) -> bool:
    if label == "MATCH" and cross_check in {"BOTH_YES", "BOTH_NO"}:
        return True
    if label == "NO_MATCH" and cross_check == "DIVERGED":
        return True
    return False


def apply_hard_rule(label: str, cross_check: str, reasoning: str) -> tuple[str, str, bool]:
    if cross_check == "DIVERGED" and label == "MATCH":
        return "NO_MATCH", reasoning + HARD_RULE_NOTE, True
    return label, reasoning, False


def _key(row: dict) -> tuple[str, str]:
    return (row.get("kalshi_ticker"), row.get("polymarket_ticker"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--queue-path", default=str(DEFAULT_QUEUE_PATH))
    parser.add_argument("--sampling-path", default=str(DEFAULT_SAMPLING_PATH))
    parser.add_argument("--labels-path", default=str(DEFAULT_LABELS_PATH))
    args = parser.parse_args(argv)

    queue_path = Path(args.queue_path)
    sampling_path = Path(args.sampling_path)
    labels_path = Path(args.labels_path)

    sampling_rows = load_jsonl(sampling_path)
    if len(sampling_rows) != EXPECTED_LABELS:
        raise SystemExit(f"expected {EXPECTED_LABELS} sampled rows, got {len(sampling_rows)}")
    sampling_index = {_key(row): row for row in sampling_rows}
    if len(sampling_index) != EXPECTED_LABELS:
        raise SystemExit("sample file contains duplicate (kalshi_ticker, polymarket_ticker) keys")

    labels = load_json(labels_path)
    if not isinstance(labels, list):
        raise SystemExit(f"labels file must be a JSON list, got {type(labels)}")
    if len(labels) != EXPECTED_LABELS:
        raise SystemExit(f"expected {EXPECTED_LABELS} raw labels, got {len(labels)}")

    patches: dict[tuple[str, str], dict] = {}
    hard_rule_fires = 0
    label_counts: dict[str, int] = {}
    cross_check_counts: dict[str, int] = {}
    stratum_label_counts: dict[tuple[str, str], int] = {}

    for entry in labels:
        key = (entry.get("kalshi_ticker"), entry.get("polymarket_ticker"))
        sample = sampling_index.get(key)
        if sample is None:
            raise SystemExit(f"label refers to pair outside S121 sample: {key}")
        raw_label = str(entry.get("label") or "").strip().upper()
        if raw_label not in VALID_LABELS:
            raise SystemExit(f"invalid label {raw_label!r} for pair {key}")
        reasoning = str(entry.get("reasoning") or "").strip()
        if len(reasoning.split()) < 12:
            raise SystemExit(f"reasoning is too short for pair {key}")
        if sample.get("matcher_classification") == "MATCH_HIGH_CONFIDENCE":
            lowered = reasoning.lower()
            for required in ("bet-type", "time granularity"):
                if required not in lowered:
                    raise SystemExit(f"HIGH_CONFIDENCE reasoning for {key} must mention {required}")

        cross_check = compute_outcome_cross_check(sample.get("kalshi_result"), sample.get("polymarket_result"))
        final_label, final_reasoning, hard_rule_fired = apply_hard_rule(raw_label, cross_check, reasoning)
        hard_rule_fires += int(hard_rule_fired)
        corroborates = compute_outcome_corroborates_label(final_label, cross_check)
        stratum = sample.get("sample_stratum")

        patches[key] = {
            "operator_label": final_label,
            "labeler": "codex",
            "reasoning": final_reasoning,
            "outcome_cross_check": cross_check,
            "outcome_corroborates_label": corroborates,
            "labeling_session": "S121",
            "sample_stratum": stratum,
            "matcher_classification": sample.get("matcher_classification"),
            "matcher_jaccard": sample.get("matcher_jaccard"),
            "matcher_reason": sample.get("matcher_reason"),
        }
        label_counts[final_label] = label_counts.get(final_label, 0) + 1
        cross_check_counts[cross_check] = cross_check_counts.get(cross_check, 0) + 1
        stratum_label_counts[(stratum, final_label)] = stratum_label_counts.get((stratum, final_label), 0) + 1

    if len(patches) != EXPECTED_LABELS:
        raise SystemExit(f"expected {EXPECTED_LABELS} unique label keys, got {len(patches)}")

    queue_rows = load_jsonl(queue_path)
    updated = 0
    for row in queue_rows:
        patch = patches.get(_key(row))
        if patch is None:
            continue
        if row.get("operator_label") is not None or row.get("labeler") is not None:
            raise SystemExit(f"refusing to overwrite existing label on pair {_key(row)}")
        row.update(patch)
        updated += 1

    if updated != EXPECTED_LABELS:
        raise SystemExit(f"applied {updated} labels; expected {EXPECTED_LABELS}")

    write_jsonl(queue_rows, queue_path)
    print(f"Applied {updated} S121 labels to {queue_path}")
    print(f"Hard rule (DIVERGED + MATCH -> NO_MATCH) fired {hard_rule_fires} time(s)")
    print(f"Label counts: {label_counts}")
    print(f"Outcome cross-check counts: {cross_check_counts}")
    print("Per-stratum label counts:")
    for (stratum, label), count in sorted(stratum_label_counts.items()):
        print(f"  {stratum} / {label}: {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
