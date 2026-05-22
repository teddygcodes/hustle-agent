# S159 — Per-game NBA/NHL vig_stack replay + Outcome-D ladders (2026-05-21)

**SIMULATION ONLY.** Closes the S134 Open Loop. No scanner/config/`ACTIVE_STRATEGIES` change, no bot restart, no trades, no live Kalshi API. Fully offline (deterministic riskless-arb P&L). Source: all `bot/state/archive/universe-*.jsonl.gz` + current `universe.jsonl`.

## The gate (necessary-but-not-sufficient -> sufficient)

`buy all K NOs` -> exactly one outcome wins -> payout `(K-1)*100c`, cost `Σ NO_ask`. Capturable riskless arb iff `Σ NO_ask < (K-1)*100` (K=2 winners: `NO_ask_A + NO_ask_B < 100`). The headline `YES-sum > 100` is measured at `yes_ask` (pay the ask on every YES leg) — the adversarial mirror, not the capturable side.

| series | structure | full sets | Σyes_ask>100 | **gate: ΣNO_ask<payout** | median Σyes_mid (all) | Σyes_mid @gate-pass (≈#winners×100) | median margin |
|---|---|---|---|---|---|---|---|
| KXNBAGAME | 1-winner partition | 3680 | 97.4% | **3.7%** | 100c | 102c (≈1.0) | -1c |
| KXNHLGAME | 1-winner partition | 2995 | 95.9% | **2.0%** | 100c | 102c (≈1.0) | -1c |
| KXNHLSPREAD | overlap / nested | 2999 | 56.3% | ~~3.5%~~ (ARTIFACT) | 94c | 112c (≈1.1) | -12c |
| KXNHLTOTAL | overlap / nested | 2793 | 99.9% | ~~96.1%~~ (ARTIFACT) | 407c | 408c (≈4.1) | +260c |
| KXMLBTOTAL | overlap / nested | 5605 | 99.9% | ~~99.6%~~ (ARTIFACT) | 621c | 621c (≈6.2) | +508c |

Reading: a single-winner partition's `Σyes_mid` sits near 100c (one winner) **including its gate-passing sets**. If the gate-passers sit far above 100c (≈ #simultaneous winners × 100), the rungs OVERLAP — buying all NOs pays `(K-W)*100` with W>1, not `(K-1)*100`, so the gate margin is fictitious. KXNHLSPREAD's all-set median (94c) looks partition-like, but its gate-passers sit at ~200c (2 winners): the winning team satisfies both its `by-1` and `by-2+` rungs. The totals are nested `total>=N` thresholds (4-6 winners).

## KXNBAGAME — SINGLE_WINNER_PARTITION

- groups (scan,event): 3,680 | unique games: 51 | rung-count dist: {2: 3680} | median capture completeness: 1.00
- full-capture evaluable sets: 3,680 (skipped, unbuyable leg: 0)
- median Σyes_ask = 102c | median Σyes_mid = 100c | median ΣNO_ask = 101c

**Capturable-arb gate:** 137 of 3,680 full sets pass `ΣNO_ask < 100` = **3.7%** (136 of 137 partition-consistent / single-winner); median margin **-1c** (negative => typical set costs MORE than the guaranteed payout).

**Per-unique-game P&L** (each game traded once at its best qualifying scan; deterministic = payout − ΣNO_ask − Kalshi fees @ 0.07):

- unique games with ≥1 qualifying scan: **24** (of 51 games)
- Σ net P&L: **-59.0c** | median: **-3.0c** | Σ ex-top-winner: **-58.0c**
- games net-positive after fees: 0 / 24 | illiquid (a leg with 0 vol_24h or 0 OI): 0 / 24

  per-game qualifiers (best scan): event | k | ΣNO_ask | Σyes_mid | gross | fees | net | min leg vol_24h | min leg OI
  - `KXNBAGAME-26MAY07LALOKC` | k=2 | 99c | 102c | +1c | −2c | **-1c** | vol24=4597 | OI=4202
  - `KXNBAGAME-26MAY24OKCSAS` | k=2 | 97c | 104c | +3c | −4c | **-1c** | vol24=2003 | OI=1913
  - `KXNBAGAME-26APR30NYKATL` | k=2 | 98c | 104c | +2c | −4c | **-2c** | vol24=3584 | OI=3139
  - `KXNBAGAME-26MAY06MINSAS` | k=2 | 98c | 103c | +2c | −4c | **-2c** | vol24=6629 | OI=5459
  - `KXNBAGAME-26MAY08SASMIN` | k=2 | 98c | 103c | +2c | −4c | **-2c** | vol24=14458 | OI=35169
  - `KXNBAGAME-26MAY10SASMIN` | k=2 | 98c | 103c | +2c | −4c | **-2c** | vol24=3364 | OI=4883
  - `KXNBAGAME-26MAY11DETCLE` | k=2 | 98c | 103c | +2c | −4c | **-2c** | vol24=261 | OI=258
  - `KXNBAGAME-26MAY09OKCLAL` | k=2 | 98c | 103c | +2c | −4c | **-2c** | vol24=2865 | OI=3746
  - `KXNBAGAME-26MAY10NYKPHI` | k=2 | 98c | 103c | +2c | −4c | **-2c** | vol24=2001 | OI=5359
  - `KXNBAGAME-26MAY13CLEDET` | k=2 | 98c | 106c | +2c | −4c | **-2c** | vol24=99 | OI=101
  - `KXNBAGAME-26MAY22OKCSAS` | k=2 | 98c | 104c | +2c | −4c | **-2c** | vol24=5481 | OI=5150
  - `KXNBAGAME-26APR27DETORL` | k=2 | 99c | 102c | +1c | −4c | **-3c** | vol24=4529 | OI=5963
  - `KXNBAGAME-26APR27OKCPHX` | k=2 | 99c | 102c | +1c | −4c | **-3c** | vol24=51190 | OI=50494
  - `KXNBAGAME-26APR27MINDEN` | k=2 | 99c | 102c | +1c | −4c | **-3c** | vol24=4678 | OI=6514
  - `KXNBAGAME-26APR25DENMIN` | k=2 | 99c | 102c | +1c | −4c | **-3c** | vol24=11561170 | OI=8571863
  - `KXNBAGAME-26MAY01LALHOU` | k=2 | 99c | 102c | +1c | −4c | **-3c** | vol24=1645763 | OI=1786352
  - `KXNBAGAME-26MAY06PHINYK` | k=2 | 99c | 102c | +1c | −4c | **-3c** | vol24=1202 | OI=1122
  - `KXNBAGAME-26MAY08NYKPHI` | k=2 | 99c | 102c | +1c | −4c | **-3c** | vol24=7176 | OI=5995
  - `KXNBAGAME-26MAY09DETCLE` | k=2 | 99c | 102c | +1c | −4c | **-3c** | vol24=283 | OI=263
  - `KXNBAGAME-26MAY11OKCLAL` | k=2 | 99c | 102c | +1c | −4c | **-3c** | vol24=1173 | OI=3903
  - `KXNBAGAME-26MAY12MINSAS` | k=2 | 99c | 102c | +1c | −4c | **-3c** | vol24=533 | OI=443
  - `KXNBAGAME-26MAY15SASMIN` | k=2 | 99c | 102c | +1c | −4c | **-3c** | vol24=1947 | OI=956
  - `KXNBAGAME-26MAY15DETCLE` | k=2 | 99c | 102c | +1c | −4c | **-3c** | vol24=16859 | OI=21697
  - `KXNBAGAME-26MAY20SASOKC` | k=2 | 99c | 102c | +1c | −4c | **-3c** | vol24=2514 | OI=2411

_Verdict:_ spread artifact -> capturable arb on <10% of contemporaneous sets; median set costs MORE than the guaranteed payout

## KXNHLGAME — SINGLE_WINNER_PARTITION

- groups (scan,event): 2,995 | unique games: 44 | rung-count dist: {2: 2995} | median capture completeness: 1.00
- full-capture evaluable sets: 2,995 (skipped, unbuyable leg: 0)
- median Σyes_ask = 102c | median Σyes_mid = 100c | median ΣNO_ask = 101c

**Capturable-arb gate:** 61 of 2,995 full sets pass `ΣNO_ask < 100` = **2.0%** (61 of 61 partition-consistent / single-winner); median margin **-1c** (negative => typical set costs MORE than the guaranteed payout).

**Per-unique-game P&L** (each game traded once at its best qualifying scan; deterministic = payout − ΣNO_ask − Kalshi fees @ 0.07):

- unique games with ≥1 qualifying scan: **12** (of 44 games)
- Σ net P&L: **-28.0c** | median: **-2.5c** | Σ ex-top-winner: **-27.0c**
- games net-positive after fees: 0 / 12 | illiquid (a leg with 0 vol_24h or 0 OI): 0 / 12

  per-game qualifiers (best scan): event | k | ΣNO_ask | Σyes_mid | gross | fees | net | min leg vol_24h | min leg OI
  - `KXNHLGAME-26MAY10VGKANA` | k=2 | 97c | 104c | +3c | −4c | **-1c** | vol24=138 | OI=234
  - `KXNHLGAME-26MAY24COLVGK` | k=2 | 97c | 106c | +3c | −4c | **-1c** | vol24=423 | OI=358
  - `KXNHLGAME-26MAY09COLMIN` | k=2 | 98c | 107c | +2c | −4c | **-2c** | vol24=10 | OI=10
  - `KXNHLGAME-26MAY09CARPHI` | k=2 | 98c | 103c | +2c | −4c | **-2c** | vol24=490 | OI=884
  - `KXNHLGAME-26MAY16BUFMTL` | k=2 | 98c | 103c | +2c | −4c | **-2c** | vol24=1566 | OI=1430
  - `KXNHLGAME-26MAY22VGKCOL` | k=2 | 98c | 104c | +2c | −4c | **-2c** | vol24=1059 | OI=905
  - `KXNHLGAME-26APR26COLLA` | k=2 | 99c | 102c | +1c | −4c | **-3c** | vol24=6062 | OI=14070
  - `KXNHLGAME-26APR29MTLTB` | k=2 | 99c | 102c | +1c | −4c | **-3c** | vol24=187159 | OI=247311
  - `KXNHLGAME-26MAY08MTLBUF` | k=2 | 99c | 102c | +1c | −4c | **-3c** | vol24=527 | OI=1067
  - `KXNHLGAME-26MAY08VGKANA` | k=2 | 99c | 103c | +1c | −4c | **-3c** | vol24=550 | OI=999
  - `KXNHLGAME-26MAY10BUFMTL` | k=2 | 99c | 102c | +1c | −4c | **-3c** | vol24=103 | OI=266
  - `KXNHLGAME-26MAY13MINCOL` | k=2 | 99c | 102c | +1c | −4c | **-3c** | vol24=15987 | OI=14653

_Verdict:_ spread artifact -> capturable arb on <10% of contemporaneous sets; median set costs MORE than the guaranteed payout

## KXNHLSPREAD — NOT_SINGLE_WINNER_PARTITION

- groups (scan,event): 3,036 | unique games: 46 | rung-count dist: {4: 3032, 6: 4} | median capture completeness: 1.00
- full-capture evaluable sets: 2,999 (skipped, unbuyable leg: 0)
- median Σyes_ask = 103c | median Σyes_mid = 94c | median ΣNO_ask = 312c

**Structural disqualification — NOT a single-winner partition.** The all-set median Σyes_mid (94c) LOOKS partition-like, but **35% of the gate-passing sets price >1 simultaneous winner** (Σyes_mid up to ~250c) — the per-team `by-1`/`by-2+` rungs BOTH fire for the winning team, so the rungs overlap. A clean partition has ~0% such sets (the winners do), so this overlap is decisive. With W>=2 rungs resolving YES, buying all K NOs pays `(K-W)*100`, NOT `(K-1)*100`; the 3.5% apparent gate-pass (and any positive 'margin') is a fake-vig artifact of mis-applying the partition payout formula (the S144 B-vs-T lesson). No fixed-payout vig_stack arb exists. A realized P&L would require settled outcomes (outcome-dependent) and is moot — nothing riskless to realize. **Not a vig_stack shape; corrects the brief's 'closer to vig_stack shape' premise.**

## KXNHLTOTAL — NOT_SINGLE_WINNER_PARTITION

- groups (scan,event): 3,033 | unique games: 46 | rung-count dist: {2: 1, 3: 3, 4: 2, 5: 10, 6: 23, 7: 17, 8: 2976, 10: 1} | median capture completeness: 1.00
- full-capture evaluable sets: 2,793 (skipped, unbuyable leg: 0)
- median Σyes_ask = 444c | median Σyes_mid = 407c | median ΣNO_ask = 440c

**Structural disqualification — NOT a single-winner partition.** The K rungs are nested `total>=N` thresholds (all-set median Σyes_mid = 407c ≈ 4 simultaneous winners). With W>=2 rungs resolving YES, buying all K NOs pays `(K-W)*100`, NOT `(K-1)*100`; the 96.1% apparent gate-pass (and any positive 'margin') is a fake-vig artifact of mis-applying the partition payout formula (the S144 B-vs-T lesson). No fixed-payout vig_stack arb exists. A realized P&L would require settled outcomes (outcome-dependent) and is moot — nothing riskless to realize. **Not a vig_stack shape; corrects the brief's 'closer to vig_stack shape' premise.**

## KXMLBTOTAL — NOT_SINGLE_WINNER_PARTITION

- groups (scan,event): 7,707 | unique games: 317 | rung-count dist: {1: 25, 2: 48, 3: 22, 4: 3, 6: 5, 7: 5, 8: 8, 9: 6, 10: 28, 11: 7408, 12: 48, 13: 28, 14: 32, 15: 10, 16: 14, 17: 10, 18: 6, 22: 1} | median capture completeness: 1.00
- full-capture evaluable sets: 5,605 (skipped, unbuyable leg: 0)
- median Σyes_ask = 636c | median Σyes_mid = 621c | median ΣNO_ask = 492c

**Structural disqualification — NOT a single-winner partition.** The K rungs are nested `total>=N` thresholds (all-set median Σyes_mid = 621c ≈ 6 simultaneous winners). With W>=2 rungs resolving YES, buying all K NOs pays `(K-W)*100`, NOT `(K-1)*100`; the 99.6% apparent gate-pass (and any positive 'margin') is a fake-vig artifact of mis-applying the partition payout formula (the S144 B-vs-T lesson). No fixed-payout vig_stack arb exists. A realized P&L would require settled outcomes (outcome-dependent) and is moot — nothing riskless to realize. **Not a vig_stack shape; corrects the brief's 'closer to vig_stack shape' premise.**

## Outcome

**Outcome C.**

The apparent per-game 'structural vig' is illusory. Only the 2-outcome winners are a genuine single-winner partition (the vig_stack shape); the spread and totals are not. Two distinct mechanisms:

1. **2-outcome winners (KXNBAGAME / KXNHLGAME): spread artifact.** The `yes_ask`-sum>100 headline (~96-97%, reconciling with S133/S134) is the adversarial mirror — at MID the median sum is exactly 100c (no real vig). The capturable `NO_ask_A + NO_ask_B < 100` gate fires on only 2-4% of sets at a negative median margin, and every thin qualifier nets ≤0 after Kalshi fees. The ~12c spread eats the entire mid-vig.
2. **Spread + totals (KXNHLSPREAD / KXNHLTOTAL / KXMLBTOTAL): not a single-winner partition.** Totals are nested `total>=N` thresholds (~4-6 simultaneous winners); the spread's per-team `by-1`/`by-2+` rungs both fire for the winning team (~2 winners at the gate-passing sets). When W>=2 rungs resolve YES, buying all NOs pays `(K-W)*100`, not `(K-1)*100`, so the high apparent gate-pass / positive 'margin' (incl. KXNHLSPREAD's +$14.23 on liquid sets) is a formula-misapplication artifact. **This corrects the brief's premise** that the multi-rung ladders are 'closer to true vig_stack shape than 2-outcome winners' — they are further from it.

