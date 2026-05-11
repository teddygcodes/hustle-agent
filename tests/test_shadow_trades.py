import json
from datetime import datetime, timezone
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_shadow_trade_executor_row_available_sizing(tmp_path, monkeypatch):
    import bot.shadow_trades as shadow

    f = tmp_path / "shadow_trades.jsonl"
    monkeypatch.setattr(shadow, "BOT_STATE_DIR", tmp_path)
    monkeypatch.setattr(shadow, "SHADOW_TRADES_FILE", f)

    row = shadow.record_blocked_trade(
        ticker="KXHIGHCHI-26MAY10-B65.5",
        opp_type="vig_stack_series",
        blocked_reason="family_disabled_reject",
        source="executor",
        would_side="no",
        would_entry_price_cents=93,
        would_contracts=161,
        family="KXHIGHCHI",
        close_ts="2026-05-11T05:59:00Z",
        extra={"family": "KXHIGHCHI", "contracts": 161, "price_cents": 93},
        ts=datetime(2026, 5, 10, 19, 1, tzinfo=timezone.utc),
    )

    rec = json.loads(f.read_text().splitlines()[0])
    assert row is not None
    assert rec["id"].startswith("SHADOW-")
    assert rec["blocked_reason"] == "family_disabled_reject"
    assert rec["would_entry_price"] == 0.93
    assert rec["would_contracts"] == 161
    assert rec["would_notional"] == 149.73
    assert rec["sizing_status"] == "available"
    assert rec["status"] == "open"
    assert rec["settled_at"] is None
    assert rec["market_result"] is None
    assert rec["would_pnl"] is None


def test_shadow_trade_live_watcher_row_unavailable_sizing(tmp_path, monkeypatch):
    import bot.shadow_trades as shadow

    f = tmp_path / "shadow_trades.jsonl"
    monkeypatch.setattr(shadow, "BOT_STATE_DIR", tmp_path)
    monkeypatch.setattr(shadow, "SHADOW_TRADES_FILE", f)

    shadow.record_blocked_trade(
        ticker="KXWTAMATCH-26MAY10SIEPLI-PLI",
        opp_type="live_momentum",
        blocked_reason="sport_disabled",
        source="live_watcher",
        would_side="yes",
        would_entry_price_cents=92,
        would_contracts=None,
        sport="wta",
        extra={"sport": "wta", "kalshi_price": 92},
        ts=datetime(2026, 5, 10, 19, 3, tzinfo=timezone.utc),
    )

    rec = json.loads(f.read_text().splitlines()[0])
    assert rec["would_entry_price"] == 0.92
    assert rec["would_contracts"] is None
    assert rec["would_notional"] is None
    assert rec["sizing_status"] == "unavailable"
    assert rec["sport"] == "wta"
