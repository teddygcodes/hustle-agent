# Strategy Lab

On-demand bridge between Session 48 discovery-agent findings (or any other source of strategy ideas) and production scanner code. Write a ~20-line candidate, run the driver, get a markdown report telling you what would have happened on historical universe + clv data — in seconds, not days.

**Read first:** [`CLAUDE.md` — "Canonical Data Schema Reference"](../../CLAUDE.md). The lab is read-only on `bot/state/` and depends on the canonical schema (`market_result ∈ {yes, no}`, `clv_cents`, `skipped_by_gate`, `status ∈ {settled, counterfactual_settled}`). Two schema mistakes in 24 hours surfaced May 1, 2026 (Session 43b plan-time fix + Session 45 verification error). If a field name or value enum looks reasonable but you're not sure, **STOP and check the schema reference**.

## How to write a candidate (5 steps)

1. Create `tools/strategy_lab/candidates/<your_idea>.py` (this directory is gitignored except for `__init__.py` and the reference example, so user candidates stay local).
2. Implement a class satisfying the `CandidateStrategy` Protocol from `tools.strategy_lab.candidate`. Required: a `name: str` attribute and an `evaluate(market: dict, context: dict | None) -> CandidateOpportunity | None` method.
3. Optionally set `clv_match_window_hours: float = 2.0` on the instance to widen/narrow the join window.
4. Expose a module-level `STRATEGY = YourClass()`.
5. Run: `python3 -m tools.strategy_lab.driver --candidate <your_idea> --days 14`.

The reference example is [`candidates/example_total_points_under.py`](candidates/example_total_points_under.py). It's intentionally a stub (no real edge) — copy it as scaffolding, then replace `evaluate()` with your real model.

### When to set pair_key

Stateful candidates (those that re-emit on every scan while a divergence or
condition persists) MUST set `pair_key` on each CandidateOpportunity. One-shot
candidates (one emit per real entry decision) can leave it None.

The lab uses pair_key for per-unique-outcome aggregation. Without it, stateful
candidates report inflated per-emit P&L — same hypothetical opportunity counted
35-122 times in Session 72's founding case, sign-flipped on dedup
(-$4.16 per-unique vs +$2,567 per-emit).

If you're not sure whether your candidate is stateful: check whether evaluate()
can return a non-None opportunity for the SAME pair across consecutive scans
on the same divergence. If yes, set pair_key.

### What `market` looks like

A raw row from `bot/state/universe.jsonl`. Sample keys:

```python
{
  "ts": "2026-04-29T04:08:44.366163+00:00",
  "scan_id": "20260429T040841",
  "ticker": "KXNBATOTAL-26APR30NYKATL-228",
  "series_ticker": "KXNBATOTAL",
  "event_ticker": "KXNBATOTAL-26APR30NYKATL",
  "status": "active",
  "close_ts": "2026-05-14T17:00:00Z",
  "yes_ask": 84,         # integer cents
  "yes_bid": 12,
  "no_ask": 88,
  "no_bid": 16,
  "volume": 0,           # lifetime
  "volume_24h": 0,
  "open_interest": 0,
  "title": "Game 6: New York at Atlanta: Total Points",
  "scanned_by": ["sports_monotonicity_arb"],
  "regime": {"time_of_day": "overnight", "day_of_week": "wed", ...},
}
```

`scanned_by` tells you which existing strategies already look at the market. If it's `[]`, the market is in **scanner-gap territory** — exactly what Session 48's `concurrent_attack_angles` heuristic is designed to surface and the lab is designed to evaluate.

### `CandidateOpportunity` fields

```python
@dataclass
class CandidateOpportunity:
    ticker: str
    side: str                  # "yes" | "no" — canonical schema, NEVER "yes_won"
    target_price_cents: float  # entry price the candidate would pay
    fair_value_cents: float    # candidate's modeled fair value
    edge_cents: float          # fair_value - target_price (signed)
    confidence: float          # 0.0-1.0 — varies → per-decile breakdown emitted
    reason: str                # short string for the report (reason histogram)
    extra: dict | None = None  # optional; supports `extra={"contracts": 50}` to override default 100 contracts
    pair_key: str | None = None  # Session 73: stateful-candidate dedup key. See "When to set pair_key" above.
```

## How to read a report

The driver writes `reports_out/strategy_lab_<name>_<date>.md`. Sections:

