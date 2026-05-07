"""Behavior-preservation golden-file test for VigStackSeries.

Locks the contract: VigStackSeries produces output identical to what
the legacy scan_vig_stack_series produced on the same market input, to
within float-epsilon (1e-6).

Session 13a built up the test in two phases:
  1. While the legacy function still existed, the test ran BOTH code
     paths and compared them directly. Fixtures captured the legacy
     outputs as JSON.
  2. After the legacy function was deleted, the test compares the new
     code against the FROZEN fixtures. The fixtures ARE the legacy
     output — captured at the moment Phase 1's parity was locked.

If a fixture needs to be regenerated (e.g. constants change in a way
that's intentional), regenerate by running:

    python3 tools/regenerate_vig_stack_fixtures.py

(That tool doesn't exist yet; if you need it, look at the build code
in tests/fixtures/vig_stack_series/*.json and write the regenerator.)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from bot.strategies import Market
from bot.strategies.vig_stack_series import VigStackSeries

FIXTURES = Path(__file__).parent / "fixtures" / "vig_stack_series"


# ---------------------------------------------------------------------------
# Fixture builders — Kalshi-shaped market dicts. Each "scenario" returns
# a flat list of dicts that scan_vig_stack_series would receive via
# get_markets, and that get_buffered_markets would return as Market
# objects.
# ---------------------------------------------------------------------------

def _market(*, ticker: str, series_ticker: str, yes_ask: int, no_ask: int,
            volume: int = 200, open_interest: int = 50,
            close_time: str = "2026-12-31T23:59:00Z",
            title: str | None = None) -> dict:
    return {
        "ticker": ticker,
        "series_ticker": series_ticker,
        "title": title or f"{ticker} test",
        "event_ticker": f"{series_ticker}-EV",
        "status": "active",
        "yes_ask": yes_ask,
        "yes_bid": max(yes_ask - 1, 1),
        "no_ask": no_ask,
        "no_bid": max(no_ask - 1, 1),
        "volume": volume,
        "volume_24h": volume,
        "open_interest": open_interest,
        "close_time": close_time,
        "expiration_time": close_time,
    }


def scenario_stable_with_edge() -> list[dict]:
    """KXHIGHMIA ladder, stable family. Vig sums to ~135¢ — plenty.
    no_ask=75 -> no_ask_prob=0.75 >= 0.70 stable floor. Expect 5 ACCEPTs."""
    series = "KXHIGHMIA"
    rungs = [
        ("KXHIGHMIA-26APR26-T70", 25, 75),
        ("KXHIGHMIA-26APR26-B71.5", 30, 70),
        ("KXHIGHMIA-26APR26-B73.5", 25, 75),
        ("KXHIGHMIA-26APR26-B75.5", 25, 75),
        ("KXHIGHMIA-26APR26-T78", 30, 70),
    ]
    return [_market(ticker=t, series_ticker=series, yes_ask=ya, no_ask=na)
            for t, ya, na in rungs]


def scenario_volatile_below_floor() -> list[dict]:
    """KXHIGHDEN ladder (volatile family). All NO contracts below 0.93
    weather floor. Expect REJECT(non_stable_below_weather_floor) per rung."""
    series = "KXHIGHDEN"
    # Vig sum=130 (1.30 factor). NO=0.85, fair=100 - 30/1.3 = 76.9, edge=0.078
    rungs = [
        ("KXHIGHDEN-26APR26-T70", 25, 85),
        ("KXHIGHDEN-26APR26-B72.5", 30, 80),
        ("KXHIGHDEN-26APR26-B74.5", 25, 85),
        ("KXHIGHDEN-26APR26-B76.5", 25, 85),
        ("KXHIGHDEN-26APR26-T78", 25, 85),
    ]
    return [_market(ticker=t, series_ticker=series, yes_ask=ya, no_ask=na)
            for t, ya, na in rungs]


def scenario_no_vig() -> list[dict]:
    """KXHIGHCHI ladder where yes_sum_prob < 1.05. Expect REJECT(no_vig)
    fired once for the whole ladder — no per-rung opportunity."""
    series = "KXHIGHCHI"
    # yes_asks sum to 100 cents -> yes_sum_prob=1.00, below 1.05 threshold
    rungs = [
        ("KXHIGHCHI-26APR26-T60", 20, 80),
        ("KXHIGHCHI-26APR26-B62.5", 20, 80),
        ("KXHIGHCHI-26APR26-B64.5", 20, 80),
        ("KXHIGHCHI-26APR26-B66.5", 20, 80),
        ("KXHIGHCHI-26APR26-T68", 20, 80),
    ]
    return [_market(ticker=t, series_ticker=series, yes_ask=ya, no_ask=na)
            for t, ya, na in rungs]


def scenario_mixed_edge() -> list[dict]:
    """KXHIGHAUS (stable) ladder. Vig=130. Mix of accept and below-edge
    rungs. Some no_ask high enough to pass edge_threshold, some not."""
    series = "KXHIGHAUS"
    # vig_factor=1.30. yes_fair=ya/1.30; no_fair=100 - ya/1.30
    # Rung A (ya=20, na=80): yes_fair=15.38, no_fair=84.61, edge=4.61, rel=0.0577 -> ACCEPT
    # Rung B (ya=40, na=60): yes_fair=30.77, no_fair=69.23, edge=9.23, rel=0.1538 -> ACCEPT
    # Rung C (ya=50, na=51): yes_fair=38.46, no_fair=61.54, edge=10.54, rel=0.207 -> ACCEPT (na>=70 floor for stable)
    #   Wait — Rung C: na=51 -> no_ask_prob=0.51 < VIG_STACK_MIN_NO_ENTRY_PRICE=0.70 -> REJECT(no_price_below_floor)
    # Rung D (ya=10, na=91): yes_fair=7.69, no_fair=92.31, edge=1.31, rel=0.0144 -> REJECT(edge_below_threshold)
    # Rung E (ya=10, na=89): yes_fair=7.69, no_fair=92.31, edge=3.31, rel=0.0372 -> ACCEPT
    rungs = [
        ("KXHIGHAUS-26APR26-T70", 20, 80),
        ("KXHIGHAUS-26APR26-B72.5", 40, 60),
        ("KXHIGHAUS-26APR26-B74.5", 50, 51),
        ("KXHIGHAUS-26APR26-B76.5", 10, 91),
        ("KXHIGHAUS-26APR26-T78", 10, 89),
    ]
    return [_market(ticker=t, series_ticker=series, yes_ask=ya, no_ask=na)
            for t, ya, na in rungs]


def scenario_near_threshold() -> list[dict]:
    """KXINX index range ladder. Edge just below 0.02 threshold for one
    rung, just above for another. Tickers must contain '-B' for the
    index-only filter to keep them."""
    series = "KXINX"
    # vig_factor=1.06 (sum=106). To make edge tightly near threshold:
    #   na=98, no_ask_prob=0.98, ya=2, yes_fair=2/1.06=1.887, no_fair=98.113
    #   edge=98.113-98=0.113, rel=0.113/98=0.00115 -> REJECT(edge_below_threshold)
    # But wait: na=98 -> 0.98 below 0.93 floor? 0.98>=0.93 ✓
    # Actually KXINX is in VIG_STACK_STABLE_FAMILIES so applies 0.70 floor. 0.98>=0.70 ✓
    # Need yes_sum=106. Use 4 rungs: ya = 2, 27, 27, 50 (sum=106)
    # Rung A (ya=2, na=98): rel=0.00116 -> REJECT(edge_below_threshold)
    # Rung B (ya=27, na=73): yes_fair=25.47, no_fair=74.53, edge=1.53, rel=0.0210 -> ACCEPT
    # Rung C (ya=27, na=73): same -> ACCEPT
    # Rung D (ya=50, na=50): yes_fair=47.17, no_fair=52.83, edge=2.83 — but na=50 < 0.70 floor -> REJECT(no_price_below_floor)
    rungs = [
        ("KXINX-26APR26-B5400.5", 2, 98),
        ("KXINX-26APR26-B5500.5", 27, 73),
        ("KXINX-26APR26-B5600.5", 27, 73),
        ("KXINX-26APR26-B5700.5", 50, 50),
    ]
    return [_market(ticker=t, series_ticker=series, yes_ask=ya, no_ask=na)
            for t, ya, na in rungs]


SCENARIOS = {
    "stable_with_edge": scenario_stable_with_edge,
    "volatile_below_floor": scenario_volatile_below_floor,
    "no_vig": scenario_no_vig,
    "mixed_edge": scenario_mixed_edge,
    "near_threshold": scenario_near_threshold,
}


# ---------------------------------------------------------------------------
# Driver helpers — run legacy and new code against the same fixture.
# ---------------------------------------------------------------------------

def _markets_to_market_objs(market_dicts: list[dict]) -> list[Market]:
    return [
        Market(
            ticker=m["ticker"],
            series_ticker=m["series_ticker"],
            event_ticker=m.get("event_ticker"),
            status=m.get("status", "active"),
            close_ts=m.get("close_time"),
            yes_ask=m.get("yes_ask"),
            yes_bid=m.get("yes_bid"),
            no_ask=m.get("no_ask"),
            no_bid=m.get("no_bid"),
            volume_24h=m.get("volume_24h"),
            open_interest=m.get("open_interest"),
            raw=dict(m),
        )
        for m in market_dicts
    ]


def _load_legacy_fixture(name: str) -> tuple[list[dict], list[dict], list[tuple[str, str, str]]]:
    """Load a frozen fixture captured at the time scan_vig_stack_series
    was deleted. Returns (input_market_dicts, expected_opps, expected_calls)."""
    payload = json.loads((FIXTURES / f"{name}.json").read_text())
    expected_calls = [tuple(c) for c in payload["expected_calls"]]
    return payload["input"], payload["expected_opps"], expected_calls


def _new_run(market_dicts: list[dict]) -> tuple[list[dict], list[tuple[str, str, str]]]:
    """Run VigStackSeries against the same data."""
    universe = _markets_to_market_objs(market_dicts)
    decision_calls: list[tuple[str, str, str]] = []
    def capture_decision(**kw):
        decision_calls.append((kw.get("ticker", ""), kw.get("decision", ""), kw.get("reason", "")))

    s = VigStackSeries()
    with patch("bot.strategies.vig_stack_series._fetch_vig_stack_forecasts", return_value={}), \
         patch("bot.decisions.log_decision", side_effect=capture_decision), \
         patch("bot.clv.record_counterfactual_skip"):
        candidates = s.candidate_markets(universe)
        opps = []
        for m in candidates:
            opp = s.evaluate(m)
            if opp is not None:
                opps.append(opp)
        s.finalize("test_scan")
    return opps, decision_calls


def _opp_signature(o: dict) -> tuple:
    """The 'identity' tuple the user wants compared:
    (ticker, side, edge, fair_value_cents, kalshi_price)."""
    return (
        o["ticker"],
        o["recommended_side"],
        round(o["edge"], 6),
        round(o["no_fair_cents"], 6),
        o["no_ask_cents"],
    )


def _assert_equivalent(legacy_opps: list[dict], new_opps: list[dict]) -> None:
    legacy_keys = {o["ticker"]: o for o in legacy_opps}
    new_keys = {o["ticker"]: o for o in new_opps}
    assert set(legacy_keys.keys()) == set(new_keys.keys()), (
        f"Different ticker set:\n  legacy={sorted(legacy_keys)}\n  new   ={sorted(new_keys)}"
    )

    for ticker in legacy_keys:
        l = legacy_keys[ticker]
        n = new_keys[ticker]
        # Float fields with 1e-6 epsilon
        for field in ("edge", "relative_edge", "vig_factor", "no_fair_cents"):
            assert abs(l[field] - n[field]) < 1e-6, (
                f"{ticker} {field}: legacy={l[field]} new={n[field]}"
            )
        # Exact integer / string fields
        for field in ("yes_sum_cents", "no_ask_cents", "type", "ticker", "recommended_side"):
            assert l[field] == n[field], (
                f"{ticker} {field}: legacy={l[field]!r} vs new={n[field]!r}"
            )
        # edge_result sub-dict
        for field in ("fair_value", "kalshi_price", "edge", "relative_edge"):
            assert abs(l["edge_result"][field] - n["edge_result"][field]) < 1e-6, (
                f"{ticker} edge_result.{field}: legacy={l['edge_result'][field]} new={n['edge_result'][field]}"
            )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def _attribution_market(ticker: str, series_ticker: str) -> Market:
    return Market(
        ticker=ticker,
        series_ticker=series_ticker,
        event_ticker=None,
        status="active",
        close_ts=None,
        yes_ask=50,
        yes_bid=49,
        no_ask=50,
        no_bid=49,
        volume_24h=100,
        open_interest=100,
        raw={},
    )


@pytest.mark.parametrize(
    "ticker,series_ticker",
    [
        ("KXMLBGAME-26MAY082210ATLLAD-LAD", "KXMLBGAME"),
        ("KXNBAGAME-26MAY08NYKPHI-PHI", "KXNBAGAME"),
        ("KXNHLGAME-26MAY08VGKANA-VGK", "KXNHLGAME"),
    ],
)
def test_classifier_per_game_prefixes_emit_vig_stack_series(
    ticker: str,
    series_ticker: str,
) -> None:
    """Session 63: per-game KX*GAME tickers are series-shaped."""
    market = _attribution_market(ticker, series_ticker)
    with patch("bot.strategies.vig_stack_series.SPORTS_FUTURES_TICKERS", [series_ticker]):
        assert VigStackSeries().name_for(market) == "vig_stack_series"


@pytest.mark.parametrize(
    "ticker,series_ticker",
    [
        ("KXMLB-26-LAD", "KXMLB"),
        ("KXNBA-26-OKC", "KXNBA"),
        ("KXNHL-26-VGK", "KXNHL"),
    ],
)
def test_classifier_long_dated_futures_emit_vig_stack_futures(
    ticker: str,
    series_ticker: str,
) -> None:
    """True championship futures keep the vig_stack_futures label."""
    market = _attribution_market(ticker, series_ticker)
    assert VigStackSeries().name_for(market) == "vig_stack_futures"


@pytest.mark.parametrize("scenario_name", list(SCENARIOS.keys()))
def test_byte_identical_opportunities(scenario_name: str) -> None:
    market_dicts, expected_opps, _ = _load_legacy_fixture(scenario_name)
    new_opps, _ = _new_run(market_dicts)
    _assert_equivalent(expected_opps, new_opps)


@pytest.mark.parametrize("scenario_name", list(SCENARIOS.keys()))
def test_decision_log_calls_match(scenario_name: str) -> None:
    market_dicts, _, expected_calls = _load_legacy_fixture(scenario_name)
    _, new_calls = _new_run(market_dicts)
    assert sorted(expected_calls) == sorted(new_calls), (
        f"\nExpected-only: {sorted(set(expected_calls) - set(new_calls))}\n"
        f"New-only:      {sorted(set(new_calls) - set(expected_calls))}"
    )


def test_stable_with_edge_produces_accepts() -> None:
    """Sanity check: the frozen stable_with_edge fixture actually has opps.
    If this fails, the fixture has decayed and needs to be regenerated."""
    _, expected_opps, _ = _load_legacy_fixture("stable_with_edge")
    assert len(expected_opps) > 0, "stable_with_edge fixture produced no accepts"


def test_no_vig_emits_single_decision() -> None:
    """no_vig gate logs ONCE per ladder — not per-rung. Regression
    guard against the obvious refactor mistake where evaluate emits
    per-market."""
    market_dicts, _, expected_calls = _load_legacy_fixture("no_vig")
    _, new_calls = _new_run(market_dicts)
    expected_no_vig = [c for c in expected_calls if c[2] == "no_vig"]
    new_no_vig = [c for c in new_calls if c[2] == "no_vig"]
    assert len(expected_no_vig) == len(new_no_vig) == 1, (
        f"no_vig: expected={expected_no_vig} new={new_no_vig}"
    )


# ---------------------------------------------------------------------------
# Session 15.5 — close_ts threading through every log_decision in vig_stack
# ---------------------------------------------------------------------------

def _run_capturing_extras(market_dicts: list[dict]) -> list[dict]:
    """Same as _new_run but captures the full kwargs (including extra) per
    log_decision call. Returns a list of (ticker, reason, extra) tuples."""
    universe = _markets_to_market_objs(market_dicts)
    captured: list[dict] = []

    def capture(**kw):
        captured.append({
            "ticker": kw.get("ticker", ""),
            "reason": kw.get("reason", ""),
            "decision": kw.get("decision", ""),
            "extra": kw.get("extra"),
        })

    s = VigStackSeries()
    with patch("bot.strategies.vig_stack_series._fetch_vig_stack_forecasts", return_value={}), \
         patch("bot.decisions.log_decision", side_effect=capture), \
         patch("bot.clv.record_counterfactual_skip"):
        candidates = s.candidate_markets(universe)
        for m in candidates:
            s.evaluate(m)
        s.finalize("test_scan")
    return captured


@pytest.mark.parametrize("scenario_name", list(SCENARIOS.keys()))
def test_every_decision_extra_carries_close_ts(scenario_name: str) -> None:
    """Session 15.5: every log_decision call from vig_stack_series must include
    market.close_ts in extra so bot.regime.tag can populate event_horizon_hr."""
    market_dicts, _, _ = _load_legacy_fixture(scenario_name)
    # The fixtures all use the same close_time (2026-12-31T23:59:00Z).
    expected_close = market_dicts[0]["close_time"]

    captured = _run_capturing_extras(market_dicts)
    assert captured, f"scenario {scenario_name} produced no log_decision calls"

    missing = [c for c in captured
               if not (c["extra"] and c["extra"].get("close_ts") == expected_close)]
    assert not missing, (
        f"scenario {scenario_name}: log_decision call(s) missing close_ts in extra:\n"
        + "\n".join(f"  ticker={c['ticker']} reason={c['reason']} extra={c['extra']}"
                    for c in missing)
    )
