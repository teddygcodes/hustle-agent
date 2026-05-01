"""SFPHI regression — the founding example must surface in BOTH heuristics.

PAPER-4A16F5D2 (KXMLBGAME-26APR291840SFPHI-PHI, vig_stack, +$172.52, exited_early)
on 2026-04-30 -> outlier_pnl HIGH severity.

vig_stack_futures cohort (decisions.jsonl) emerged 2026-04-29, n>=3 in last 7d,
n=0 in prior 30d -> cohort_emergence NOTABLE/HIGH severity.

If this test breaks, the agent has lost its founding example. P0 regression.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from tools.discovery_agent import main as agent_main


def _seed_sfphi_fixture(repo: Path) -> None:
    state = repo / "bot" / "state"
    state.mkdir(parents=True, exist_ok=True)
    (repo / "bot" / "logs").mkdir(parents=True, exist_ok=True)
    now = dt.datetime(2026, 4, 30, 12, 0, tzinfo=dt.timezone.utc)

    sfphi = {
        "id": "PAPER-4A16F5D2",
        "ticker": "KXMLBGAME-26APR291840SFPHI-PHI",
        "type": "vig_stack",
        "side": "no",
        "entry_price": 0.44,
        "contracts": 454,
        "timestamp": "2026-04-30T12:09:55.855881+00:00",
        "edge_at_entry": 0.0137,
        "confidence": 0.8,
        "status": "exited_early",
        "exit_price": 0.82,
        "pnl": 172.52,
        "resolved_at": "2026-05-01T00:59:30.955474+00:00",
        "exit_reason": "auto_take_profit",
    }

    # Baseline cohort context: prior-30d small live_momentum + vig_stack trades
    # so cohort math has a population. NONE of these should fire outlier_pnl
    # individually (each |pnl| < $75).
    baseline = []
    for i in range(15):
        baseline.append({
            "id": f"BASE-{i}",
            "ticker": f"KXMLBGAME-26APR{(i % 28) + 1:02d}-X",
            "type": "live_momentum" if i % 2 else "vig_stack",
            "status": "won" if i % 3 else "lost",
            "pnl": 12.0 if i % 3 else -8.0,
            "timestamp": (now - dt.timedelta(days=10 + i)).isoformat(),
            "resolved_at": (now - dt.timedelta(days=10 + i)).isoformat(),
        })
    (state / "paper_trades.json").write_text(json.dumps([sfphi] + baseline))

    # decisions.jsonl: 5 vig_stack_futures decisions in last 7d (matches real-world
    # emergence of 2000+ records starting 2026-04-29) + only vig_stack_series in
    # prior 30d. Provides the cohort_emergence NEW signal.
    decisions = []
    for i in range(5):
        decisions.append({
            "ts": (now - dt.timedelta(hours=i * 4)).isoformat(),
            "ticker": "KXMLBGAME-26APR291840SFPHI-PHI" if i == 0 else f"KXMLBGAME-26APR29-D{i}",
            "opp_type": "vig_stack_futures",
            "edge": 0.014, "decision": "accept", "reason": "ok",
            "gates": {}, "extra": {}, "regime": {},
        })
    for i in range(20):
        decisions.append({
            "ts": (now - dt.timedelta(days=10 + i)).isoformat(),
            "ticker": f"KXHIGHNY-26APR{(i % 28) + 1:02d}-T63",
            "opp_type": "vig_stack_series",
            "edge": 0.01, "decision": "reject", "reason": "no_vig",
            "gates": {}, "extra": {}, "regime": {},
        })
    (state / "decisions.jsonl").write_text("\n".join(json.dumps(d) for d in decisions) + "\n")

    # Other 12 sources empty/minimal so their heuristics either skip or no-op.
    (state / "trade_history.json").write_text("[]")
    (state / "live_journal.json").write_text("[]")
    (state / "positions.json").write_text("[]")
    (state / "bot_state.json").write_text("{}")
    (state / "clv.json").write_text("[]")
    (state / "predictions.jsonl").write_text("")
    (state / "tracker_cadence.jsonl").write_text("")
    (state / "strategy_audit.json").write_text("{}")
    (state / "universe.jsonl").write_text("")
    (state / "live_ticks.jsonl").write_text("")


def test_sfphi_surfaces_in_outlier_pnl(tmp_path: Path):
    _seed_sfphi_fixture(tmp_path)
    out = agent_main.run(repo=tmp_path)
    sfphi = [
        f for f in out["all_findings"]
        if f.heuristic == "outlier_pnl" and f.evidence.get("trade_id") == "PAPER-4A16F5D2"
    ]
    assert len(sfphi) == 1, (
        f"SFPHI not flagged by outlier_pnl. Got: "
        f"{[(f.heuristic, f.evidence) for f in out['all_findings']]}"
    )
    assert sfphi[0].severity == "high"
    assert sfphi[0].evidence["sport"] == "mlb"
    assert sfphi[0].evidence["opp_type"] == "vig_stack"


def test_vig_stack_futures_surfaces_in_cohort_emergence(tmp_path: Path):
    """SFPHI ticker is KXMLBGAME-* (per-game) so Session 43b classifies it as
    'mlb_game' (not just 'mlb' as Session 43a did). Severity is notable/high
    because the fixture's 5 decisions include accept records → cohort produces
    accepts_recent >= 1, escaping the 'info' demotion."""
    _seed_sfphi_fixture(tmp_path)
    out = agent_main.run(repo=tmp_path)
    vsf = [
        f for f in out["all_findings"]
        if f.heuristic == "cohort_emergence"
        and f.evidence.get("opp_type") == "vig_stack_futures"
        and f.evidence.get("sport") == "mlb_game"
    ]
    assert len(vsf) == 1
    assert vsf[0].severity in ("notable", "high")
    assert vsf[0].evidence["source"] == "decisions"
    # Session 43b refinement: new evidence keys present, semantics correct
    assert vsf[0].evidence["accepts_recent"] >= 1, (
        "SFPHI fixture seeds 5 vig_stack_futures decisions all with decision='accept'; "
        "accepts_recent should reflect that"
    )
    assert vsf[0].evidence["unique_tickers_recent"] >= 1
    # paper_trades_recent for the DECISIONS-side cohort is 0 BY DESIGN: SFPHI's
    # paper_trade has type='vig_stack' but the decisions cohort is opp_type=
    # 'vig_stack_futures'. The vocabulary mismatch is the bug-pair signal — the
    # join via paper_trade.type == cohort.opp_type intentionally does NOT bridge.
    # See test_paper_trades_use_type_field_not_opp_type for the vocab-policy lock.
    assert vsf[0].evidence["paper_trades_recent"] == 0


def test_both_findings_appear_in_markdown_report(tmp_path: Path):
    _seed_sfphi_fixture(tmp_path)
    agent_main.run(repo=tmp_path)
    discovery_dir = tmp_path / "bot" / "state" / "discovery"
    md = next(discovery_dir.glob("discovery_report_*.md")).read_text()
    assert "PAPER-4A16F5D2" in md
    assert "vig_stack_futures" in md
    assert "## NEW findings" in md


def test_findings_jsonl_is_parseable(tmp_path: Path):
    _seed_sfphi_fixture(tmp_path)
    agent_main.run(repo=tmp_path)
    discovery_dir = tmp_path / "bot" / "state" / "discovery"
    jsonl = next(discovery_dir.glob("discovery_findings_*.jsonl"))
    parsed = [json.loads(line) for line in jsonl.read_text().splitlines() if line.strip()]
    assert len(parsed) >= 2
    assert all("fingerprint" in r and "evidence" in r for r in parsed)
