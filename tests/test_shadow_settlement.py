"""Session 161 — shadow-settlement resolver regressions."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from bot import clv as _clv
from bot import shadow_settlement as ss
from bot import shadow_trades as _shadow

NOW = datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)


def _row(rid, ticker, *, side="yes", contracts=None, entry=None,
         blocked_reason="sport_disabled", sport=None, family=None,
         opp_type=None, status="open"):
    sizing = "available" if (contracts is not None and entry is not None) else "unavailable"
    if opp_type is None:
        opp_type = "vig_stack_series" if blocked_reason == "family_disabled_reject" else "live_momentum"
    return {
        "id": rid, "ts": "2026-05-11T02:20:46.123206+00:00", "ticker": ticker,
        "opp_type": opp_type, "blocked_reason": blocked_reason, "would_side": side,
        "would_entry_price": entry, "would_contracts": contracts,
        "would_notional": round(contracts * entry, 2) if sizing == "available" else None,
        "sizing_status": sizing, "family": family, "sport": sport, "close_ts": None,
        "status": status, "settled_at": None, "market_result": None, "would_pnl": None,
        "source": "executor", "source_decision_reason": blocked_reason,
        "extra": {}, "regime": {},
    }


def _write_shadow(rows):
    path = _shadow.SHADOW_TRADES_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, separators=(",", ":"), default=str) + "\n" for r in rows))


def _write_clv(records):
    path = _clv._get_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, indent=2, default=str))


def _clv_rec(ticker, result, *, status="counterfactual_settled", opp_type="live_momentum"):
    return {"ticker": ticker, "status": status, "market_result": result, "opp_type": opp_type}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Never hit Kalshi in unit tests. Default get_market returns an error
    (=> Tier 3 leaves the row open). Tier-2/3 tests override these."""
    monkeypatch.setattr(ss, "get_market", lambda t: {"error": "no network"})
    monkeypatch.setattr(ss, "_fetch_settled_markets", lambda e: {})


def _resolve(**overrides):
    kwargs = dict(max_event_fetches=50, max_ticker_probes=50, time_budget_sec=60, now=NOW)
    kwargs.update(overrides)
    return ss.resolve_shadow_trades(**kwargs)


def _by_id(rid):
    return next(r for r in ss._load_shadow_rows() if r["id"] == rid)


# --- Tier 1 local join + would_pnl math (mirrors tracker.py:374) -------------

@pytest.mark.parametrize("side,result,contracts,entry,expected_pnl,expected_outcome", [
    ("yes", "yes", 60, 0.82, round(60 * (1 - 0.82), 4), "won"),
    ("yes", "no", 60, 0.82, round(-60 * 0.82, 4), "lost"),
    ("no", "no", 58, 0.86, round(58 * (1 - 0.86), 4), "won"),
    ("no", "yes", 58, 0.86, round(-58 * 0.86, 4), "lost"),
])
def test_available_would_pnl_math(side, result, contracts, entry, expected_pnl, expected_outcome):
    _write_shadow([_row("SHADOW-A", "KXINX-26MAY11H1600-B7412", side=side,
                         contracts=contracts, entry=entry,
                         blocked_reason="family_disabled_reject", family="KXINX")])
    _write_clv([_clv_rec("KXINX-26MAY11H1600-B7412", result, opp_type="vig_stack_series")])

    summary = _resolve()
    row = _by_id("SHADOW-A")

    assert summary["settled_local"] == 1
    assert row["status"] == "settled"
    assert row["market_result"] == result
    assert row["settled_at"] == NOW.isoformat()
    assert row["would_pnl"] == expected_pnl
    assert row["extra"]["would_outcome"] == expected_outcome
    assert row["extra"]["settlement_source"] == "clv_local"


def test_unavailable_row_gets_outcome_flag_but_no_pnl():
    _write_shadow([_row("SHADOW-U", "KXATPMATCH-26MAY10ROCJOH-JOH", side="yes", sport="atp")])
    _write_clv([_clv_rec("KXATPMATCH-26MAY10ROCJOH-JOH", "no")])

    _resolve()
    row = _by_id("SHADOW-U")

    assert row["status"] == "settled"
    assert row["market_result"] == "no"
    assert row["would_pnl"] is None          # never invent a contract count
    assert row["extra"]["would_outcome"] == "lost"


def test_reads_clv_raw_not_active_strategy_filtered():
    # A clv record whose opp_type is NOT an active strategy would be dropped by
    # clv._load(); the resolver reads raw, so it must still settle the shadow row.
    _write_shadow([_row("SHADOW-D", "KXWTAMATCH-26MAY11GAUJOV-GAU", side="yes", sport="wta")])
    _write_clv([_clv_rec("KXWTAMATCH-26MAY11GAUJOV-GAU", "yes", opp_type="totally_disabled_xyz")])

    summary = _resolve()
    assert summary["settled_local"] == 1
    assert _by_id("SHADOW-D")["market_result"] == "yes"


