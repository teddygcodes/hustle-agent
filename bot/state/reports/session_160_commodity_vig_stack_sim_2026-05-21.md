# S160 — Commodity-ladder vig_stack simulation (2026-05-21)

**SIMULATION ONLY.** Next prospecting pass after S159. No scanner/config/`ACTIVE_STRATEGIES` change, no bot restart, no trades, no live Kalshi API. Fully offline; source: all `bot/state/archive/universe-*.jsonl[.gz]` + current `universe.jsonl`. Imports no bot modules.

**Question:** do commodity price-ladder families (KXWTI / KXSILVERD / KXAAAGASD / KXAAAGASW) carry capturable vig like KXINX (a stable-whitelisted vig_stack member)? **Finding:** they are cumulative *above-$X* THRESHOLD ladders (nested / overlapping), not the exclusive `-B` between-bucket partition KXINX uses. The operator-chosen test reconstructs an exclusive partition by differencing adjacent thresholds and asks whether *that* carries edge.

## Benchmark: KXINX (`-B` partition, direct) vs commodity `-T` families (synthetic)

| series | structure | full sets | median ITM rungs (≈winners) | RAW Σyes_mid | partition Σmid | buy-all margin | after fees | Σ-YES gate (mid / ask) |
|---|---|---|---|---|---|---|---|---|
| KXINX (direct) | 1-winner partition | 866 | 0 | 162c | 162c | -64c | -70c | 97% / 100% |
| KXWTI (synthetic) | nested / overlap | 46 | 8 | 794c | 100c | -56c | -108c | 0% / 100% |
| KXSILVERD (synthetic) | nested / overlap | 57 | 22 | 2230c | 100c | -283c | -400c | 0% / 100% |
| KXAAAGASD (synthetic) | nested / overlap | 59 | 9 | 842c | 100c | -100c | -139c | 0% / 98% |
| KXAAAGASW | INSUFFICIENT DATA | 0 | — | — | — | — | — | — |
| KXGOLDD (synthetic) | nested / overlap | 82 | 20 | 2034c | 100c | -314c | -411c | 0% / 100% |
| KXBRENTD (synthetic) | nested / overlap | 67 | 12 | 1092c | 100c | -153c | -229c | 0% / 100% |
| KXNATGASD (synthetic) | nested / overlap | 16 | 16 | 2047c | 100c | -681c | -822c | 0% / 100% |

**Reading.** The decisive mutual-exclusivity number is **median ITM rungs** (rungs with yes_mid>50c ≈ simultaneous YES winners): KXINX `-B` ≈ 1 (one bucket wins); every commodity family is many (a settled price makes every lower threshold YES at once). RAW Σyes_mid is unreliable here — KXINX's reads high only because illiquid `-B` wings quote a wide bid-ask (yes_bid=0/yes_ask=7 → phantom mid), not because it overlaps. After differencing the commodity thresholds into a synthetic partition, **every family's synthetic Σmid is ~100c** (no overround, by construction — a differenced monotone CDF is a probability distribution). The only "vig" left is the bid-ask spread, and buying all synthetic NOs costs exactly that: buy-all margin **= −Σspread** (negative), more negative after fees. The Σ-YES≥105c gate fires on ~0% of synthetic partitions. This is S159's per-game spread-artifact lesson, now provable for the commodity threshold class.

## KXINX — SINGLE_WINNER_PARTITION

