from __future__ import annotations

import json
from pathlib import Path

from tools.validate_cross_platform_matcher import validate_queue


REPO_ROOT = Path(__file__).resolve().parents[1]
PROD_QUEUE_PATH = REPO_ROOT / "bot" / "state" / "cross_platform_labeling_queue.jsonl"


def _write_rows(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _row(
    suggested_label: str = "MATCH",
    operator_label=None,
    kalshi_result: str = "yes",
    polymarket_result: str = "yes",
    labeler=None,
    labeling_session=None,
    sample_stratum=None,
) -> dict:
    row = {
        "kalshi_ticker": "KXBTC",
        "kalshi_question": "Will Bitcoin be above 100000 on May 12?",
        "kalshi_close_date": "2026-05-12T12:00:00Z",
        "kalshi_result": kalshi_result,
        "kalshi_category": "Crypto",
        "polymarket_ticker": "216",
        "polymarket_question": "Bitcoin above 100000 May 12?",
        "polymarket_close_date": "2026-05-12T13:00:00Z",
        "polymarket_result": polymarket_result,
        "polymarket_category": "",
        "jaccard": 0.75,
        "days_apart": 0.042,
        "suggested_label": suggested_label,
        "operator_label": operator_label,
    }
    if labeler is not None:
        row["labeler"] = labeler
    if labeling_session is not None:
        row["labeling_session"] = labeling_session
    if sample_stratum is not None:
        row["sample_stratum"] = sample_stratum
    return row


def test_validate_queue_reports_zero_label_pending_status(tmp_path):
    queue = tmp_path / "queue.jsonl"
    disagreements = tmp_path / "disagreements.jsonl"
    _write_rows(queue, [_row(operator_label=None)])

    summary = validate_queue(queue, disagreements)

    assert summary["operator_validation"]["labeled_count"] == 0
    assert summary["operator_validation"]["accuracy"] is None
    assert summary["operator_validation"]["status"] == "0 labels available, awaiting operator review."
    assert disagreements.exists()


def test_validate_queue_counts_false_positive_against_operator_no_match(tmp_path):
    queue = tmp_path / "queue.jsonl"
    disagreements = tmp_path / "disagreements.jsonl"
    _write_rows(queue, [_row(operator_label="NO_MATCH")])

    summary = validate_queue(queue, disagreements)

    assert summary["operator_validation"]["labeled_count"] == 1
    assert summary["operator_validation"]["false_positive_count"] == 1
    assert summary["operator_validation"]["false_negative_count"] == 0
    assert summary["operator_validation"]["accuracy"] == 0.0


def test_validate_queue_writes_heuristic_disagreement_rows(tmp_path):
    queue = tmp_path / "queue.jsonl"
    disagreements = tmp_path / "disagreements.jsonl"
    _write_rows(queue, [_row(suggested_label="MATCH", kalshi_result="yes", polymarket_result="no")])

    summary = validate_queue(queue, disagreements)

    assert summary["heuristic_agreement"]["disagree"] == 1
    rows = [json.loads(line) for line in disagreements.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["suggested_label"] == "MATCH"
    assert rows[0]["matcher_label"] == "NO_MATCH"


def test_validate_queue_can_filter_codex_labels_from_other_labels(tmp_path):
    queue = tmp_path / "queue.jsonl"
    disagreements = tmp_path / "disagreements.jsonl"
    _write_rows(
        queue,
        [
            _row(operator_label="NO_MATCH", labeler="codex"),
            _row(operator_label="NO_MATCH", labeler="operator"),
            _row(operator_label="NO_MATCH"),
        ],
    )

    summary = validate_queue(queue, disagreements, labeler="codex")

    assert summary["operator_validation"]["labeler_filter"] == "codex"
    assert summary["operator_validation"]["labeled_count"] == 1
    assert summary["operator_validation"]["false_positive_count"] == 1


def test_validate_queue_can_filter_by_labeling_session_and_report_strata(tmp_path):
    queue = tmp_path / "queue.jsonl"
    disagreements = tmp_path / "disagreements.jsonl"
    _write_rows(
        queue,
        [
            _row(
                operator_label="NO_MATCH",
                labeler="codex",
                labeling_session="S121",
                sample_stratum="high_confidence_boundary",
            ),
            _row(operator_label="NO_MATCH", labeler="codex", labeling_session="S119"),
        ],
    )

    summary = validate_queue(queue, disagreements, labeler="codex", labeling_session="S121")

    validation = summary["operator_validation"]
    assert validation["labeler_filter"] == "codex"
    assert validation["labeling_session_filter"] == "S121"
    assert validation["labeled_count"] == 1
    assert validation["false_positive_count"] == 1
    assert validation["per_bucket"]["MATCH_HIGH_CONFIDENCE"]["false_positive_count"] == 1
    assert validation["per_stratum"]["high_confidence_boundary"]["false_positive_count"] == 1


def test_matcher_classifications_locked_against_codex_labels(tmp_path):
    """Regression guard against the S118-S121 labeled validation corpus.

    Locks the matcher's classification behavior on every Codex-labeled row in
    bot/state/cross_platform_labeling_queue.jsonl. S122's sports
    game-instance gate fixes the S121 back-to-back false positive.
    """
    disagreements = tmp_path / "disagreements.jsonl"
    summary = validate_queue(PROD_QUEUE_PATH, disagreements, labeler="codex")

    validation = summary["operator_validation"]
    assert validation["labeled_count"] == 120, (
        "expected 120 codex labels (80 S118-S120 + 40 S121 holdout), "
        f"got {validation['labeled_count']}"
    )
    assert validation["false_positive_count"] == 0, (
        "S122 should eliminate high-confidence false positives on the 120-label corpus; "
        f"got {validation['false_positive_count']}"
    )
    assert validation["false_negative_count"] == 0, (
        "matcher false-negative count drifted from S121 evidence (0); "
        f"got {validation['false_negative_count']}"
    )
    assert validation["accuracy"] >= 0.97
    confusion = validation["confusion"]
    assert confusion.get("NO_MATCH->no_match") == 71
    assert confusion.get("NO_MATCH->match_needs_review") == 5
    assert confusion.get("NO_MATCH->match_high_confidence", 0) == 0
    assert confusion.get("MATCH->match_high_confidence") == 27
    assert confusion.get("MATCH->match_needs_review") == 2
    assert confusion.get("NEEDS_REVIEW->match_high_confidence", 0) == 0
    assert confusion.get("NEEDS_REVIEW->match_needs_review") == 14
    assert confusion.get("NEEDS_REVIEW->no_match") == 1


def test_matcher_holdout_validation_documents_back_to_back_fix(tmp_path):
    """S122 fixes the S121 boundary false positive without false negatives."""
    disagreements = tmp_path / "disagreements.jsonl"
    summary = validate_queue(PROD_QUEUE_PATH, disagreements, labeler="codex", labeling_session="S121")

    validation = summary["operator_validation"]
    assert validation["labeled_count"] == 40
    assert validation["false_positive_count"] == 0
    assert validation["false_negative_count"] == 0
    assert validation["accuracy"] == 0.95
    assert validation["exact_accuracy"] == 0.95
    assert validation["per_bucket"]["MATCH_HIGH_CONFIDENCE"]["labeled_count"] == 13
    assert validation["per_bucket"]["MATCH_HIGH_CONFIDENCE"]["false_positive_count"] == 0
    assert validation["per_bucket"]["MATCH_NEEDS_REVIEW"]["false_positive_count"] == 0
    assert validation["per_bucket"]["NO_MATCH"]["false_positive_count"] == 0
    assert validation["per_stratum"]["high_confidence_boundary"]["labeled_count"] == 8
    assert validation["per_stratum"]["high_confidence_boundary"]["false_positive_count"] == 0
    assert validation["per_stratum"]["high_confidence_interior"]["false_positive_count"] == 0
