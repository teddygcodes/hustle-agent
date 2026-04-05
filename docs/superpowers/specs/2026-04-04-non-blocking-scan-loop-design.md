# Non-Blocking Scan Loop Design
**Date:** 2026-04-04
**Status:** Approved

---

## Problem

The main scan loop blocks on every GO/SKIP decision. When an opportunity alert is sent, the entire scanner freezes — no new scans, no new alerts — until the user responds or the market closes. With two active strategies finding 3-5 edges per day, a single ignored alert can hold up the loop for hours.

---

## Solution

Decouple scanning from execution. The scan loop fires alerts and immediately continues. Execution happens when the user responds to Telegram — not during the scan loop.

---

## Architecture

### Main Loop (bot/main.py)

**Remove:**
- `self._go_event` (asyncio.Event)
- `self._skip_event` (asyncio.Event)
- `self._pending_opportunity` (instance var)
- `self._pending_opp_id` (instance var)
- All `asyncio.wait()` calls blocking on GO/SKIP
- The MM opportunity blocking wait

**New loop shape for each opportunity:**
```
compute sizing
→ add to pending queue (_add_to_pending)
→ send alert (notifier.send_alert)
→ continue to next opportunity immediately
```

No waiting. No events. The scanner always keeps moving.

### GO Command (handle_go)

```
GO      → execute opportunity #1 (first in LIST)
GO N    → execute opportunity #N
```

Steps:
1. Parse N from args (default 1)
2. Load and prune pending queue
3. Look up entry at index N-1 — return error if out of range
4. Call `execute_trade(opp, opp["sizing"])`
5. If success: remove from pending, send confirmation
6. If failure: send failure reason, leave in queue (user can retry or SKIP)

### SKIP Command (handle_skip)

```
SKIP    → remove opportunity #1
SKIP N  → remove opportunity #N
```

Steps:
1. Parse N from args (default 1)
2. Load pending queue
3. Remove entry at index N-1 — return error if out of range
4. Save pruned queue, confirm to user

### Market Maker Flow

Same pattern as edge opportunities:
- Queue MM opportunity, send alert, continue scanning
- `GO N` / `SKIP N` handles MM pairs the same as edge trades
- MM opportunities appear in LIST alongside edge opportunities with `[MM]` prefix

### LIST Command

No change to output format. Numbering is already 1-based. Add `[MM]` tag prefix for market maker opportunities.

---

## Pending Queue Schema (unchanged)

Each entry in `bot/state/pending.json`:
```json
{
  "opp_id": "a1b2c3d4",
  "ticker": "KXHIGHNY-26APR05-T68",
  "type": "weather",
  "edge": 0.07,
  "relative_edge": 0.41,
  "recommended_side": "no",
  "added_at": "2026-04-04T14:00:00+00:00",
  "expires_at": "2026-04-05T03:59:00+00:00",
  "opp": { ...full opportunity dict with sizing... }
}
```

Expiry = market close time (already implemented). Entries auto-prune on every load.

---

## Execution at GO Time

Sizing was computed at scan time and stored in `opp["sizing"]`. At GO time:
- Use stored sizing directly
- `execute_trade` re-verifies edge internally (3¢ kill switch + edge threshold check)
- If edge evaporated or price moved: return failure reason, leave in queue

No balance re-calculation needed at GO time — `execute_trade`'s internal balance check handles stale sizing gracefully (worst case: 0 contracts → no trade).

---

## Files Changed

| File | Change |
|------|--------|
| `bot/main.py` | Remove `_go_event`, `_skip_event`, `_pending_opportunity`, `_pending_opp_id`. Remove all `asyncio.wait()` GO/SKIP blocks. Simplify opportunity loop to fire-and-forget. Same for MM flow. |
| `bot/main.py` | Update `handle_go(args)` to parse N, look up from pending by index, call `execute_trade`, send confirmation. |
| `bot/main.py` | Update `handle_skip(args)` to parse N (default 1), remove from pending by index. |
| `bot/market_maker.py` | No changes — `execute_mm_pair` called from `handle_go` when type is `market_maker`. |

No changes to `executor.py`, `scanner.py`, `notifier.py`, `clv.py`, or any other module.

---

## What Stays the Same

- Pending queue file and schema
- Edge re-verify (3¢ kill switch, edge threshold) inside `execute_trade`
- PAPER mode bypass
- CLV recording
- Expiry-based pruning
- LIST output format (add `[MM]` tag only)
- All other Telegram commands

---

## Success Criteria

1. Scanner completes a full cycle even when opportunities are queued and unanswered
2. `GO 2` executes the second item in LIST, not the first
3. `GO` with no number executes #1
4. `SKIP` with no number removes #1
5. Failed GO (edge evaporated) leaves opportunity in queue
6. Market maker opportunities appear in LIST and respond to `GO N`
