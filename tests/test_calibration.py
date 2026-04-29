"""Tests for bot/calibration.py — append-only JSONL prediction log.

Session 11 (Apr 25): every record_clv_entry / record_counterfactual_skip call
funnels through `record_prediction`. These tests pin schema, atomic append,
the never-raise contract, idempotency on (scan_id, ticker), and the ±60s
settlement matching window.
"""
from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import calibration  # noqa: E402


@pytest.fixture
def tmp_predictions_file(tmp_path, monkeypatch):
    f = tmp_path / "predictions.jsonl"
    monkeypatch.setattr(calibration, "PREDICTIONS_FILE", f)
    monkeypatch.setattr("bot.calibration.BOT_STATE_DIR", tmp_path)
    return f


def _read_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class TestSchemaIntegrity:

    def test_basic_record_has_required_fields(self, tmp_predictions_file):
        calibration.record_prediction(
            ticker="KXHIGHMIA-26APR25-T80",
            opp_type="vig_stack_series",
            predicted_fair_cents=87.5,
            market_price_cents=82,
            scan_id="20260425T120000",
        )
        recs = _read_records(tmp_predictions_file)
        assert len(recs) == 1
        r = recs[0]
        assert r["ticker"] == "KXHIGHMIA-26APR25-T80"
        assert r["opp_type"] == "vig_stack_series"
        assert r["predicted_fair_cents"] == 87.5
        assert r["market_price_cents"] == 82
        assert r["scan_id"] == "20260425T120000"
        assert r["closing_yes_price"] is None
        assert "ts" in r and r["ts"].endswith("+00:00")

    def test_predicted_rounded_to_2_decimals(self, tmp_predictions_file):
        calibration.record_prediction(
            ticker="X", opp_type="vig_stack_series",
            predicted_fair_cents=87.123456,
            market_price_cents=80, scan_id="s1",
        )
        recs = _read_records(tmp_predictions_file)
        assert recs[0]["predicted_fair_cents"] == 87.12

    def test_market_price_coerced_to_int(self, tmp_predictions_file):
        calibration.record_prediction(
            ticker="X", opp_type="vig_stack_series",
            predicted_fair_cents=50.0,
            market_price_cents=82.7,  # type: ignore[arg-type]
            scan_id="s1",
        )
        recs = _read_records(tmp_predictions_file)
        assert recs[0]["market_price_cents"] == 82
        assert isinstance(recs[0]["market_price_cents"], int)

    def test_none_predicted_skips_silently(self, tmp_predictions_file):
        calibration.record_prediction(
            ticker="X", opp_type="live_momentum",
            predicted_fair_cents=None,
            market_price_cents=80, scan_id="s1",
        )
        assert _read_records(tmp_predictions_file) == []

    def test_zero_predicted_skips_silently(self, tmp_predictions_file):
        calibration.record_prediction(
            ticker="X", opp_type="live_momentum",
            predicted_fair_cents=0.0,
            market_price_cents=80, scan_id="s1",
        )
        assert _read_records(tmp_predictions_file) == []

    def test_explicit_recorded_at_used(self, tmp_predictions_file):
        ts = "2026-04-25T12:00:00+00:00"
        calibration.record_prediction(
            ticker="X", opp_type="vig_stack_series",
            predicted_fair_cents=80.0, market_price_cents=70,
            scan_id="s1", recorded_at=ts,
        )
        recs = _read_records(tmp_predictions_file)
        assert recs[0]["ts"] == ts

    def test_record_carries_regime_dict(self, tmp_predictions_file):
        """Session 14: every prediction row gets `regime` with all 4 axes.
        market_state is not threaded through this writer, so event_horizon_hr
        is expected to be None — other axes still populate."""
        calibration.record_prediction(
            ticker="KXMLBGAME-26APR25-LAA",
            opp_type="vig_stack_series",
            predicted_fair_cents=42.0,
            market_price_cents=38,
            scan_id="scan-x",
        )
        r = _read_records(tmp_predictions_file)[-1]
        assert "regime" in r
        regime = r["regime"]
        assert set(regime.keys()) == {
            "time_of_day", "day_of_week", "sport_phase", "event_horizon_hr", "match_phase",
        }
        # MLB ticker → sport_phase resolves
        assert regime["sport_phase"] in {"preseason", "regular", "playoffs", "off"}
        # No market_state at this writer
        assert regime["event_horizon_hr"] is None


