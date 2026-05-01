"""main.py orchestrator — heuristic isolation, schema-aware skip, report writing."""

from __future__ import annotations

import json
from pathlib import Path

from tools.discovery_agent import main as agent_main
from tools.discovery_agent.findings import Finding


class _Working:
    name = "working"
    data_sources = ("paper_trades",)

    def run(self, ctx):
        return [Finding(
            heuristic="working", severity="info", title="ok", summary="-",
            evidence={"k": "v", "_fingerprint_keys": ["k"]},
        )]


class _Broken:
    name = "broken"
    data_sources = ("paper_trades",)

    def run(self, ctx):
        raise RuntimeError("boom")


class _NeedsMissing:
    name = "needs_missing"
    data_sources = ("order_microstructure",)

    def run(self, ctx):  # pragma: no cover — should be skipped
        raise AssertionError("should not run when source missing")


def _seed(repo: Path):
    state = repo / "bot" / "state"
    state.mkdir(parents=True, exist_ok=True)
    (repo / "bot" / "logs").mkdir(parents=True, exist_ok=True)
    (state / "paper_trades.json").write_text(json.dumps([{"id": "P1", "type": "vig_stack"}]))


def test_one_broken_heuristic_does_not_abort_others(tmp_path: Path, monkeypatch):
    _seed(tmp_path)
    monkeypatch.setattr(agent_main, "DEFAULT_HEURISTICS", [_Working(), _Broken()])
    out = agent_main.run(repo=tmp_path)
    assert any(f.heuristic == "working" for f in out["all_findings"])
    assert any(name == "broken" for name, _ in out["errors"])
    report = next((tmp_path / "bot" / "state" / "discovery").glob("discovery_report_*.md"))
    text = report.read_text()
    assert "broken" in text and "RuntimeError" in text


def test_missing_data_source_skips_heuristic(tmp_path: Path, monkeypatch):
    _seed(tmp_path)
    monkeypatch.setattr(agent_main, "DEFAULT_HEURISTICS", [_NeedsMissing()])
    out = agent_main.run(repo=tmp_path)
    assert out["all_findings"] == []
    assert any("skipped" in msg for _, msg in out["errors"])


def test_writes_jsonl_and_markdown(tmp_path: Path, monkeypatch):
    _seed(tmp_path)
    monkeypatch.setattr(agent_main, "DEFAULT_HEURISTICS", [_Working()])
    agent_main.run(repo=tmp_path)
    discovery_dir = tmp_path / "bot" / "state" / "discovery"
    jsonl_files = list(discovery_dir.glob("discovery_findings_*.jsonl"))
    md_files = list(discovery_dir.glob("discovery_report_*.md"))
    assert len(jsonl_files) == 1 and len(md_files) == 1
    raw = [json.loads(line) for line in jsonl_files[0].read_text().splitlines() if line.strip()]
    assert raw[0]["heuristic"] == "working"


def test_dedup_classifies_new_stable_resolved(tmp_path: Path, monkeypatch):
    """Run once, run again with a new finding added — only the new one is NEW."""
    _seed(tmp_path)

    class V1:
        name = "v1"
        data_sources = ("paper_trades",)
        def run(self, ctx):
            return [Finding(
                heuristic="v1", severity="info", title="t", summary="-",
                evidence={"k": "A", "_fingerprint_keys": ["k"]},
            )]

    class V2:
        name = "v1"
        data_sources = ("paper_trades",)
        def run(self, ctx):
            return [
                Finding(heuristic="v1", severity="info", title="t", summary="-",
                        evidence={"k": "A", "_fingerprint_keys": ["k"]}),
                Finding(heuristic="v1", severity="info", title="t", summary="-",
                        evidence={"k": "B", "_fingerprint_keys": ["k"]}),
            ]

    monkeypatch.setattr(agent_main, "DEFAULT_HEURISTICS", [V1()])
    agent_main.run(repo=tmp_path)
    monkeypatch.setattr(agent_main, "DEFAULT_HEURISTICS", [V2()])
    out = agent_main.run(repo=tmp_path)
    new_keys = {f.evidence["k"] for f in out["new"]}
    stable_keys = {f.evidence["k"] for f in out["stable"]}
    assert new_keys == {"B"}
    assert stable_keys == {"A"}