def test_clv_settlement_failed_propagates_dead_mark():
    _write_shadow([_row("SHADOW-F", "KXATPMATCH-26MAY10ROCJOH-JOH", sport="atp")])
    _write_clv([{"ticker": "KXATPMATCH-26MAY10ROCJOH-JOH", "status": "settlement_failed",
                 "opp_type": "live_momentum"}])

    summary = _resolve()
    row = _by_id("SHADOW-F")
    assert summary["dead_marked"] == 1
    assert row["status"] == "settlement_failed"
    assert row["extra"]["settlement_note"] == "clv_settlement_failed"


def test_tier1_dead_marks_malformed_ticker_zero_api():
    # No-day event segment (KXHIGHNY-26APR-*) is provably malformed (clv._is_dead_ticker);
    # dead-mark in Tier 1 with no probe.
    _write_shadow([_row("SHADOW-M", "KXHIGHNY-26APR-T80", side="no",
                        blocked_reason="family_disabled_reject", family="KXHIGHNY")])
    _write_clv([])

    summary = _resolve(max_ticker_probes=0)   # prove zero-API: no probe needed
    row = _by_id("SHADOW-M")
    assert summary["dead_marked"] == 1
    assert row["status"] == "settlement_failed"
    assert row["extra"]["settlement_note"] == "malformed_ticker"


# --- Tier 2 event fetch ------------------------------------------------------

def test_tier2_event_fetch_settles_exact_ticker(monkeypatch):
    # Exact ticker absent from clv, but a sibling in the same event is settled.
    _write_shadow([_row("SHADOW-T60", "KXHIGHCHI-26MAY11-T60", side="no", contracts=157,
                        entry=0.95, blocked_reason="family_disabled_reject", family="KXHIGHCHI")])
    _write_clv([_clv_rec("KXHIGHCHI-26MAY11-T62", "no", opp_type="vig_stack_series")])

    def fake_fetch(event):
        assert event == "KXHIGHCHI-26MAY11"
        return {"KXHIGHCHI-26MAY11-T60": {"status": "settled", "result": "no"}}
    monkeypatch.setattr(ss, "_fetch_settled_markets", fake_fetch)

    summary = _resolve()
    row = _by_id("SHADOW-T60")
    assert summary["settled_event"] == 1
    assert summary["event_fetches"] == 1
    assert row["status"] == "settled"
    assert row["market_result"] == "no"
    assert row["would_pnl"] == round(157 * (1 - 0.95), 4)   # NO won
    assert row["extra"]["settlement_source"] == "event_fetch"


def test_tier2_capped_by_max_event_fetches(monkeypatch):
    _write_shadow([_row("SHADOW-E", "KXHIGHCHI-26MAY11-T60", side="no",
                        blocked_reason="family_disabled_reject", family="KXHIGHCHI")])
    _write_clv([_clv_rec("KXHIGHCHI-26MAY11-T62", "no", opp_type="vig_stack_series")])
    monkeypatch.setattr(ss, "_fetch_settled_markets", lambda e: pytest.fail("should not fetch"))

    # probes also 0 so Tier 3 doesn't reach the row — isolate the Tier-2 cap.
    summary = _resolve(max_event_fetches=0, max_ticker_probes=0)
    assert summary["event_fetches"] == 0
    assert _by_id("SHADOW-E")["status"] == "open"


# --- Tier 3 probe + dead-mark ------------------------------------------------

def test_tier3_probe_settles(monkeypatch):
    _write_shadow([_row("SHADOW-P", "KXATPMATCH-26MAY10AAA-AAA", side="yes", sport="atp")])
    _write_clv([])
    monkeypatch.setattr(ss, "get_market", lambda t: {"status": "settled", "result": "yes"})

    summary = _resolve()
    row = _by_id("SHADOW-P")
    assert summary["settled_probe"] == 1
    assert summary["ticker_probes"] == 1
    assert row["status"] == "settled"
    assert row["market_result"] == "yes"
    assert row["extra"]["settlement_source"] == "ticker_probe"


def test_tier3_dead_marks_confirmed_void(monkeypatch):
    # Terminal status but a non-binary result => confirmed void => dead-mark.
    _write_shadow([_row("SHADOW-G", "KXATPMATCH-26MAY10AAA-AAA", side="yes", sport="atp")])
    _write_clv([])
    monkeypatch.setattr(ss, "get_market", lambda t: {"status": "finalized", "result": ""})

    summary = _resolve()
    row = _by_id("SHADOW-G")
    assert summary["dead_marked"] == 1
    assert row["status"] == "settlement_failed"
    assert row["extra"]["settlement_note"] == "settled_non_binary"


def test_tier3_leaves_open_on_unsettled_market(monkeypatch):
    _write_shadow([_row("SHADOW-W", "KXATPMATCH-26MAY10AAA-AAA", side="yes", sport="atp")])
    _write_clv([])
    monkeypatch.setattr(ss, "get_market", lambda t: {"status": "active"})

    summary = _resolve()
    assert summary["dead_marked"] == 0
    assert summary["still_open"] == 1
    assert _by_id("SHADOW-W")["status"] == "open"


