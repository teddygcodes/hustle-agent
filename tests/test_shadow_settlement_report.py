"""Session 161 — were-the-blocks-right report generator."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

report = importlib.import_module("tools.shadow_settlement_report")


def _settled(ticker, *, reason, side, outcome, pnl=None, contracts=None, entry=None,
             family=None, sport=None, day="2026-05-11", source="clv_local"):
    mr = side if outcome == "won" else ("no" if side == "yes" else "yes")
    return {
        "id": f"SHADOW-{ticker}-{day}", "ts": f"{day}T02:00:00+00:00", "ticker": ticker,
        "opp_type": "vig_stack_series" if reason == "family_disabled_reject" else "live_momentum",
        "blocked_reason": reason, "would_side": side, "would_entry_price": entry,
        "would_contracts": contracts, "would_notional": None,
        "sizing_status": "available" if contracts else "unavailable",
        "family": family, "sport": sport, "close_ts": None, "status": "settled",
        "settled_at": f"{day}T16:00:00+00:00", "market_result": mr, "would_pnl": pnl,
        "source": "executor", "source_decision_reason": reason,
        "extra": {"would_outcome": outcome, "settlement_source": source}, "regime": {},
    }


def test_family_cohort_pnl_wr_dedup_and_decision():
    rows = [
        _settled("KXINX-T1", reason="family_disabled_reject", side="no", outcome="won",
                 pnl=10.0, contracts=60, entry=0.83, family="KXINX"),
        # duplicate (same ticker+day) — collapses on dedup
        _settled("KXINX-T1", reason="family_disabled_reject", side="no", outcome="won",
                 pnl=10.0, contracts=60, entry=0.83, family="KXINX"),
        _settled("KXINX-T2", reason="family_disabled_reject", side="no", outcome="lost",
                 pnl=-50.0, contracts=60, entry=0.83, family="KXINX"),
    ]
    out = report.build_report(rows)

    assert "KXINX" in out
    assert "67%" in out          # WR raw = 2/3
    assert "50%" in out          # WR dedup = 1/2
    assert "$-30.00" in out      # pnl raw = +10+10-50
    assert "$-40.00" in out      # pnl dedup = +10-50
    assert "block data-CONFIRMED" in out   # wr_dedup 50% not >55, pnl_dedup negative
    assert "N-thin" in out       # dedup_n = 2 < 10


def test_sport_cohort_direction_only_wrong_block():
    # 12 unique winning-heavy atp rows -> clears N-thin, WR>55 -> re-enable candidate.
    rows = []
    for i in range(8):
        rows.append(_settled(f"KXATPMATCH-26MAY10A{i}-A", reason="sport_disabled",
                              side="yes", outcome="won", sport="atp", day=f"2026-05-{10+i:02d}"))
    for i in range(4):
        rows.append(_settled(f"KXATPMATCH-26MAY10B{i}-B", reason="sport_disabled",
                              side="yes", outcome="lost", sport="atp", day=f"2026-05-{10+i:02d}"))
    out = report.build_report(rows)

    assert "atp" in out
    assert "8/4" in out                  # won/lost
    assert "67%" in out                  # WR
    assert "block likely WRONG (re-enable candidate)" in out
    # large cohort -> the atp row is NOT flagged N-thin (the row text has no warning glyph)
    atp_line = [ln for ln in out.splitlines() if ln.startswith("| atp ")][0]
    assert "N-thin" not in atp_line


def test_coverage_section_counts_and_sources():
    rows = [
        _settled("KXINX-T1", reason="family_disabled_reject", side="no", outcome="won",
                 pnl=5.0, contracts=10, entry=0.5, family="KXINX", source="clv_local"),
        _settled("KXATPMATCH-26MAY10X-X", reason="sport_disabled", side="yes",
                 outcome="lost", sport="atp", source="ticker_probe"),
        {"id": "SHADOW-OPEN", "ticker": "KXWTAMATCH-1", "status": "open",
         "blocked_reason": "sport_disabled", "sport": "wta", "extra": {}, "ts": "2026-05-12T00:00:00+00:00"},
        {"id": "SHADOW-DEAD", "ticker": "KXTEST-1", "status": "settlement_failed",
         "blocked_reason": "sport_disabled", "sport": "atp", "extra": {}, "ts": "2026-05-12T00:00:00+00:00"},
    ]
    out = report.build_report(rows)

    assert "total shadow rows: **4**" in out
    assert "settled: **2**" in out
    assert "settlement_failed: 1" in out
    assert "still open: 1" in out
    assert "clv_local=1" in out
    assert "ticker_probe=1" in out


def test_empty_rows_renders_placeholders():
    out = report.build_report([])
    assert "total shadow rows: **0**" in out
    assert "_(none settled)_" in out