1. **LAB LIMITATIONS** — read these every time. The numbers are upper-bound hypotheticals, not P&L forecasts.
2. **Run metadata** — window, match-window-hours, rows scanned, would-have-bets emitted, resolved vs unresolved counts.
3. **Aggregate (resolved subset)** — mean CLV, win rate, total hypothetical P&L, settle rate.
4. **Per-sport breakdown** — one row per sport.
5. **Per-confidence-decile** — only when the candidate emits varied confidence.
6. **Top 5 winners / Top 5 losers** — drill-down on extremes.
7. **Reason histogram** — distribution of `opp.reason` strings.

A candidate showing **net-negative resolved P&L** is dead — even before slippage, it loses. A candidate showing positive P&L is *interesting*, not *proven* — see Limitations.

## Limitations (loud)

- **Hypothetical P&L is settlement-anchored.** No slippage, no exit-side logic, no partial fills, no order-book dynamics. Live execution will be 20-30%+ worse on slippage alone (see Session 15 microstructure architecture).
- **Only matches against existing `clv.json` records.** Markets the bot never evaluated produce `UNRESOLVED` tags — the lab can't judge them. Net-positive lab P&L on a market family the bot ignores entirely is suggestive but not actionable until the bot starts capturing CFs there (Session 31 territory).
- **±2 hour clv-match window is a heuristic.** Tune via `candidate.clv_match_window_hours` for long-dated futures or for opportunistic candidates that emit at unusual times.
- **Use the lab to FILTER ideas, not FORECAST production P&L.** Any candidate net-negative here is dead. A candidate net-positive here is a candidate worth investigating in a separate coder session — not auto-promoted.

## How to promote a candidate to production

The lab does **not** auto-promote. The lab's output is a markdown report; that's it. Acting on a finding requires a separate coder session that:

1. Refactors the candidate's `evaluate()` logic into a proper `bot/strategies/*.py` snapshot strategy (see `bot.strategies.Strategy` Protocol from Session 13a) OR a `TickStrategy` (Session 19a) for live-tick logic.
2. Wires the new strategy into `REGISTERED_STRATEGIES` (snapshot) or `live_watcher` (tick) — with the appropriate plumbing for `decisions.jsonl` instrumentation, CF emission via `bot/clv.py`, and `paper_trades.json` enrichment per Session 50 (`confidence`, `dqs`, `sport`).
3. Ships behind any safety gates — duplicate-entry guard, `STRATEGY_BUDGETS`, `MOMENTUM_DISABLED_SPORTS`, etc.
4. Restarts the bot, validates first scans, then runs an explicit re-validation routine ~14d post-deploy mirroring the Session 38a pattern.

A lab report alone is **not enough evidence** to ship production code. It says "worth investigating," not "ship it."

## Canonical schema reminder (`clv.json`)

| Field | Canonical | Anti-pattern (DO NOT USE) |
|---|---|---|
| Settlement value | `clv_cents` | `outcome_clv_cents` |
| Side that won | `market_result` ∈ `{"yes", "no", null}` | `outcome_settlement`, `"yes_won"`, `"no_won"` |
| CF reject reason | `skipped_by_gate` | `skip_reason` |
| Settled status | `status ∈ {"settled", "counterfactual_settled"}` | (these strings are stable; use them verbatim) |
| Entry price | `entry_price_cents` (integer cents 1-99) | `entry_price` (the schema reference shows this is dollars-as-float; the on-disk shape is cents-as-int) |

If you see a snippet in your candidate that uses any anti-pattern, **STOP and re-read [`CLAUDE.md` — "Canonical Data Schema Reference"](../../CLAUDE.md)**. Two real verification errors traced to schema-value typos in May 1, 2026; the canonical reference exists to prevent a third.

## Discipline

- Lab is **read-only** on `bot/state/`. Writes ONLY to `tools/strategy_lab/reports_out/`.
- Pure stdlib + reuse of `bot.*` and `tools.backtest.*` helpers. NO new dependencies.
- 0-candidate runs are acceptable and surface gracefully (the report says "0 would-have-bets" with a sentinel; the smoke test for the example candidate may legitimately produce 0 matches).
- Bot does not need to restart after lab changes (the lab is not imported by `bot/main.py`).

## Out of scope (v1)

- Genetic / grid-search parameter sweeps (single candidate per run).
- Live-trading harness (back-test only).
- Auto-prototyping candidates from Session 48 findings (workflow tooling for that is a separate session).
- Player-prop-specific candidate types (lab is sport / market-type agnostic; user writes the filter).
- Cross-strategy fusion candidates (lab evaluates one candidate at a time).
- Discovery-agent integration (lab is on-demand CLI, not scheduled).
