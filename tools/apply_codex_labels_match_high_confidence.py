"""Session 119: post-process Codex's MATCH_HIGH_CONFIDENCE labels.

Takes Codex's raw label JSON, the original sampling JSONL, computes mechanical
outcome_cross_check + outcome_corroborates_label, applies the hard rule
(DIVERGED outcomes + Codex-MATCH → force NO_MATCH), and writes the labels back
into bot/state/cross_platform_labeling_queue.jsonl in place.

Per S118 pattern, sets:
    operator_label, labeler="codex", reasoning, outcome_cross_check,
    outcome_corroborates_label
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


DEFAULT_QUEUE_PATH = ROOT / "bot" / "state" / "cross_platform_labeling_queue.jsonl"
DEFAULT_SAMPLING_PATH = ROOT / "bot" / "state" / "codex_sampling_match_high_confidence.jsonl"
DEFAULT_LABELS_PATH = ROOT / "bot" / "state" / "codex_labels_match_high_confidence_raw.json"


HARD_RULE_NOTE = " [hard rule: outcome divergence overrides text-similarity judgment.]"


def compute_outcome_cross_check(kalshi_result, polymarket_result) -> str:
    if not kalshi_result or not polymarket_result:
        return "INSUFFICIENT_DATA"
    k = str(kalshi_result).strip().lower()
    p = str(polymarket_result).strip().lower()
    if k == "yes" and p == "yes":
        return "BOTH_YES"
    if k == "no" and p == "no":
        return "BOTH_NO"
    if {k, p} == {"yes", "no"}:
        return "DIVERGED"
    return "INSUFFICIENT_DATA"


def compute_outcome_corroborates_label(label: str, cross_check: str) -> bool:
    if label == "MATCH" and cross_check in ("BOTH_YES", "BOTH_NO"):
        return True
    if label == "NO_MATCH" and cross_check == "DIVERGED":
        return True
    return False


def apply_hard_rule(codex_label: str, cross_check: str, codex_reasoning: str) -> tuple[str, str, bool]:
    """Returns (final_label, final_reasoning, hard_rule_fired)."""
    if cross_check == "DIVERGED" and codex_label == "MATCH":
        return ("NO_MATCH", codex_reasoning + HARD_RULE_NOTE, True)
    return (codex_label, codex_reasoning, False)


def load_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    with path.open() as f:
        for line in f:
            if line.strip():
                out.append(json.loads(line))
    return out


def load_json(path: Path):
    with path.open() as f:
        return json.load(f)


def write_jsonl(records: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        for r in records:
            f.write(json.dumps(r, sort_keys=True) + "\n")
    tmp.replace(path)


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
    sampling_index = {
        (r["kalshi_ticker"], r["polymarket_ticker"]): r for r in sampling_rows
    }
    if len(sampling_index) != len(sampling_rows):
        print(
            f"WARNING: sampling has duplicate (kalshi, polymarket) keys: "
            f"{len(sampling_rows)} rows -> {len(sampling_index)} keys",
            file=sys.stderr,
        )

    codex_labels = load_json(labels_path)
    if not isinstance(codex_labels, list):
        raise SystemExit(f"labels file must be a JSON list, got {type(codex_labels)}")

    # Build the final-label index keyed by (kalshi_ticker, polymarket_ticker).
    final_labels: dict[tuple[str, str], dict] = {}
    hard_rule_fires = 0
    cross_check_counts: dict[str, int] = {}
    label_counts: dict[str, int] = {}
    stratum_label_counts: dict[tuple[str, str], int] = {}

    for entry in codex_labels:
        k = entry["kalshi_ticker"]
        p = entry["polymarket_ticker"]
        codex_label = entry["label"]
        codex_reasoning = entry["reasoning"]
        key = (k, p)

        sample = sampling_index.get(key)
        if sample is None:
            print(f"WARNING: codex label refers to unknown pair {key}", file=sys.stderr)
            continue

        cross_check = compute_outcome_cross_check(sample.get("kalshi_result"), sample.get("polymarket_result"))
        final_label, final_reasoning, hard_rule_fired = apply_hard_rule(codex_label, cross_check, codex_reasoning)
        if hard_rule_fired:
            hard_rule_fires += 1
        corroborates = compute_outcome_corroborates_label(final_label, cross_check)
        cross_check_counts[cross_check] = cross_check_counts.get(cross_check, 0) + 1
        label_counts[final_label] = label_counts.get(final_label, 0) + 1
        stratum = sample.get("stratum", "?")
        stratum_label_counts[(stratum, final_label)] = stratum_label_counts.get((stratum, final_label), 0) + 1

        final_labels[key] = {
            "operator_label": final_label,
            "labeler": "codex",
            "reasoning": final_reasoning,
            "outcome_cross_check": cross_check,
            "outcome_corroborates_label": corroborates,
        }

    if len(final_labels) != len(codex_labels):
        raise SystemExit(
            f"mismatched label count: codex provided {len(codex_labels)} but "
            f"only {len(final_labels)} matched the sampling pool."
        )

    queue_rows = load_jsonl(queue_path)
    updated = 0
    for row in queue_rows:
        key = (row.get("kalshi_ticker"), row.get("polymarket_ticker"))
        patch = final_labels.get(key)
        if patch is None:
            continue
        if row.get("labeler") and row.get("labeler") != "codex":
            print(
                f"WARNING: pair {key} already has non-codex labeler="
                f"{row['labeler']!r}; skipping to preserve existing label",
                file=sys.stderr,
            )
            continue
        if row.get("operator_label") and row.get("labeler") == "codex":
            # Already labeled by codex; only happens if rerun. Overwrite is fine.
            pass
        row.update(patch)
        updated += 1

    if updated != len(final_labels):
        print(
            f"WARNING: applied {updated} updates to the queue but had "
            f"{len(final_labels)} labels; some pairs may be missing from the queue.",
            file=sys.stderr,
        )

    write_jsonl(queue_rows, queue_path)

    print(f"Applied {updated} labels to {queue_path}")
    print(f"Hard rule (DIVERGED + MATCH -> NO_MATCH) fired {hard_rule_fires} time(s)")
    print(f"Label counts: {label_counts}")
    print(f"Outcome cross-check counts: {cross_check_counts}")
    print("Per-stratum label counts:")
    for (stratum, label), n in sorted(stratum_label_counts.items()):
        print(f"  {stratum} / {label}: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