- groups (scan,event): 866 | unique events: 17 | full-capture ladders: 866 | analysis mode: direct
- structure: median **0.0 ITM rungs** (yes_mid>50c ≈ simultaneous winners) | RAW median Σyes_mid = 162c | RAW median Σyes_ask = 308c
- buy-all NOs: partition Σmid = 161.8c | median margin = -64.0c (Σ -52613c) | after fees @ 0.035 = -69.5c (Σ -56948c)
- Σ-YES≥105c gate pass: 97% (mid) / 100% (ask) | monotonicity-clamp violations: 0
- per-rung NO-floor clearing (settlement-free; median per-ladder #NOs ≥ floor / Σcost): 0.70 → 28 NOs / 2764c | 0.85 → 28 NOs / 2764c | 0.93 → 26 NOs / 2588c
- settlement (price-convergence) coverage: 1/17 events (6%)
- floor-sweep realized P&L: settlement coverage too thin (1 < 5 settled events) -> **Outcome B** for the realized-P&L sub-analysis; the structural buy-all margin above is settlement-free and stands.

_Verdict:_ single-winner partition (median 0 simultaneously-ITM rungs, ≤1 — two exclusive outcomes can't both exceed 50%) — the genuine vig_stack shape; benchmark reference.

## KXWTI — NOT_SINGLE_WINNER_PARTITION

- groups (scan,event): 79 | unique events: 16 | full-capture ladders: 46 | analysis mode: synthetic
- structure: median **8.0 ITM rungs** (yes_mid>50c ≈ simultaneous winners) | RAW median Σyes_mid = 794c | RAW median Σyes_ask = 840c
- buy-all NOs: partition Σmid = 100.0c | median margin = -56.0c (Σ -4953c) | after fees @ 0.070 = -108.0c (Σ -7147c) | median Σspread = 56c (= −margin)
- Σ-YES≥105c gate pass: 0% (mid) / 100% (ask) | monotonicity-clamp violations: 17
- per-rung NO-floor clearing (settlement-free; median per-ladder #NOs ≥ floor / Σcost): 0.70 → 16 NOs / 1541c | 0.85 → 16 NOs / 1536c | 0.93 → 14 NOs / 1404c
- settlement (price-convergence) coverage: 0/16 events (0%)
- floor-sweep realized P&L: settlement coverage too thin (0 < 5 settled events) -> **Outcome B** for the realized-P&L sub-analysis; the structural buy-all margin above is settlement-free and stands.

_Verdict:_ NOT a partition (median 8 ITM rungs ≈ that many simultaneous YES winners; RAW Σyes_mid 794c). Synthetic partition Σmid=100c (no overround) and buy-all margin =-56c (= -Σspread), -108c after fees -> spread artifact, no capturable vig (Outcome C).

## KXSILVERD — NOT_SINGLE_WINNER_PARTITION

- groups (scan,event): 121 | unique events: 11 | full-capture ladders: 57 | analysis mode: synthetic
- structure: median **22.0 ITM rungs** (yes_mid>50c ≈ simultaneous winners) | RAW median Σyes_mid = 2230c | RAW median Σyes_ask = 2604c
- buy-all NOs: partition Σmid = 100.0c | median margin = -283.0c (Σ -28399c) | after fees @ 0.070 = -400.0c (Σ -35290c) | median Σspread = 283c (= −margin)
- Σ-YES≥105c gate pass: 0% (mid) / 100% (ask) | monotonicity-clamp violations: 161
- per-rung NO-floor clearing (settlement-free; median per-ladder #NOs ≥ floor / Σcost): 0.70 → 41 NOs / 4283c | 0.85 → 41 NOs / 4279c | 0.93 → 41 NOs / 4237c
- settlement (price-convergence) coverage: 0/11 events (0%)
- floor-sweep realized P&L: settlement coverage too thin (0 < 5 settled events) -> **Outcome B** for the realized-P&L sub-analysis; the structural buy-all margin above is settlement-free and stands.

_Verdict:_ NOT a partition (median 22 ITM rungs ≈ that many simultaneous YES winners; RAW Σyes_mid 2230c). Synthetic partition Σmid=100c (no overround) and buy-all margin =-283c (= -Σspread), -400c after fees -> spread artifact, no capturable vig (Outcome C).

## KXAAAGASD — NOT_SINGLE_WINNER_PARTITION

- groups (scan,event): 77 | unique events: 19 | full-capture ladders: 59 | analysis mode: synthetic
- structure: median **9.0 ITM rungs** (yes_mid>50c ≈ simultaneous winners) | RAW median Σyes_mid = 842c | RAW median Σyes_ask = 913c
- buy-all NOs: partition Σmid = 100.0c | median margin = -100.0c (Σ -20630c) | after fees @ 0.070 = -139.0c (Σ -22770c) | median Σspread = 100c (= −margin)
- Σ-YES≥105c gate pass: 0% (mid) / 98% (ask) | monotonicity-clamp violations: 145
- per-rung NO-floor clearing (settlement-free; median per-ladder #NOs ≥ floor / Σcost): 0.70 → 19 NOs / 1904c | 0.85 → 18 NOs / 1899c | 0.93 → 17 NOs / 1715c
- settlement (price-convergence) coverage: 1/19 events (5%)
- floor-sweep realized P&L: settlement coverage too thin (1 < 5 settled events) -> **Outcome B** for the realized-P&L sub-analysis; the structural buy-all margin above is settlement-free and stands.

_Verdict:_ NOT a partition (median 9 ITM rungs ≈ that many simultaneous YES winners; RAW Σyes_mid 842c). Synthetic partition Σmid=100c (no overround) and buy-all margin =-100c (= -Σspread), -139c after fees -> spread artifact, no capturable vig (Outcome C).

## KXAAAGASW — INSUFFICIENT_DATA

- groups (scan,event): 25 | unique events: 3 | full-capture ladders: 0 | analysis mode: n/a

_Verdict:_ INSUFFICIENT DATA — no full-capture ladders (partial scans truncate this family).

## KXGOLDD — NOT_SINGLE_WINNER_PARTITION

- groups (scan,event): 110 | unique events: 11 | full-capture ladders: 82 | analysis mode: synthetic
- structure: median **20.0 ITM rungs** (yes_mid>50c ≈ simultaneous winners) | RAW median Σyes_mid = 2034c | RAW median Σyes_ask = 2228c
- buy-all NOs: partition Σmid = 100.0c | median margin = -313.5c (Σ -36270c) | after fees @ 0.070 = -411.0c (Σ -44953c) | median Σspread = 314c (= −margin)
- Σ-YES≥105c gate pass: 0% (mid) / 100% (ask) | monotonicity-clamp violations: 212
- per-rung NO-floor clearing (settlement-free; median per-ladder #NOs ≥ floor / Σcost): 0.70 → 41 NOs / 4314c | 0.85 → 41 NOs / 4314c | 0.93 → 41 NOs / 4314c
- settlement (price-convergence) coverage: 0/11 events (0%)
- floor-sweep realized P&L: settlement coverage too thin (0 < 5 settled events) -> **Outcome B** for the realized-P&L sub-analysis; the structural buy-all margin above is settlement-free and stands.

_Verdict:_ NOT a partition (median 20 ITM rungs ≈ that many simultaneous YES winners; RAW Σyes_mid 2034c). Synthetic partition Σmid=100c (no overround) and buy-all margin =-314c (= -Σspread), -411c after fees -> spread artifact, no capturable vig (Outcome C).

## KXBRENTD — NOT_SINGLE_WINNER_PARTITION

- groups (scan,event): 108 | unique events: 12 | full-capture ladders: 67 | analysis mode: synthetic
- structure: median **12.0 ITM rungs** (yes_mid>50c ≈ simultaneous winners) | RAW median Σyes_mid = 1092c | RAW median Σyes_ask = 1208c
- buy-all NOs: partition Σmid = 100.0c | median margin = -153.0c (Σ -16681c) | after fees @ 0.070 = -229.0c (Σ -21628c) | median Σspread = 153c (= −margin)
- Σ-YES≥105c gate pass: 0% (mid) / 100% (ask) | monotonicity-clamp violations: 29
- per-rung NO-floor clearing (settlement-free; median per-ladder #NOs ≥ floor / Σcost): 0.70 → 21 NOs / 2152c | 0.85 → 20 NOs / 2110c | 0.93 → 19 NOs / 2039c
- settlement (price-convergence) coverage: 0/12 events (0%)
- floor-sweep realized P&L: settlement coverage too thin (0 < 5 settled events) -> **Outcome B** for the realized-P&L sub-analysis; the structural buy-all margin above is settlement-free and stands.

_Verdict:_ NOT a partition (median 12 ITM rungs ≈ that many simultaneous YES winners; RAW Σyes_mid 1092c). Synthetic partition Σmid=100c (no overround) and buy-all margin =-153c (= -Σspread), -229c after fees -> spread artifact, no capturable vig (Outcome C).

## KXNATGASD — NOT_SINGLE_WINNER_PARTITION

- groups (scan,event): 146 | unique events: 12 | full-capture ladders: 16 | analysis mode: synthetic
- structure: median **16.0 ITM rungs** (yes_mid>50c ≈ simultaneous winners) | RAW median Σyes_mid = 2047c | RAW median Σyes_ask = 2503c
- buy-all NOs: partition Σmid = 100.0c | median margin = -681.0c (Σ -13571c) | after fees @ 0.070 = -822.0c (Σ -16774c) | median Σspread = 681c (= −margin)
- Σ-YES≥105c gate pass: 0% (mid) / 100% (ask) | monotonicity-clamp violations: 238
- per-rung NO-floor clearing (settlement-free; median per-ladder #NOs ≥ floor / Σcost): 0.70 → 61 NOs / 7240c | 0.85 → 60 NOs / 7240c | 0.93 → 60 NOs / 7150c
- settlement (price-convergence) coverage: 1/12 events (8%)
- floor-sweep realized P&L: settlement coverage too thin (1 < 5 settled events) -> **Outcome B** for the realized-P&L sub-analysis; the structural buy-all margin above is settlement-free and stands.

_Verdict:_ NOT a partition (median 16 ITM rungs ≈ that many simultaneous YES winners; RAW Σyes_mid 2047c). Synthetic partition Σmid=100c (no overround) and buy-all margin =-681c (= -Σspread), -822c after fees -> spread artifact, no capturable vig (Outcome C).

## Outcome

**Outcome C.**

Commodity price-ladder families are **ruled out** for vig_stack on two independent grounds, each with an explicit deciding number:

1. **Not a single-winner partition.** Every commodity family with data shows a median of **many** simultaneously-in-the-money rungs (≈ many simultaneous YES winners), vs KXINX `-B` at median **0 ITM rungs** (≤1 — a true partition can have at most one >50%-likely outcome). Commodities are cumulative `above-$X` thresholds — the KXNHLSPREAD overlap failure S159 caught. Buying all NOs on the raw rungs pays `(K−W)*100` with W>1, not `(K−1)*100`.
2. **Synthetic partition has no capturable vig.** Differencing the thresholds yields synthetic Σmid ≈ 100c for every family (no mid-overround, by construction). The buy-all-synthetic-NOs structural arb margin is **negative = −Σspread** (median, before fees) and more negative after fees, and the real vig_stack Σ-YES≥105c gate fires on ~0% of synthetic ladders at mid. The only "vig" is the bid-ask spread, doubled by the 2-leg synthetic construction — S159's spread artifact, now provable.

**=> Do NOT recommend a scanner expansion for commodity price-ladder families (KXWTI / KXSILVERD / KXAAAGASD / KXAAAGASW; Outcome-D KXGOLDD / KXBRENTD / KXNATGASD share the identical threshold structure and are ruled out for the same reason). Honest Outcome C narrows the frontier to the parked `post_event_reversion` strategy class.**

Data note: KXAAAGASW had no full-capture ladders (partial scans truncate these tiny families) — excluded from the ruling-out as a data gap, not evidence of edge.

## Notes

- **Synthetic construction.** Synthetic-bucket NO on `[X_i,X_{i+1})` = long `C_i` NO + long `C_{i+1}` YES (pays 100 iff price NOT in the bucket), buy-only, cost `no_ask_i + yes_ask_{i+1}` (2 legs). Tails are single-leg. Σ over all buckets of the NO cost = `Σ(yes_ask_i + no_ask_i)` = `100K + Σspread`, vs the one-winner payout `K*100`, so buy-all margin = `−Σspread` exactly (verified per ladder via the Σspread column).
- **Mutual-exclusivity discriminator.** Median count of rungs with yes_mid>50c (≈ simultaneous YES winners) — robust to the bid-ask spread that inflates Σyes_mid on illiquid `-B` wings. A single-winner partition reads ~1; nested thresholds read many.
- **No-overround is structural.** Differencing a monotone survival curve telescopes to Σ prob = 100c; the synthetic partition cannot carry mid-overround. Measured median synthetic Σmid per family confirms ~100c ± rounding.
- **Dedup (S86/S156).** Grouped by `(scan_id, event_ticker)`; rungs deduped by ticker within a scan; floor-sweep realized P&L reported raw (per-event best) and unique-day to avoid intra-day-scan inflation.
- **Settlement.** Offline price-convergence only: an event's latest snapshot whose curve has collapsed (one bucket ≥90c, rest ≤10c) pins the winning bucket; un-collapsed events are excluded from realized P&L. No live API.
- **Fees.** `ceil(rate·p·(1−p))` per leg; KXINX 0.035 (S&P financial rate), commodities 0.07 (general; conservative).
- **Fill feasibility.** Commodity per-rung volume/OI is far below NBA's; the 2-leg synthetic NO needs BOTH legs to fill (min across legs reported as the `illiquid` count) — an additional barrier even if the spread economics worked.
- **Completeness guard.** Only full-capture ladders (`k_here == k_true`, `k_true ≥ 3`) are evaluated; partial scans / truncated ladders are skipped.
