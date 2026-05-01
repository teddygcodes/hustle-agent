import json
from pathlib import Path

import pytest

from tools.discovery_agent.findings import (
    Finding,
    classify_findings,
    load_prior_findings,
    write_findings_jsonl,
)


def _f(heuristic="outlier_pnl", trade_id="T1", **extra):
    evidence = {"trade_id": trade_id, "_fingerprint_keys": ["trade_id"], **extra}
    return Finding(
        heuristic=heuristic,
        severity="notable",
        title=f"{heuristic} on {trade_id}",
        summary="x",
        evidence=evidence,
    )


def test_fingerprint_stable_across_instances():
    a = _f(trade_id="X")
    b = _f(trade_id="X")
    assert a.fingerprint() == b.fingerprint()


def test_fingerprint_changes_with_evidence():
    assert _f(trade_id="X").fingerprint() != _f(trade_id="Y").fingerprint()


def test_fingerprint_includes_heuristic_name():
    a = _f(heuristic="outlier_pnl", trade_id="X")
    b = _f(heuristic="cohort_emergence", trade_id="X")
    assert a.fingerprint() != b.fingerprint()


def test_fingerprint_requires_keys_declaration():
    bad = Finding(
        heuristic="x", severity="info", title="t", summary="s",
        evidence={"trade_id": "X"},
    )
    with pytest.raises(ValueError, match="_fingerprint_keys"):
        bad.fingerprint()


def test_classify_new_stable_resolved():
    prior = [_f(trade_id="A"), _f(trade_id="B"), _f(trade_id="C")]
    new = [_f(trade_id="B"), _f(trade_id="D"), _f(trade_id="E")]
    is_new, stable, resolved = classify_findings(new, prior)
    assert {f.evidence["trade_id"] for f in is_new} == {"D", "E"}
    assert {f.evidence["trade_id"] for f in stable} == {"B"}
    assert {f.evidence["trade_id"] for f in resolved} == {"A", "C"}


def test_load_prior_findings_empty_when_no_files(tmp_path: Path):
    assert load_prior_findings(tmp_path) == []


def test_load_prior_findings_picks_most_recent(tmp_path: Path):
    older = tmp_path / "discovery_findings_2026-04-28.jsonl"
    newer = tmp_path / "discovery_findings_2026-04-29.jsonl"
    write_findings_jsonl(older, [_f(trade_id="OLD")])
    write_findings_jsonl(newer, [_f(trade_id="NEW")])
    loaded = load_prior_findings(tmp_path)
    assert {f.evidence["trade_id"] for f in loaded} == {"NEW"}


def test_write_and_read_roundtrip(tmp_path: Path):
    findings = [_f(trade_id="A"), _f(trade_id="B")]
    out = tmp_path / "discovery_findings_2026-04-30.jsonl"
    write_findings_jsonl(out, findings)
    raw = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
    assert len(raw) == 2
    assert raw[0]["evidence"]["trade_id"] == "A"