def test_tier3_leaves_open_on_fetch_error_never_false_dead_marks():
    # Default _no_network get_market returns {"error": ...}; a transient failure
    # must NOT dead-mark a forward-only row.
    _write_shadow([_row("SHADOW-ERR", "KXATPMATCH-26MAY10AAA-AAA", side="yes", sport="atp")])
    _write_clv([])

    summary = _resolve()
    assert summary["dead_marked"] == 0
    assert summary["ticker_probes"] == 1
    assert _by_id("SHADOW-ERR")["status"] == "open"


def test_tier3_capped_by_max_ticker_probes(monkeypatch):
    _write_shadow([_row("SHADOW-X", "KXATPMATCH-26MAY10AAA-AAA", side="yes", sport="atp")])
    _write_clv([])
    monkeypatch.setattr(ss, "get_market", lambda t: pytest.fail("should not probe"))
    summary = _resolve(max_ticker_probes=0)
    assert summary["ticker_probes"] == 0
    assert _by_id("SHADOW-X")["status"] == "open"


# --- Persistence: forward-only, no clobber -----------------------------------

def test_persist_preserves_unmutated_and_concurrent_append():
    rows = [_row("SHADOW-1", "KXINX-26MAY11H1600-B7412", side="no", contracts=60, entry=0.82,
                 blocked_reason="family_disabled_reject", family="KXINX"),
            _row("SHADOW-2", "KXINX-26MAY11H1600-B7362", side="no",
                 blocked_reason="family_disabled_reject", family="KXINX")]
    _write_shadow(rows)
    mutated = {"SHADOW-1": {**rows[0], "status": "settled", "market_result": "no"}}

    # Simulate a concurrent append landing between load and persist.
    appended = _row("SHADOW-3", "KXNEW-26MAY22-T1", sport="atp")
    with open(_shadow.SHADOW_TRADES_FILE, "a") as f:
        f.write(json.dumps(appended, separators=(",", ":"), default=str) + "\n")

    applied = ss._persist(mutated)
    ids = {r["id"] for r in ss._load_shadow_rows()}
    assert applied == 1
    assert ids == {"SHADOW-1", "SHADOW-2", "SHADOW-3"}      # nothing dropped
    assert _by_id("SHADOW-1")["status"] == "settled"
    assert _by_id("SHADOW-2")["status"] == "open"
    assert _by_id("SHADOW-3")["status"] == "open"


def test_forward_only_idempotent_rerun():
    _write_shadow([_row("SHADOW-A", "KXINX-26MAY11H1600-B7412", side="no", contracts=60,
                        entry=0.82, blocked_reason="family_disabled_reject", family="KXINX")])
    _write_clv([_clv_rec("KXINX-26MAY11H1600-B7412", "no", opp_type="vig_stack_series")])

    first = _resolve()
    settled_at = _by_id("SHADOW-A")["settled_at"]
    pnl = _by_id("SHADOW-A")["would_pnl"]

    second = _resolve(now=datetime(2026, 5, 23, tzinfo=timezone.utc))
    row = _by_id("SHADOW-A")
    assert first["settled_local"] == 1
    assert second["open_before"] == 0            # nothing open to touch
    assert second["settled_local"] == 0
    assert row["settled_at"] == settled_at        # not reverted / re-stamped
    assert row["would_pnl"] == pnl


def test_untouched_rows_byte_identical():
    rows = [_row("SHADOW-1", "KXINX-26MAY11H1600-B7412", side="no", contracts=60, entry=0.82,
                 blocked_reason="family_disabled_reject", family="KXINX"),
            _row("SHADOW-2", "KXATPMATCH-26MAY10ROCJOH-JOH", side="yes", sport="atp")]
    _write_shadow(rows)
    _write_clv([_clv_rec("KXINX-26MAY11H1600-B7412", "no", opp_type="vig_stack_series")])
    original_line_2 = [ln for ln in _shadow.SHADOW_TRADES_FILE.read_text().splitlines()
                       if '"SHADOW-2"' in ln][0]

    _resolve(max_ticker_probes=0)   # SHADOW-2 has no clv match; leave it untouched
    new_line_2 = [ln for ln in _shadow.SHADOW_TRADES_FILE.read_text().splitlines()
                  if '"SHADOW-2"' in ln][0]
    assert new_line_2 == original_line_2          # untouched row re-serializes identically


def test_no_open_rows_is_noop():
    _write_shadow([_row("SHADOW-DONE", "KXINX-26MAY11H1600-B7412", status="settled")])
    summary = _resolve()
    assert summary == {
        "open_before": 0, "settled_local": 0, "settled_event": 0, "settled_probe": 0,
        "dead_marked": 0, "event_fetches": 0, "ticker_probes": 0, "still_open": 0,
        "persisted": 0,
    }