class TestAtomicAppend:

    def test_concurrent_writes_all_land(self, tmp_predictions_file):
        """20 threads × 10 writes each with unique (scan_id, ticker) = 200 records."""
        N_THREADS = 20
        N_PER_THREAD = 10

        def writer(thread_id):
            for i in range(N_PER_THREAD):
                calibration.record_prediction(
                    ticker=f"T{thread_id}-{i}",
                    opp_type="vig_stack_series",
                    predicted_fair_cents=80.0,
                    market_price_cents=70,
                    scan_id=f"scan-{thread_id}-{i}",
                )

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        recs = _read_records(tmp_predictions_file)
        assert len(recs) == N_THREADS * N_PER_THREAD
        assert {r["ticker"] for r in recs} == {
            f"T{t}-{i}" for t in range(N_THREADS) for i in range(N_PER_THREAD)
        }

    def test_creates_state_dir_if_missing(self, tmp_path, monkeypatch):
        nested = tmp_path / "deep" / "nest" / "state"
        f = nested / "predictions.jsonl"
        monkeypatch.setattr(calibration, "PREDICTIONS_FILE", f)
        monkeypatch.setattr("bot.calibration.BOT_STATE_DIR", nested)

        calibration.record_prediction(
            ticker="X", opp_type="vig_stack_series",
            predicted_fair_cents=80.0, market_price_cents=70,
            scan_id="s1",
        )
        assert f.exists()
        assert len(_read_records(f)) == 1


class TestNeverRaises:
    """The trade path must never blow up because of calibration-log failure."""

    def test_disk_failure_is_swallowed(self, tmp_predictions_file):
        with patch("bot.calibration.open", side_effect=OSError("disk full")):
            calibration.record_prediction(
                ticker="X", opp_type="vig_stack_series",
                predicted_fair_cents=80.0, market_price_cents=70,
                scan_id="s1",
            )
        assert _read_records(tmp_predictions_file) == []

    def test_update_disk_failure_is_swallowed(self, tmp_predictions_file):
        # Seed a record so the update path actually runs
        calibration.record_prediction(
            ticker="X", opp_type="vig_stack_series",
            predicted_fair_cents=80.0, market_price_cents=70,
            scan_id="s1",
            recorded_at="2026-04-25T12:00:00+00:00",
        )
        with patch("bot.calibration.open", side_effect=OSError("disk full")):
            n = calibration.update_prediction_close(
                ticker="X",
                recorded_at="2026-04-25T12:00:00+00:00",
                closing_yes_price=85.0,
            )
        assert n == 0


class TestIdempotency:

    def test_same_scan_and_ticker_writes_once(self, tmp_predictions_file):
        for _ in range(3):
            calibration.record_prediction(
                ticker="T1", opp_type="vig_stack_series",
                predicted_fair_cents=80.0, market_price_cents=70,
                scan_id="scan-A",
            )
        assert len(_read_records(tmp_predictions_file)) == 1

    def test_different_ticker_same_scan_writes_both(self, tmp_predictions_file):
        for ticker in ("T1", "T2", "T3"):
            calibration.record_prediction(
                ticker=ticker, opp_type="vig_stack_series",
                predicted_fair_cents=80.0, market_price_cents=70,
                scan_id="scan-A",
            )
        assert len(_read_records(tmp_predictions_file)) == 3

    def test_different_scan_same_ticker_writes_both(self, tmp_predictions_file):
        for scan in ("scan-A", "scan-B", "scan-C"):
            calibration.record_prediction(
                ticker="T1", opp_type="vig_stack_series",
                predicted_fair_cents=80.0, market_price_cents=70,
                scan_id=scan,
            )
        assert len(_read_records(tmp_predictions_file)) == 3


