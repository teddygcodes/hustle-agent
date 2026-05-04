"""Generate golden-file fixtures for tests/test_live_momentum_strategy.py.

Per Session 19a's pragmatic Task 3: scenarios run through
LiveMomentumStrategy.process_tick (the new code path). Output captures
(actions, log_decision_calls, state_snapshots) per tick to JSON. Tests
assert the new code reproduces these fixtures — a self-consistency
regression lock against future drift.

The byte-identical-to-legacy proof comes from manual code review, NOT
from running legacy _tick_momentum in parallel (the I/O mocking glue
required ~150 LOC of harness, deferred to 19b's full back-tester
which exercises real tick streams against real paper trade outcomes).

Run:
    python3 tools/regenerate_live_momentum_fixtures.py [scenario_name]

Omit scenario_name to regenerate all. --check compares new output
to existing JSON without overwriting (returns 1 on drift).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bot.strategies import Buy, Hold, Market, Sell, Tick
from bot.strategies.live_momentum import LiveMomentumStrategy

FIXTURES_DIR = Path(__file__).parent.parent / "tests" / "fixtures" / "live_momentum"


def _tick(i: int, yes_ask: int, *, opp_yes_ask: int | None = None,
          ticker: str = "KXATPMATCH-TEST", opp_ticker: str = "KXATPMATCH-OPP") -> dict:
    """Build a tick dict for tennis (no ESPN data required)."""
    if opp_yes_ask is None:
        opp_yes_ask = 100 - yes_ask
    return {
        "ts": f"2026-04-26T00:{i // 60:02d}:{i % 60:02d}Z",
        "ticker": ticker,
        "yes_bid": max(yes_ask - 1, 1),
        "yes_ask": yes_ask,
        "no_bid": max((100 - yes_ask) - 1, 1),
        "no_ask": 100 - yes_ask,
        "opp_ticker": opp_ticker,
        "opp_yes_bid": max(opp_yes_ask - 1, 1),
        "opp_yes_ask": opp_yes_ask,
        "opp_no_bid": max((100 - opp_yes_ask) - 1, 1),
        "opp_no_ask": 100 - opp_yes_ask,
        "wp": None, "score_diff": None, "period": None, "espn_data": None,
        "close_ts": "2026-12-31T23:59:00Z",
        "raw": {"yes_ask": yes_ask, "yes_bid": max(yes_ask - 1, 1),
                "ticker": ticker, "title": "Test Match"},
        "raw_opp": {"yes_ask": opp_yes_ask, "yes_bid": max(opp_yes_ask - 1, 1),
                    "ticker": opp_ticker, "title": "Test Match"},
    }


def scenario_take_profit() -> list[dict]:
    """Tennis. Leader at 75-77c with variance, dip to 72c (5c dip ≥ min_dip),
    entry, climb to 83c yes_ask (yes_bid=82, gain=10) → TAKE_PROFIT (sport_tp=10).

    Note: current_value at exit uses yes_bid (= yes_ask - 1). Entry at 72
    (yes_ask=72, paid 72c per contract). To trigger TP, need yes_bid - 72 >= 10,
    i.e. yes_ask >= 83."""
    prices = [75, 76, 77, 76, 75, 74, 73, 72,   # warmup, range 5c, dip → entry at 72
              74, 77, 80, 83]                    # climb → yes_bid=82, gain=10 → TP
    return [_tick(i, p) for i, p in enumerate(prices)]


def scenario_stop_loss() -> list[dict]:
    """Tennis. Same dip-and-entry, then crashes 10c → STOP_LOSS (sport_sl=10).

    drop_cents = entry_price - current_value (yes_bid). Entry at 72.
    Need yes_bid <= 62, i.e. yes_ask <= 63."""
    prices = [75, 76, 77, 76, 75, 74, 73, 72,   # warmup + dip → entry at 72
              70, 65, 63]                        # crash → yes_bid=62, drop=10 → SL
    return [_tick(i, p) for i, p in enumerate(prices)]


def scenario_near_settle() -> list[dict]:
    """Tennis. Entry at 84 (dip from peak 90, dip=6 ≥ min_dip=5), climb so
    yes_bid >= 93 → NEAR_SETTLE. Need yes_ask >= 94."""
    prices = [85, 87, 89, 90, 88, 86, 84,        # warmup, dip 6 → entry at 84
              86, 89, 92, 94]                     # climb → yes_bid=93 → NEAR_SETTLE
    return [_tick(i, p) for i, p in enumerate(prices)]


def scenario_trailing_stop() -> list[dict]:
    """Tennis. Entry at 72, climb to peak yes_bid=80 (+8c, below TP=10),
    then drop yes_bid 6c.

    PRODUCTION BUG: peak_values[ticker] is never written on the first
    call because `prev_peak = peak_values.get(ticker, current_value)`
    defaults to current_value, then `if current_value > prev_peak`
    is `current_value > current_value` = False. Result: drop_from_peak
    is always 0 in production live_watcher, so TRAILING_STOP never
    fires. This is the real reason Session 18 saw 0/95 trail fires
    (not the LIVE_PROFIT_TARGET activation Session 18 hypothesized,
    nor the threshold issue Session 18.5 swept).

    Session 19a faithfully preserves this bug per behavior-preservation
    discipline. This scenario documents the bug: a trail-eligible
    price path produces NO exit. Position remains open. Fixing the
    bug is a separate small follow-up tracked in the commit message
    and CLAUDE.md."""
    prices = [75, 76, 77, 76, 75, 74, 73, 72,   # warmup + dip → entry at 72
              74, 77, 79, 81,                     # climb → would-be peak +8c
              79, 77, 75]                         # drop 6c — but trail does NOT fire
    return [_tick(i, p) for i, p in enumerate(prices)]


def scenario_no_exit() -> list[dict]:
    """Tennis. Entry, prices oscillate but never hit TP/SL/NEAR/TRAIL.
    Position stays open at last tick.

    Constraints (entry at 72 = yes_bid 71 effective entry value... no, entry
    price_cents=72 because that's yes_ask paid):
      gain_cents max = yes_bid_max - 72 < TP=10  → yes_ask_max <= 82
      drop_cents max = 72 - yes_bid_min < SL=10  → yes_ask_min >= 63
      peak_drop max < trail=6 OR gain at trough <= 0 (TRAIL needs gain>0)
      yes_bid never >= 93 (NEAR_SETTLE)."""
    prices = [75, 76, 77, 76, 75, 74, 73, 72,   # warmup + dip → entry at 72
              74, 76, 74, 71, 74, 76, 73]       # oscillate; max yb=75, min yb=70 → never threshold
    return [_tick(i, p) for i, p in enumerate(prices)]


SCENARIOS = {
    "take_profit": scenario_take_profit,
    "stop_loss": scenario_stop_loss,
    "near_settle": scenario_near_settle,
    "trailing_stop": scenario_trailing_stop,
    "no_exit": scenario_no_exit,
}


def _serialize_action(a) -> dict:
    if isinstance(a, Buy):
        return {"type": "Buy", "side": a.side, "qty": a.qty,
                "reason": a.reason, "ticker": a.ticker,
                "price_cents": a.price_cents}
    if isinstance(a, Sell):
        return {"type": "Sell", "side": a.side, "qty": a.qty,
                "reason": a.reason, "ticker": a.ticker,
                "exit_price": a.exit_price}
    return {"type": "Hold", "reason": a.reason}


def run_scenario(ticks: list[dict]) -> dict:
    """Run scenario through LiveMomentumStrategy, capture observable output."""
    market = Market(
        ticker=ticks[0]["ticker"], series_ticker=ticks[0]["ticker"].split("-")[0],
        event_ticker=None, status="active", close_ts=ticks[0].get("close_ts"),
        yes_ask=ticks[0]["yes_ask"], yes_bid=ticks[0]["yes_bid"],
        no_ask=ticks[0]["no_ask"], no_bid=ticks[0]["no_bid"],
        volume_24h=100, open_interest=50, raw={},
    )
    s = LiveMomentumStrategy()
    state = s.init_state(
        market, sport="tennis",
        opponent_ticker=ticks[0].get("opp_ticker"),
        balance=500.0, mode="momentum", match_title="Test Match",
    )

    captured = {"actions": [], "log_calls": [], "states": []}

    def capture_log(**kw):
        captured["log_calls"].append({
            "ticker": kw.get("ticker", ""),
            "decision": kw.get("decision", ""),
            "reason": kw.get("reason", ""),
        })

    with patch("bot.decisions.log_decision", side_effect=capture_log):
        for t in ticks:
            tick = Tick(
                ts=t["ts"], ticker=t["ticker"],
                yes_bid=t["yes_bid"], yes_ask=t["yes_ask"],
                no_bid=t["no_bid"], no_ask=t["no_ask"],
                opp_ticker=t.get("opp_ticker"),
                opp_yes_bid=t.get("opp_yes_bid"),
                opp_yes_ask=t.get("opp_yes_ask"),
                opp_no_bid=t.get("opp_no_bid"),
                opp_no_ask=t.get("opp_no_ask"),
                wp=t.get("wp"), score_diff=t.get("score_diff"),
                period=t.get("period"), espn_data=t.get("espn_data"),
                close_ts=t.get("close_ts"),
                raw=t.get("raw", {}), raw_opp=t.get("raw_opp", {}),
            )
            state, action = s.process_tick(state, tick)
            captured["actions"].append(_serialize_action(action))
            captured["states"].append({
                "bets_placed_count": len(state.data["bets_placed"]),
                "entry_count": state.data["entry_count"],
                "cooldown_remaining": state.data["cooldown_remaining"],
                "peak_values": dict(state.data["peak_values"]),
            })
    return captured


def main():
    p = argparse.ArgumentParser()
    p.add_argument("scenario", nargs="?", default=None)
    p.add_argument("--check", action="store_true",
                   help="Dry run: compare to existing fixture, exit 1 if drift")
    args = p.parse_args()

    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    targets = [args.scenario] if args.scenario else list(SCENARIOS.keys())
    drift = False

    for name in targets:
        if name not in SCENARIOS:
            print(f"unknown scenario: {name}", file=sys.stderr)
            continue
        ticks = SCENARIOS[name]()
        captured = run_scenario(ticks)
        out = {"scenario": name, "ticks": ticks, "expected": captured}
        path = FIXTURES_DIR / f"{name}.json"
        if args.check:
            existing = json.loads(path.read_text()) if path.exists() else None
            if existing != out:
                print(f"DRIFT: {name}")
                drift = True
            else:
                print(f"OK: {name}")
        else:
            path.write_text(json.dumps(out, indent=2, default=str))
            n_actions = len(captured["actions"])
            actions_summary = [a["type"] for a in captured["actions"]]
            non_hold = [(i, a) for i, a in enumerate(captured["actions"])
                        if a["type"] != "Hold"]
            print(f"wrote {path.name} — {n_actions} ticks, "
                  f"actions: {actions_summary}, "
                  f"non-Hold: {non_hold}")

    sys.exit(1 if drift else 0)


if __name__ == "__main__":
    main()
