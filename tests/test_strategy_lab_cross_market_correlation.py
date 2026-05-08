"""Unit tests for tools/strategy_lab/candidates/cross_market_correlation.py.

Note (Session 72): the schema-discipline test for the lab lives at
tests/test_strategy_lab.py::test_canonical_schema_used_throughout. It uses
``lab_dir.rglob("*.py")`` and recursively scans every .py under
tools/strategy_lab/, including the new candidate file. NO extension is
needed in this test file — the existing test auto-covers cross_market_correlation.py.
"""
from __future__ import annotations

import pytest

from tools.strategy_lab.candidate import CandidateOpportunity
from tools.strategy_lab.candidates import cross_market_correlation as cmc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row(
    ticker: str,
    *,
    yes_ask: int,
    volume_24h: int = 1000,
    scan_id: str = "20260507T120000",
    ts: str = "2026-05-07T12:00:00+00:00",
) -> dict:
    """Mint a minimal universe row dict shaped like bot/state/universe.jsonl."""
    return {
        "ticker": ticker,
        "yes_ask": yes_ask,
        "yes_bid": max(0, yes_ask - 2),
        "no_ask": 100 - max(0, yes_ask - 2),
        "no_bid": 100 - yes_ask,
        "volume_24h": volume_24h,
        "scan_id": scan_id,
        "ts": ts,
        "status": "active",
    }


# ---------------------------------------------------------------------------
# 1. Pair-key extraction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ticker, expected",
    [
        (
            "KXNBASERIES-26CLEDETR2-DET",
            ("series", "KXNBA", frozenset({"CLE", "DET"}), "DET"),
        ),
        (
            "KXNBASERIES-26CLEDETR2-CLE",
            ("series", "KXNBA", frozenset({"CLE", "DET"}), "CLE"),
        ),
        (
            "KXNBAGAME-26MAY11DETCLE-DET",
            ("game", "KXNBA", frozenset({"DET", "CLE"}), "DET"),
        ),
        (
            "KXNBAGAME-26MAY07CLEDET-CLE",
            ("game", "KXNBA", frozenset({"CLE", "DET"}), "CLE"),
        ),
        (
            "KXNHLSERIES-26MTLBUFR2-MTL",
            ("series", "KXNHL", frozenset({"MTL", "BUF"}), "MTL"),
        ),
        (
            "KXNHLGAME-26MAY08MTLBUF-BUF",
            ("game", "KXNHL", frozenset({"MTL", "BUF"}), "BUF"),
        ),
    ],
)
def test_parse_pair_key_known_tickers(ticker, expected):
    assert cmc._parse_pair_key(ticker) == expected


def test_parse_pair_key_returns_none_for_unsupported_sport():
    # KXMLB is not in _SPORTS — Phase 0 deferred MLB to v2 (regular-season noise).
    assert cmc._parse_pair_key("KXMLBGAME-26MAY08ATLLAD-ATL") is None
    assert cmc._parse_pair_key("KXMLB-26-OKC") is None


def test_parse_pair_key_returns_none_for_malformed():
    assert cmc._parse_pair_key("not-a-ticker") is None
    assert cmc._parse_pair_key("KXNBAGAME-tooshort-X") is None
    assert cmc._parse_pair_key("KXNBASERIES-26CLEDETR2") is None  # missing winner
    assert cmc._parse_pair_key("KXNBASERIES-26CLEDET-DET") is None  # missing 'R'


# ---------------------------------------------------------------------------
# 2. Series-side rows do NOT emit (state-update only)
# ---------------------------------------------------------------------------


def test_series_side_row_returns_none_and_updates_partner_index():
    strat = cmc.CrossMarketCorrelation()
    row = _row("KXNBASERIES-26CLEDETR2-CLE", yes_ask=35)
    out = strat.evaluate(row)
    assert out is None
    # Partner index updated:
    assert strat._series_ticker_by_pair[
        ("KXNBA", frozenset({"CLE", "DET"}), "CLE")
    ] == "KXNBASERIES-26CLEDETR2-CLE"


# ---------------------------------------------------------------------------
# 3. Game side WITHOUT series partner returns None
# ---------------------------------------------------------------------------


def test_game_without_partner_returns_none():
    strat = cmc.CrossMarketCorrelation()
    out = strat.evaluate(_row("KXNBAGAME-26MAY11DETCLE-CLE", yes_ask=56))
    assert out is None


# ---------------------------------------------------------------------------
# 4. Below-divergence threshold returns None
# ---------------------------------------------------------------------------


def test_below_divergence_threshold_returns_none():
    strat = cmc.CrossMarketCorrelation()
    # series 50, game 53 → Δ=3 < MIN_DIVERGENCE_CENTS (5)
    strat.evaluate(_row("KXNBASERIES-26CLEDETR2-CLE", yes_ask=50))
    out = strat.evaluate(_row("KXNBAGAME-26MAY11DETCLE-CLE", yes_ask=53))
    assert out is None


# ---------------------------------------------------------------------------
# 5. Liquidity floor on either leg blocks emit
# ---------------------------------------------------------------------------


def test_low_liquidity_partner_blocks_emit():
    strat = cmc.CrossMarketCorrelation()
    # Series leg has volume_24h below floor (50 < 100)
    strat.evaluate(_row("KXNBASERIES-26CLEDETR2-CLE", yes_ask=35, volume_24h=50))
    out = strat.evaluate(_row("KXNBAGAME-26MAY11DETCLE-CLE", yes_ask=56))
    assert out is None


