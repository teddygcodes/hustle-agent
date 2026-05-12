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
    """Regression guard against the S118-S123 labeled validation corpus.

    Locks the matcher's classification behavior on every Codex-labeled row in
    bot/state/cross_platform_labeling_queue.jsonl: 80 S118-S120 design rows +
    40 S121 holdout-1 rows + 40 S123 holdout-2 rows = 160 total. S122's sports
    game-instance gate fixes the S121 back-to-back false positive; S123
    holdout-2 confirms v3 generalizes (0 FPs on the unseen 40-pair sample).
    """
    disagreements = tmp_path / "disagreements.jsonl"
    summary = validate_queue(PROD_QUEUE_PATH, disagreements, labeler="codex")

    validation = summary["operator_validation"]
    assert validation["labeled_count"] == 160, (
        "expected 160 codex labels (80 S118-S120 + 40 S121 holdout-1 + 40 S123 holdout-2), "
        f"got {validation['labeled_count']}"
    )
    assert validation["false_positive_count"] == 0, (
        "matcher v3 must eliminate high-confidence false positives on the 160-label corpus; "
        f"got {validation['false_positive_count']}"
    )
    assert validation["false_negative_count"] == 0, (
        "matcher false-negative count drifted from S122/S123 evidence (0); "
        f"got {validation['false_negative_count']}"
    )
    assert validation["accuracy"] >= 0.92
    confusion = validation["confusion"]
    assert confusion.get("NO_MATCH->no_match") == 77
    assert confusion.get("NO_MATCH->match_needs_review") == 15
    assert confusion.get("NO_MATCH->match_high_confidence", 0) == 0
    assert confusion.get("MATCH->match_high_confidence") == 42
    assert confusion.get("MATCH->match_needs_review") == 7
    assert confusion.get("NEEDS_REVIEW->match_high_confidence", 0) == 0
    assert confusion.get("NEEDS_REVIEW->match_needs_review") == 14
    assert confusion.get("NEEDS_REVIEW->no_match") == 5


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


def test_matcher_holdout_2_validation_zero_fps_clean_generalization(tmp_path):
    """S123 holdout-2 confirms matcher v3 generalizes beyond the design corpus.

    Filters to the 40 S123 holdout-2 labels (stratified at seed=123 from the
    4,880 unlabeled rows at S123 start, distinct from the 120 prior codex
    labels). Locks 0 false positives — the load-bearing signal that v3's
    bet-type / time-granularity / game-instance gates aren't overfit to the
    S118-S121 cases they were designed against.
    """
    disagreements = tmp_path / "disagreements.jsonl"
    summary = validate_queue(PROD_QUEUE_PATH, disagreements, labeler="codex", labeling_session="S123")

    validation = summary["operator_validation"]
    assert validation["labeled_count"] == 40
    assert validation["false_positive_count"] == 0, (
        "matcher v3 must produce zero false positives on the S123 holdout-2 sample; "
        f"got {validation['false_positive_count']}"
    )
    assert validation["false_negative_count"] == 0
    assert validation["accuracy"] == 0.775
    assert validation["exact_accuracy"] == 0.525
    # HIGH_CONFIDENCE bucket is the load-bearing live-arb signal:
    # every operator MATCH agrees with v3 MATCH_HIGH_CONFIDENCE on the 15 holdout-2 picks.
    assert validation["per_bucket"]["MATCH_HIGH_CONFIDENCE"]["labeled_count"] == 15
    assert validation["per_bucket"]["MATCH_HIGH_CONFIDENCE"]["false_positive_count"] == 0
    assert validation["per_bucket"]["MATCH_HIGH_CONFIDENCE"]["exact_accuracy"] == 1.0
    assert validation["per_bucket"]["MATCH_NEEDS_REVIEW"]["labeled_count"] == 15
    assert validation["per_bucket"]["MATCH_NEEDS_REVIEW"]["false_positive_count"] == 0
    assert validation["per_bucket"]["NO_MATCH"]["labeled_count"] == 10
    assert validation["per_bucket"]["NO_MATCH"]["false_positive_count"] == 0


def test_matcher_holdout_2_boundary_high_confidence_clean(tmp_path):
    """S123 boundary stratum — the 8 lowest-Jaccard HIGH_CONFIDENCE picks.

    Boundary picks are the most informative subset for surfacing residual FPs:
    same-sport same-team-different-date back-to-back patterns (the S121 FP
    archetype that drove S122's game-instance gate) cluster in this stratum
    because the matcher's jaccard score is lowest while still clearing the
    HIGH_CONFIDENCE threshold. 0 FPs here is the cleanest "v3 generalizes"
    evidence the holdout can produce.
    """
    disagreements = tmp_path / "disagreements.jsonl"
    summary = validate_queue(PROD_QUEUE_PATH, disagreements, labeler="codex", labeling_session="S123")

    boundary = summary["operator_validation"]["per_stratum"]["high_confidence_boundary"]
    interior = summary["operator_validation"]["per_stratum"]["high_confidence_interior"]

    assert boundary["labeled_count"] == 8
    assert boundary["false_positive_count"] == 0
    assert boundary["false_negative_count"] == 0
    assert boundary["exact_accuracy"] == 1.0
    assert interior["labeled_count"] == 7
    assert interior["false_positive_count"] == 0
    assert interior["false_negative_count"] == 0
    assert interior["exact_accuracy"] == 1.0
