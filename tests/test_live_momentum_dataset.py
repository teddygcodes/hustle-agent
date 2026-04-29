"""Tests for tools/live_momentum_dataset.py (Session 30 Stage 1).

Pins the join semantics (scan_found -> tick / bet / clv), forward-return and
MFE/MAE-in-window math, schema-tolerance for missing fields, gzip archive
handling, CSV column stability, and the leakage property test (case 10).
"""
from __future__ import annotations

import csv
import gzip
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.live_momentum_dataset as ds  # noqa: E402


T0 = datetime(2026, 4, 28, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _tick(ticker: str, ts: datetime, *, price: int, bid: int, opp_price: int = None,
          opp_bid: int = None, leader: bool = True, **extra) -> dict:
    if opp_price is None:
        opp_price = 100 - price
    if opp_bid is None:
        opp_bid = max(0, opp_price - 1)
    base = {
        "ticker": ticker,
        "ts": _iso(ts),
        "sport": extra.pop("sport", "nba"),
        "match": extra.pop("match", "Test Game"),
        "price": price,
        "bid": bid,
        "opp_price": opp_price,
        "opp_bid": opp_bid,
        "leader": leader,
        "opp_leader": not leader,
        "wp": extra.pop("wp", 0.6),
        "wp_edge": extra.pop("wp_edge", 0.05),
        "momentum": extra.pop("momentum", 0.1),
        "lead_trend": extra.pop("lead_trend", 0.05),
        "dip": extra.pop("dip", 2),
        "recent_high": extra.pop("recent_high", price + 2),
        "opp_recent_high": extra.pop("opp_recent_high", opp_price + 2),
        "completion": extra.pop("completion", 0.5),
        "elapsed": extra.pop("elapsed", 1800),
        "period": extra.pop("period", 2),
        "score_diff": extra.pop("score_diff", 5),
        "volatility": extra.pop("volatility", "normal"),
    }
    base.update(extra)
    return base


def _scan_found(ticker: str, ts: datetime, *, skip_reason: str = None,
                sport: str = "nba", match: str = "Test Game", price: int = 70) -> dict:
    ev = {
        "event": "scan_found",
        "ticker": ticker,
        "timestamp": _iso(ts),
        "sport": sport,
        "match": match,
        "price": price,
        "volume": 50000,
    }
    if skip_reason:
        ev["skip_reason"] = skip_reason
    return ev


def _bet(ticker: str, ts: datetime, *, price_cents: int = 70, side: str = "yes",
         contracts: int = 19, sport: str = "nba") -> dict:
    return {
        "event": "bet",
        "ticker": ticker,
        "timestamp": _iso(ts),
        "side": side,
        "contracts": contracts,
        "price_cents": price_cents,
        "mode": "momentum",
        "sport": sport,
        "match": "Test Game",
    }


def _exit(ticker: str, ts: datetime, *, pnl: float = 1.50) -> dict:
    return {
        "event": "exit",
        "ticker": ticker,
        "timestamp": _iso(ts),
        "pnl": pnl,
        "reason": "TAKE_PROFIT",
        "mode": "momentum",
    }


def _build_ticks_stream(ticker: str, anchor: datetime, prices_by_offset_secs: dict[int, int]) -> list[dict]:
    """Produce a tick stream where the leg's ``price`` follows the offset->price map.

    ``opp_price`` is derived as 100 - price for simplicity.
    """
    return [
        _tick(ticker, anchor + timedelta(seconds=off), price=p, bid=p - 1)
        for off, p in sorted(prices_by_offset_secs.items())
    ]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _write_jsonl_gz(path: Path, rows: list[dict]) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _write_journal(path: Path, events: list[dict]) -> None:
    path.write_text(json.dumps(events))


def _write_clv(path: Path, recs: list[dict]) -> None:
    path.write_text(json.dumps(recs))


@pytest.fixture
def tmp_paths(tmp_path):
    return {
        "ticks": tmp_path / "live_ticks.jsonl",
        "archive": tmp_path / "archive",
        "journal": tmp_path / "live_journal.json",
        "clv": tmp_path / "clv.json",
        "out": tmp_path / "out" / "dataset.csv",
    }


def _run_pipeline(tmp_paths, *, days: int = 30, horizon: int = 120) -> list[dict]:
    """Helper: load + build rows from fixture files (no CSV write)."""
    now = T0 + timedelta(seconds=600)
    ticks = list(ds.load_ticks(days, now, str(tmp_paths["ticks"]), str(tmp_paths["archive"])))
    journal = ds.load_journal_events(days, now, str(tmp_paths["journal"]))
    clv = ds.load_clv_records(days, now, str(tmp_paths["clv"]))
    return list(ds.build_decision_rows(ticks, journal, clv, horizon))


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestTimestampJoin:
    """Case 1: decision rows match nearest tick within +-60s window."""

    def test_decision_picks_latest_tick_at_or_before_ts(self, tmp_paths):
        ticker = "KXNBAGAME-A"
        # Ticks at -10s, +0s, +30s relative to scan; decision_tick should be -10s
        ticks = [
            _tick(ticker, T0 - timedelta(seconds=10), price=68, bid=67),
            _tick(ticker, T0 + timedelta(seconds=30), price=72, bid=71),
        ]
        # ensure forward-return horizons can be measured; add a forward tick
        ticks.append(_tick(ticker, T0 + timedelta(seconds=125), price=75, bid=74))
        _write_jsonl(tmp_paths["ticks"], ticks)
        _write_journal(tmp_paths["journal"], [_scan_found(ticker, T0, skip_reason="low_volume")])
        _write_clv(tmp_paths["clv"], [])

        rows = _run_pipeline(tmp_paths)
        assert len(rows) == 1
        # decision tick was the -10s tick, where price=68
        assert rows[0]["leader_price"] == 68

    def test_no_tick_within_lookback_drops_row(self, tmp_paths):
        ticker = "KXNBAGAME-A"
        # Only a tick way in the future
        ticks = [_tick(ticker, T0 + timedelta(seconds=120), price=70, bid=69)]
        _write_jsonl(tmp_paths["ticks"], ticks)
        _write_journal(tmp_paths["journal"], [_scan_found(ticker, T0, skip_reason="low_volume")])
        _write_clv(tmp_paths["clv"], [])

        rows = _run_pipeline(tmp_paths)
        assert rows == []


class TestForwardReturns:
    """Case 2: forward returns at +30s, +60s, +120s computed correctly."""

    def test_forward_returns_exact_cents(self, tmp_paths):
        ticker = "KXNBAGAME-A"
        ticks = _build_ticks_stream(ticker, T0, {
            -5: 70,    # decision tick (price=70)
            30: 73,    # +30s -> +3c
            60: 75,    # +60s -> +5c
            120: 72,   # +120s -> +2c
        })
        _write_jsonl(tmp_paths["ticks"], ticks)
        _write_journal(tmp_paths["journal"], [_scan_found(ticker, T0, skip_reason="low_volume")])
        _write_clv(tmp_paths["clv"], [])

        rows = _run_pipeline(tmp_paths)
        assert len(rows) == 1
        r = rows[0]
        assert r["leader_price"] == 70
        assert r["fwd_return_30s_cents"] == 3.0
        assert r["fwd_return_60s_cents"] == 5.0
        assert r["fwd_return_120s_cents"] == 2.0

    def test_no_forward_tick_yields_none(self, tmp_paths):
        ticker = "KXNBAGAME-A"
        # Only a tick AT/BEFORE decision_ts exists; no forward ticks at all
        ticks = [_tick(ticker, T0 - timedelta(seconds=2), price=70, bid=69)]
        _write_jsonl(tmp_paths["ticks"], ticks)
        _write_journal(tmp_paths["journal"], [_scan_found(ticker, T0, skip_reason="low_volume")])
        _write_clv(tmp_paths["clv"], [])

        rows = _run_pipeline(tmp_paths)
        assert len(rows) == 1
        r = rows[0]
        # No tick exists past decision_ts so no forward return can be measured
        assert r["fwd_return_30s_cents"] is None
        assert r["fwd_return_60s_cents"] is None
        assert r["fwd_return_120s_cents"] is None


class TestMfeMaeMath:
    """Case 3: hand-computed MFE/MAE-in-window."""

    def test_mfe_mae_window_120s(self, tmp_paths):
        ticker = "KXNBAGAME-A"
        ticks = _build_ticks_stream(ticker, T0, {
            -1: 70,     # decision tick
            10: 73,     # MFE candidate +3
            40: 67,     # MAE candidate -3 (i.e., adverse 3)
            80: 75,     # MFE candidate +5 (winner)
            110: 72,    # within window
            130: 80,    # OUT of window (post-horizon, must NOT count)
        })
        _write_jsonl(tmp_paths["ticks"], ticks)
        _write_journal(tmp_paths["journal"], [_scan_found(ticker, T0, skip_reason="low_volume")])
        _write_clv(tmp_paths["clv"], [])

        rows = _run_pipeline(tmp_paths)
        assert len(rows) == 1
        r = rows[0]
        assert r["leader_price"] == 70
        assert r["mfe_in_120s_window_cents"] == 5.0  # max(73,75,72,67) - 70 = 5
        assert r["mae_in_120s_window_cents"] == 3.0  # 70 - min(67) = 3 (magnitude)


class TestSchemaTolerance:
    """Cases 4 & 5: missing fields don't crash."""

    def test_missing_wp_field_doesnt_crash(self, tmp_paths):
        ticker = "KXOLDARCHIVE-A"
        # Pre-Session-23 style tick: no wp_edge / momentum / lead_trend
        old_tick = {
            "ticker": ticker,
            "ts": _iso(T0 - timedelta(seconds=5)),
            "sport": "nba",
            "match": "Old Game",
            "price": 65,
            "bid": 64,
            "opp_price": 35,
            "opp_bid": 33,
            "leader": True,
            "opp_leader": False,
            "dip": 1,
            "recent_high": 67,
            "opp_recent_high": 38,
        }
        forward = _tick(ticker, T0 + timedelta(seconds=125), price=68, bid=67)
        _write_jsonl(tmp_paths["ticks"], [old_tick, forward])
        _write_journal(tmp_paths["journal"], [_scan_found(ticker, T0, skip_reason="no_leader")])
        _write_clv(tmp_paths["clv"], [])

        rows = _run_pipeline(tmp_paths)
        assert len(rows) == 1
        r = rows[0]
        assert r["wp"] is None
        assert r["wp_edge"] is None
        assert r["momentum"] is None
        assert r["leader_price"] == 65

    def test_missing_espn_score_doesnt_crash(self, tmp_paths):
        ticker = "KXATPMATCH-A"
        # Tennis-style tick: period=None, score_diff=None, espn_scores=None
        sparse = _tick(
            ticker, T0 - timedelta(seconds=2),
            price=60, bid=59, sport="atp",
            period=None, score_diff=None, completion=0.0,
        )
        sparse["espn_scores"] = None
        forward = _tick(ticker, T0 + timedelta(seconds=125), price=63, bid=62, sport="atp")
        _write_jsonl(tmp_paths["ticks"], [sparse, forward])
        _write_journal(tmp_paths["journal"], [_scan_found(ticker, T0, skip_reason="low_volume", sport="atp")])
        _write_clv(tmp_paths["clv"], [])

        rows = _run_pipeline(tmp_paths)
        assert len(rows) == 1
        assert rows[0]["period"] is None
        assert rows[0]["score_diff"] is None


class TestArchiveHandling:
    """Case 6: gzipped archive ticks load."""

    def test_handles_gzipped_archive(self, tmp_paths):
        ticker = "KXNBAGAME-A"
        archive_dir = tmp_paths["archive"]
        archive_dir.mkdir()
        gz_path = archive_dir / "live_ticks-2026-04-28-1.jsonl.gz"
        ticks = [
            _tick(ticker, T0 - timedelta(seconds=3), price=70, bid=69),
            _tick(ticker, T0 + timedelta(seconds=125), price=73, bid=72),
        ]
        _write_jsonl_gz(gz_path, ticks)
        # No live_ticks.jsonl present
        _write_journal(tmp_paths["journal"], [_scan_found(ticker, T0, skip_reason="low_volume")])
        _write_clv(tmp_paths["clv"], [])

        rows = _run_pipeline(tmp_paths)
        assert len(rows) == 1
        assert rows[0]["leader_price"] == 70


class TestCsvOutput:
    """Cases 7 & 8: CSV columns stable; outcome columns discoverable."""

    def test_csv_output_has_all_required_columns(self, tmp_paths):
        ticker = "KXNBAGAME-A"
        ticks = [
            _tick(ticker, T0 - timedelta(seconds=2), price=70, bid=69),
            _tick(ticker, T0 + timedelta(seconds=125), price=73, bid=72),
        ]
        _write_jsonl(tmp_paths["ticks"], ticks)
        _write_journal(tmp_paths["journal"], [_scan_found(ticker, T0, skip_reason="low_volume")])
        _write_clv(tmp_paths["clv"], [])

        rows = _run_pipeline(tmp_paths)
        ds.write_csv(rows, str(tmp_paths["out"]), 120)

        with open(tmp_paths["out"]) as f:
            reader = csv.DictReader(f)
            header = reader.fieldnames
        assert header == ds.required_columns(120)

    def test_outcome_columns_tagged_separately(self):
        cols = ds.required_columns(120)
        outcome = [c for c in cols if c.startswith("outcome_") or "fwd_return_" in c or "_in_120s_window_" in c]
        assert "outcome_clv_cents" in outcome
        assert "outcome_realized_pnl" in outcome
        assert "outcome_target_yes_price_cents" in outcome
        assert "fwd_return_30s_cents" in outcome
        assert "fwd_return_60s_cents" in outcome
        assert "fwd_return_120s_cents" in outcome
        assert "mfe_in_120s_window_cents" in outcome
        assert "mae_in_120s_window_cents" in outcome
        # No collision with Session 9's `mfe_cents`
        assert "mfe_cents" not in cols
        assert "mae_cents" not in cols


class TestSkipReasonGating:
    """Case 9: tunable skip_reason emits row, structural reject does not."""

    def test_tunable_skip_reason_emits_row_structural_does_not(self, tmp_paths):
        ticker_a = "KXNBAGAME-A"
        ticker_b = "KXNBAGAME-B"
        ticks = [
            _tick(ticker_a, T0 - timedelta(seconds=2), price=70, bid=69),
            _tick(ticker_a, T0 + timedelta(seconds=125), price=72, bid=71),
            _tick(ticker_b, T0 - timedelta(seconds=2), price=70, bid=69),
            _tick(ticker_b, T0 + timedelta(seconds=125), price=72, bid=71),
        ]
        _write_jsonl(tmp_paths["ticks"], ticks)
        _write_journal(tmp_paths["journal"], [
            _scan_found(ticker_a, T0, skip_reason="low_volume"),    # tunable -> emit
            _scan_found(ticker_b, T0, skip_reason="capacity_capped"),  # structural -> drop
        ])
        _write_clv(tmp_paths["clv"], [])

        rows = _run_pipeline(tmp_paths)
        assert len(rows) == 1
        assert rows[0]["ticker"] == ticker_a
        assert rows[0]["skip_reason"] == "low_volume"
        assert rows[0]["accept"] is False

    def test_accept_emits_one_row_per_bet_event(self, tmp_paths):
        ticker = "KXNBAGAME-A"
        ticks = [
            _tick(ticker, T0 - timedelta(seconds=2), price=70, bid=69),
            _tick(ticker, T0 + timedelta(seconds=125), price=80, bid=79),
        ]
        _write_jsonl(tmp_paths["ticks"], ticks)
        # Bet (mode='momentum') is the decision; no scan_found needed for accept.
        _write_journal(tmp_paths["journal"], [
            _bet(ticker, T0, price_cents=70, side="yes"),
            _exit(ticker, T0 + timedelta(seconds=200), pnl=1.90),
        ])
        _write_clv(tmp_paths["clv"], [])

        rows = _run_pipeline(tmp_paths)
        assert len(rows) == 1
        r = rows[0]
        assert r["accept"] is True
        assert r["skip_reason"] is None
        assert r["leader_side"] == "yes"
        assert r["outcome_realized_pnl"] == 1.90


class TestLeakageProperty:
    """Case 10: THE leakage test — decision-time field values must not change
    when we shift decision_ts back in time. If a decision-time field's value
    changes after the shift, that field used data with ts > original_decision_ts.
    """

    def test_no_decision_time_field_uses_post_decision_data(self, tmp_paths):
        ticker = "KXNBAGAME-A"

        # Tick at decision_ts - 5s with one set of values
        decision_tick_old = _tick(
            ticker, T0 - timedelta(seconds=5),
            price=70, bid=69, wp=0.6, wp_edge=0.04, momentum=0.10,
            lead_trend=0.05, dip=2, period=2, score_diff=5, completion=0.50,
        )
        # Tick at decision_ts + 1s — would-be poisoning data if any field reads forward
        post_decision_tick = _tick(
            ticker, T0 + timedelta(seconds=1),
            price=99, bid=98, wp=0.99, wp_edge=0.99, momentum=0.99,
            lead_trend=0.99, dip=99, period=4, score_diff=99, completion=0.99,
        )
        forward = _tick(ticker, T0 + timedelta(seconds=125), price=80, bid=79)
        _write_jsonl(tmp_paths["ticks"], [decision_tick_old, post_decision_tick, forward])
        _write_journal(tmp_paths["journal"], [_scan_found(ticker, T0, skip_reason="low_volume")])
        _write_clv(tmp_paths["clv"], [])

        rows = _run_pipeline(tmp_paths)
        assert len(rows) == 1
        r = rows[0]

        # Decision-time fields must read ONLY from the -5s tick (old values).
        # If any of these match the post-decision-tick poisoned values, leakage exists.
        decision_time_fields = {
            "leader_price": 70,
            "wp": 0.6,
            "wp_edge": 0.04,
            "momentum": 0.10,
            "lead_trend": 0.05,
            "dip": 2,
            "period": 2,
            "score_diff": 5,
            "completion": 0.50,
        }
        for field, expected in decision_time_fields.items():
            assert r[field] == expected, (
                f"LEAKAGE: field {field!r} = {r[field]!r}, expected {expected!r} "
                f"(post-decision value was 99)"
            )

        # Now shift decision back by 60s. Since the -5s tick is still the latest
        # at-or-before the shifted decision_ts (-65s), a clean implementation must
        # either pick the same tick OR no tick (lookback exceeded). Either way,
        # the decision-time field values must NOT come from the post-decision tick.
        shifted = T0 - timedelta(seconds=60)
        _write_journal(tmp_paths["journal"], [_scan_found(ticker, shifted, skip_reason="low_volume")])

        rows2 = _run_pipeline(tmp_paths)
        # Either rows2 is empty (lookback exceeded) or matches the OLD tick values.
        # In NEITHER case should the post-decision values appear.
        for r2 in rows2:
            for field, _ in decision_time_fields.items():
                assert r2[field] != 99, (
                    f"LEAKAGE on shifted run: field {field!r} took post-decision value 99"
                )


class TestCfWithoutTicks:
    """Session 30-followup-2: CFs lacking tick context still emit a row.

    Live_momentum CFs come from match-level pre-watcher gates (no_leader,
    low_volume, no_vol_growth_*), so live_ticks.jsonl has nothing for the
    ticker. Pre-fix the dataset dropped these at find_decision_tick is None.
    Post-fix they appear with null decision-time features and populated
    identity / regime / outcome columns.
    """

    def _cf(self, ticker: str, *, status: str, ts: datetime, gate: str = "no_leader",
            entry: int = 70, side: str = "yes", sport: str = "atp_challenger",
            closing: int | None = None, market_result: str | None = None,
            clv_cents: float | None = None, clv_relative: float | None = None,
            trade_id: str | None = None) -> dict:
        rec = {
            "ticker": ticker,
            "opp_type": "live_momentum",
            "status": status,
            "trade_id": trade_id or f"CF-LM-{ts.strftime('%Y%m%dT%H%M%SZ')}-{ticker}",
            "scan_event_ts": _iso(ts),
            "recorded_at": _iso(ts + timedelta(milliseconds=10)),
            "entry_price_cents": entry,
            "side": side,
            "sport": sport,
            "skipped_by_gate": gate,
            "regime": {
                "time_of_day": "afternoon",
                "day_of_week": "Tue",
                "sport_phase": None,
                "event_horizon_hr": "<2h",
            },
        }
        if status == "counterfactual_settled":
            rec["closing_yes_price"] = closing
            rec["market_result"] = market_result
            rec["clv_cents"] = clv_cents
            rec["clv_relative"] = clv_relative
        return rec

    def test_cf_with_no_ticks_and_no_journal_emits_row(self, tmp_paths):
        """The 33-ticker case: CF exists in clv but no journal scan_found, no ticks.
        Pre-fix: dataset has 0 rows. Post-fix: 1 row from the clv sweep.
        """
        ticker = "KXATPCHALLENGERMATCH-26APR27HSUNOG-HSU"
        cf = self._cf(ticker, status="counterfactual_open", ts=T0, gate="no_leader",
                      entry=72, side="yes", sport="atp_challenger")
        _write_jsonl(tmp_paths["ticks"], [])
        _write_journal(tmp_paths["journal"], [])
        _write_clv(tmp_paths["clv"], [cf])

        rows = _run_pipeline(tmp_paths)
        assert len(rows) == 1
        r = rows[0]
        assert r["ticker"] == ticker
        assert r["accept"] is False
        assert r["skip_reason"] == "no_leader"
        assert r["sport"] == "atp_challenger"
        assert r["leader_side"] == "yes"
        assert r["leader_price"] == 72
        # Decision-time features all null
        for f in ("wp", "wp_edge", "momentum", "lead_trend", "dip", "dqs",
                  "period", "score_diff", "completion", "elapsed", "volatility",
                  "leader", "opp_leader", "recent_high", "opp_recent_high",
                  "spread_cents"):
            assert r[f] is None, f"{f} should be None for CF without ticks"
        # Forward returns / MFE / MAE all null (no subsequent ticks)
        assert r["fwd_return_30s_cents"] is None
        assert r["fwd_return_60s_cents"] is None
        assert r["fwd_return_120s_cents"] is None
        assert r["mfe_in_120s_window_cents"] is None
        assert r["mae_in_120s_window_cents"] is None
        # Outcome columns null because CF is still open
        assert r["outcome_clv_cents"] is None
        assert r["outcome_settlement"] is None

    def test_cf_with_journal_but_no_ticks_emits_row(self, tmp_paths):
        """The 103-ticker case: journal scan_found exists with the tunable
        skip_reason, but no ticks for the ticker. Pre-fix: dropped by
        find_decision_tick is None. Post-fix: emitted via the no-tick fallback.
        """
        ticker = "KXATPCHALLENGERMATCH-A"
        cf = self._cf(ticker, status="counterfactual_settled", ts=T0,
                      gate="low_volume", entry=68, side="yes",
                      sport="atp_challenger",
                      closing=100, market_result="yes", clv_cents=32, clv_relative=0.47)
        _write_jsonl(tmp_paths["ticks"], [])
        _write_journal(tmp_paths["journal"], [
            _scan_found(ticker, T0, skip_reason="low_volume", sport="atp_challenger"),
        ])
        _write_clv(tmp_paths["clv"], [cf])

        rows = _run_pipeline(tmp_paths)
        # Exactly one row — both paths see the same CF; dedup on trade_id
        assert len(rows) == 1
        r = rows[0]
        assert r["ticker"] == ticker
        assert r["accept"] is False
        assert r["skip_reason"] == "low_volume"
        assert r["leader_price"] == 68
        # Outcome columns from settled CF
        assert r["outcome_clv_cents"] == 32
        assert r["outcome_target_yes_price_cents"] == 100.0
        assert r["outcome_settlement"] == "yes_won"
        # Decision-time features still null (no ticks)
        assert r["wp"] is None
        assert r["dip"] is None

    def test_cf_with_journal_and_ticks_does_not_double_emit(self, tmp_paths):
        """When the journal-driven path successfully emits a tick-rich row AND
        the CF matches, the post-loop CF sweep must not emit a duplicate.
        """
        ticker = "KXNBAGAME-COVERED"
        ticks = [
            _tick(ticker, T0 - timedelta(seconds=2), price=70, bid=69),
            _tick(ticker, T0 + timedelta(seconds=125), price=72, bid=71),
        ]
        cf = self._cf(ticker, status="counterfactual_settled", ts=T0,
                      gate="low_volume", entry=70, side="yes", sport="nba",
                      closing=80, market_result="yes", clv_cents=10, clv_relative=0.143)
        _write_jsonl(tmp_paths["ticks"], ticks)
        _write_journal(tmp_paths["journal"], [
            _scan_found(ticker, T0, skip_reason="low_volume"),
        ])
        _write_clv(tmp_paths["clv"], [cf])

        rows = _run_pipeline(tmp_paths)
        assert len(rows) == 1
        r = rows[0]
        # Tick-rich row: decision-time features populated from the tick
        assert r["wp"] == 0.6
        assert r["leader_price"] == 70
        # Outcome from CF
        assert r["outcome_clv_cents"] == 10

    def test_settled_cf_yields_target_yes_price_and_settlement_label(self, tmp_paths):
        ticker = "KXWTACHALLENGERMATCH-A"
        cf = self._cf(ticker, status="counterfactual_settled", ts=T0,
                      gate="no_vol_growth_idle", entry=65, side="no",
                      sport="wta_challenger",
                      closing=0, market_result="no", clv_cents=35, clv_relative=0.538)
        _write_jsonl(tmp_paths["ticks"], [])
        _write_journal(tmp_paths["journal"], [])
        _write_clv(tmp_paths["clv"], [cf])

        rows = _run_pipeline(tmp_paths)
        assert len(rows) == 1
        r = rows[0]
        assert r["outcome_target_yes_price_cents"] == 0.0
        assert r["outcome_settlement"] == "no_won"
        assert r["outcome_clv_cents"] == 35
        assert r["leader_side"] == "no"

    def test_cf_only_sweep_excludes_real_settled_records(self, tmp_paths):
        """The post-loop sweep must only emit `counterfactual_*` records, not
        `settled` real trades (those are joined via the journal accept path).
        """
        ticker_real = "KXNBAGAME-REAL"
        ticker_cf = "KXATPCHALLENGERMATCH-CF"
        # Real trade record (status='settled') with no journal coverage
        real = {
            "ticker": ticker_real,
            "opp_type": "live_momentum",
            "status": "settled",
            "trade_id": "T-REAL-1",
            "scan_event_ts": _iso(T0),
            "recorded_at": _iso(T0),
            "entry_price_cents": 70,
            "side": "yes",
            "sport": "nba",
            "closing_yes_price": 100,
            "market_result": "yes",
            "clv_cents": 30,
            "clv_relative": 0.43,
        }
        cf = self._cf(ticker_cf, status="counterfactual_open", ts=T0, gate="no_leader",
                      entry=70, side="yes", sport="atp_challenger")
        _write_jsonl(tmp_paths["ticks"], [])
        _write_journal(tmp_paths["journal"], [])
        _write_clv(tmp_paths["clv"], [real, cf])

        rows = _run_pipeline(tmp_paths)
        # Only the CF row; the real settled record is not emitted (no journal bet event)
        assert len(rows) == 1
        assert rows[0]["ticker"] == ticker_cf


class TestClvJoin:
    """Bonus: clv joins for accept and reject correctly."""

    def test_counterfactual_clv_joins_on_scan_event_ts(self, tmp_paths):
        ticker = "KXNBAGAME-A"
        ticks = [
            _tick(ticker, T0 - timedelta(seconds=2), price=70, bid=69),
            _tick(ticker, T0 + timedelta(seconds=125), price=72, bid=71),
        ]
        _write_jsonl(tmp_paths["ticks"], ticks)
        _write_journal(tmp_paths["journal"], [_scan_found(ticker, T0, skip_reason="low_volume")])
        _write_clv(tmp_paths["clv"], [{
            "ticker": ticker,
            "opp_type": "live_momentum",
            "status": "counterfactual_settled",
            "scan_event_ts": _iso(T0),
            "recorded_at": _iso(T0),
            "entry_price_cents": 70,
            "closing_yes_price": 100,
            "clv_cents": 30,
            "clv_relative": 0.4286,
            "market_result": "yes",
            "skipped_by_gate": "low_volume",
        }])

        rows = _run_pipeline(tmp_paths)
        assert len(rows) == 1
        assert rows[0]["outcome_clv_cents"] == 30
        assert rows[0]["outcome_target_yes_price_cents"] == 100
        assert rows[0]["outcome_settlement"] == "yes_won"