**=> CLOSE the S134 Open Loop, ruled-out.** Do NOT ship a scanner expansion for per-game winners, spreads, or totals. With thousands of contemporaneous sets this is a genuine ruled-out, not N-thin.

## Notes

- **Battle Scar #9 (auto-exit exemption) — MOOT this session.** `vig_stack_series`/`vig_stack_no` are auto-exit-EXEMPT; `vig_stack_futures` is NOT. A per-game expansion would need an explicit exemption decision — flagged, not decided, because Outcome C ships no scanner expansion.
- **Settlement not required.** For a true partition the bought-NO set pays `(K-1)*100c` regardless of which outcome wins, so P&L is deterministic from prices alone — the sim is fully offline (no Kalshi API, no contention with the running bot). Game void/cancel is not modelled (would void both legs).
- **Fee model:** Kalshi published `ceil(0.07·C·p·(1−p))` per NO leg, per-contract; no fee helper exists in `bot/` so it is modelled here.
- **Pairing integrity:** the cursor walk captures both/all legs of a game together (median completeness 1.00, no singletons), so the 100%-partial-scan regime (CLAUDE.md §12) does not corrupt reconstruction.
- **Outcome-D scope:** ran full classification on KXNHLSPREAD, KXNHLTOTAL, KXMLBTOTAL. None is a single-winner partition, so none is a vig_stack shape; if a future session wants their outcome-dependent P&L it needs settled outcomes (a different, settlement-dependent sim).
- **Classifier guard:** a set enters the gate/P&L only if partition-consistent (Σyes_mid ≤ 130c ≈ one winner). This is what prevents the KXNHLSPREAD trap — its gate-passers sit at ~200c (two winners), so they are correctly excluded as non-arb rather than booked as a fictitious +$14.23.