def test_low_liquidity_game_blocks_emit():
    strat = cmc.CrossMarketCorrelation()
    strat.evaluate(_row("KXNBASERIES-26CLEDETR2-CLE", yes_ask=35))
    out = strat.evaluate(
        _row("KXNBAGAME-26MAY11DETCLE-CLE", yes_ask=56, volume_24h=50)
    )
    assert out is None


# ---------------------------------------------------------------------------
# 6. Basic emit on the "expensive game" branch (NO bet)
# ---------------------------------------------------------------------------


def test_emits_no_when_game_yes_higher_than_series():
    strat = cmc.CrossMarketCorrelation()
    strat.evaluate(_row("KXNBASERIES-26CLEDETR2-CLE", yes_ask=35))
    out = strat.evaluate(_row("KXNBAGAME-26MAY11DETCLE-CLE", yes_ask=56))
    assert out is not None
    assert isinstance(out, CandidateOpportunity)
    assert out.ticker == "KXNBAGAME-26MAY11DETCLE-CLE"
    assert out.side == "no"
    assert out.target_price_cents == pytest.approx(100.0 - 56.0)
    assert out.fair_value_cents == pytest.approx(100.0 - 35.0)
    assert out.edge_cents == pytest.approx(out.fair_value_cents - out.target_price_cents)
    assert out.extra is not None
    assert out.extra["partner_ticker"] == "KXNBASERIES-26CLEDETR2-CLE"
    assert out.extra["divergence_cents"] == pytest.approx(21.0)


# ---------------------------------------------------------------------------
# 7. Emit on the "cheap game" branch (YES bet)
# ---------------------------------------------------------------------------


def test_emits_yes_when_game_yes_lower_than_series():
    strat = cmc.CrossMarketCorrelation()
    # Series CLE at 65, game CLE at 44 → game-yes is LOWER → buy YES on game
    strat.evaluate(_row("KXNBASERIES-26CLEDETR2-CLE", yes_ask=65))
    out = strat.evaluate(_row("KXNBAGAME-26MAY11DETCLE-CLE", yes_ask=44))
    assert out is not None
    assert out.side == "yes"
    assert out.target_price_cents == pytest.approx(44.0)
    assert out.fair_value_cents == pytest.approx(65.0)
    assert out.edge_cents == pytest.approx(21.0)


# ---------------------------------------------------------------------------
# 8. Persistence threshold (MIN_PERSISTENCE_SCANS) gates first-seen emits
# ---------------------------------------------------------------------------


def test_persistence_threshold_blocks_first_scan_then_emits(monkeypatch):
    # Bump persistence to 2 — first scan should not emit, second scan should.
    monkeypatch.setattr(cmc, "MIN_PERSISTENCE_SCANS", 2)
    strat = cmc.CrossMarketCorrelation()

    # Scan 1: series + game both diverge.
    strat.evaluate(
        _row("KXNBASERIES-26CLEDETR2-CLE", yes_ask=35, scan_id="20260507T120000")
    )
    out1 = strat.evaluate(
        _row("KXNBAGAME-26MAY11DETCLE-CLE", yes_ask=56, scan_id="20260507T120000")
    )
    assert out1 is None  # only 1 scan_id observed; threshold is 2

    # Scan 2: same divergence on a NEW scan_id.
    strat.evaluate(
        _row("KXNBASERIES-26CLEDETR2-CLE", yes_ask=35, scan_id="20260507T122000")
    )
    out2 = strat.evaluate(
        _row("KXNBAGAME-26MAY11DETCLE-CLE", yes_ask=56, scan_id="20260507T122000")
    )
    assert out2 is not None
    assert out2.extra["n_scans_persisted"] == 2


# ---------------------------------------------------------------------------
# 9. Wrong-winner pair (CLE series ticker, DET game ticker) does NOT match
# ---------------------------------------------------------------------------


def test_different_winner_does_not_match_partner():
    strat = cmc.CrossMarketCorrelation()
    # Series CLE-side at 35¢ in state.
    strat.evaluate(_row("KXNBASERIES-26CLEDETR2-CLE", yes_ask=35))
    # Game DET-side arrives — different winner key, no partner found.
    out = strat.evaluate(_row("KXNBAGAME-26MAY11DETCLE-DET", yes_ask=56))
    assert out is None


# ---------------------------------------------------------------------------
# 10. Cross-team integrity: state for one matchup does not leak to another
# ---------------------------------------------------------------------------


def test_state_isolated_across_different_matchups():
    strat = cmc.CrossMarketCorrelation()
    # Set up CLE-DET series partner.
    strat.evaluate(_row("KXNBASERIES-26CLEDETR2-CLE", yes_ask=35))
    # PHI-NYK game arrives — different teams, should not match CLE-DET partner.
    out = strat.evaluate(_row("KXNBAGAME-26MAY10PHINYK-NYK", yes_ask=56))
    assert out is None


# ---------------------------------------------------------------------------
# 11. Required identifiers — missing scan_id / ticker returns None
# ---------------------------------------------------------------------------


def test_missing_scan_id_returns_none():
    strat = cmc.CrossMarketCorrelation()
    out = strat.evaluate(
        {"ticker": "KXNBAGAME-26MAY11DETCLE-CLE", "yes_ask": 56, "volume_24h": 1000}
    )
    assert out is None


def test_missing_ticker_returns_none():
    strat = cmc.CrossMarketCorrelation()
    out = strat.evaluate({"scan_id": "20260507T120000", "yes_ask": 56})
    assert out is None
