from __future__ import annotations

import json
from pathlib import Path

from tools.validate_cross_platform_matcher import validate_queue


def _write_rows(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _row(
    suggested_label: str = "MATCH",
    operator_label=None,
    kalshi_result: str = "yes",
    polymarket_result: str = "yes",
    labeler=None,
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