class TestSettlementMatching:
    """update_prediction_close fills closing_yes_price within ±60s window."""

    def _seed(self, file: Path, ticker: str, ts: datetime, scan_id: str = "s") -> None:
        calibration.record_prediction(
            ticker=ticker, opp_type="vig_stack_series",
            predicted_fair_cents=80.0, market_price_cents=70,
            scan_id=scan_id,
            recorded_at=ts.isoformat(),
        )

    def test_exact_match_fills_close(self, tmp_predictions_file):
        ts = datetime(2026, 4, 25, 12, 0, 0, tzinfo=timezone.utc)
        self._seed(tmp_predictions_file, "T1", ts)

        n = calibration.update_prediction_close(
            ticker="T1",
            recorded_at=ts.isoformat(),
            closing_yes_price=85.0,
        )

        assert n == 1
        recs = _read_records(tmp_predictions_file)
        assert recs[0]["closing_yes_price"] == 85.0

    def test_within_window_fills(self, tmp_predictions_file):
        anchor = datetime(2026, 4, 25, 12, 0, 0, tzinfo=timezone.utc)
        # Three predictions: at anchor, +30s, +90s. Only first two should fill.
        self._seed(tmp_predictions_file, "T1", anchor, scan_id="s1")
        self._seed(tmp_predictions_file, "T1", anchor + timedelta(seconds=30), scan_id="s2")
        self._seed(tmp_predictions_file, "T1", anchor + timedelta(seconds=90), scan_id="s3")

        n = calibration.update_prediction_close(
            ticker="T1",
            recorded_at=anchor.isoformat(),
            closing_yes_price=85.0,
        )

        assert n == 2
        recs = sorted(_read_records(tmp_predictions_file), key=lambda r: r["scan_id"])
        assert recs[0]["closing_yes_price"] == 85.0  # s1 (anchor)
        assert recs[1]["closing_yes_price"] == 85.0  # s2 (+30s)
        assert recs[2]["closing_yes_price"] is None  # s3 (+90s) outside window

    def test_negative_window_also_fills(self, tmp_predictions_file):
        """The ±60s window is symmetric — predictions BEFORE the anchor also match."""
        anchor = datetime(2026, 4, 25, 12, 0, 0, tzinfo=timezone.utc)
        self._seed(tmp_predictions_file, "T1", anchor - timedelta(seconds=45), scan_id="s1")
        self._seed(tmp_predictions_file, "T1", anchor - timedelta(seconds=75), scan_id="s2")

        n = calibration.update_prediction_close(
            ticker="T1",
            recorded_at=anchor.isoformat(),
            closing_yes_price=85.0,
        )

        assert n == 1  # Only the -45s prediction is within window

    def test_different_ticker_does_not_fill(self, tmp_predictions_file):
        ts = datetime(2026, 4, 25, 12, 0, 0, tzinfo=timezone.utc)
        self._seed(tmp_predictions_file, "T1", ts)
        self._seed(tmp_predictions_file, "T2", ts, scan_id="other")

        n = calibration.update_prediction_close(
            ticker="T1",
            recorded_at=ts.isoformat(),
            closing_yes_price=85.0,
        )

        assert n == 1
        recs = sorted(_read_records(tmp_predictions_file), key=lambda r: r["ticker"])
        assert recs[0]["closing_yes_price"] == 85.0  # T1
        assert recs[1]["closing_yes_price"] is None  # T2

    def test_missing_ticker_returns_zero(self, tmp_predictions_file):
        ts = datetime(2026, 4, 25, 12, 0, 0, tzinfo=timezone.utc)
        self._seed(tmp_predictions_file, "T1", ts)

        n = calibration.update_prediction_close(
            ticker="UNKNOWN",
            recorded_at=ts.isoformat(),
            closing_yes_price=85.0,
        )

        assert n == 0

    def test_missing_file_returns_zero(self, tmp_predictions_file):
        # File does not exist
        assert not tmp_predictions_file.exists()
        n = calibration.update_prediction_close(
            ticker="T1",
            recorded_at="2026-04-25T12:00:00+00:00",
            closing_yes_price=85.0,
        )
        assert n == 0

    def test_already_filled_skipped(self, tmp_predictions_file):
        """Re-running settlement should not double-write or change values."""
        ts = datetime(2026, 4, 25, 12, 0, 0, tzinfo=timezone.utc)
        self._seed(tmp_predictions_file, "T1", ts)

        # First call fills
        n1 = calibration.update_prediction_close(
            ticker="T1", recorded_at=ts.isoformat(), closing_yes_price=85.0,
        )
        # Second call: row already has closing_yes_price set, so no-op
        n2 = calibration.update_prediction_close(
            ticker="T1", recorded_at=ts.isoformat(), closing_yes_price=99.0,
        )

        assert n1 == 1
        assert n2 == 0
        recs = _read_records(tmp_predictions_file)
        assert recs[0]["closing_yes_price"] == 85.0  # First write wins

    def test_bad_recorded_at_returns_zero(self, tmp_predictions_file):
        ts = datetime(2026, 4, 25, 12, 0, 0, tzinfo=timezone.utc)
        self._seed(tmp_predictions_file, "T1", ts)

        n = calibration.update_prediction_close(
            ticker="T1", recorded_at="not-an-iso-date", closing_yes_price=85.0,
        )

        assert n == 0


class TestBrierMath:
    """Handcrafted Brier-score check matching the calibration_report module."""

    def test_handcraft_5_records(self):
        # Importing tools.calibration_report — relies on tools/ being on path
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        import calibration_report

        # Predictions + outcomes:
        #   80¢ → YES (1) → (0.80 - 1)^2 = 0.04
        #   80¢ → YES (1) → (0.80 - 1)^2 = 0.04
        #   50¢ → NO  (0) → (0.50 - 0)^2 = 0.25
        #   20¢ → NO  (0) → (0.20 - 0)^2 = 0.04
        #   20¢ → NO  (0) → (0.20 - 0)^2 = 0.04
        # Sum = 0.41, mean = 0.082
        preds = [(80.0, 1), (80.0, 1), (50.0, 0), (20.0, 0), (20.0, 0)]
        assert calibration_report.brier_score(preds) == pytest.approx(0.082, rel=1e-3)

    def test_perfect_predictions_score_zero(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        import calibration_report

        preds = [(100.0, 1), (100.0, 1), (0.0, 0), (0.0, 0)]
        assert calibration_report.brier_score(preds) == pytest.approx(0.0)

    def test_empty_returns_nan(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        import calibration_report

        import math
        assert math.isnan(calibration_report.brier_score([]))


class TestBucketing:
    """The bucket boundaries must be [lo, hi) — inclusive low, exclusive high.
    Top bucket [90,101) is inclusive of 100 (settlement YES = 100¢)."""

    def test_bucket_lower_edge(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        import calibration_report

        assert calibration_report.bucket_of(80.0) == (80, 90)
        assert calibration_report.bucket_of(89.99) == (80, 90)
        assert calibration_report.bucket_of(90.0) == (90, 101)
        assert calibration_report.bucket_of(100.0) == (90, 101)
        assert calibration_report.bucket_of(0.0) == (0, 10)
