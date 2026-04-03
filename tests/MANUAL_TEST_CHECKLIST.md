# Manual Test Checklist

Run these in order. Each step verifies a specific capability.
Before starting: ensure `state/agent_state.json` has a fresh default state (balance $100, cycle 0, status "planning").

---

## Phase 1: Cold Start (planning mode)

- [ ] 1. Verify `state/agent_state.json` has status `"planning"` and balance `100.00`
- [ ] 2. Run `python agent/engine.py health` — should show planning status, $100 balance, all fields present
- [ ] 3. Run `python agent/engine.py cycle` — first cycle should:
    - [ ] Agent picks a name (check `agent_state.json` `"name"` field)
    - [ ] Agent chooses dream GPU (check `agent_state.json` `"dream_gpu"` object)
    - [ ] Journal entry written to `state/journal.md`
    - [ ] Message to Tyler in `state/conversations.json`
    - [ ] UI request in `state/ui_requests.json`
    - [ ] API costs logged in `state/api_costs.json`
    - [ ] Operational expense recorded in `state/ledger.json` tagged `"operations"`
    - [ ] `logs/events.jsonl` has `cycle_start`, `tool_call`, and `cycle_end` events
- [ ] 4. Check `state/backups/` has a backup set (agent_state + ledger files)

## Phase 2: Conversation Memory

- [ ] 5. Run `python agent/engine.py send -m "I think Kalshi is the best play. What do you think?"`
- [ ] 6. Run `python agent/engine.py cycle` — verify:
    - [ ] Agent responds to Tyler's message (check `conversations.json`)
    - [ ] Tyler takeaways extracted (check `state/memory.json` `"tyler_takeaways"`)
    - [ ] Takeaway references Tyler's Kalshi preference
- [ ] 7. Run another cycle WITHOUT messaging — verify tyler_context in logs still references the Kalshi conversation

## Phase 3: Projection System

- [ ] 8. Send: `python agent/engine.py send -m "Run a projection on your top strategy before I give you money"`
- [ ] 9. Run `python agent/engine.py cycle` — verify:
    - [ ] Agent calls `run_projection` tool
    - [ ] Projection saved in `state/projections.json`
    - [ ] Projection has `bull_case`, `bear_case`, `verdict`, `assumptions`, `risks`
    - [ ] Projection factors in API operational costs (`operational_overhead` > 0)

## Phase 4: Planning Mode Enforcement

- [ ] 10. Check that the agent has NOT spent any money beyond API costs (no expense/investment transactions in ledger except `"operations"`)
- [ ] 11. If the agent tried to spend, verify it got the planning mode block message in the logs

## Phase 5: Risk Management

- [ ] 12. Manually set balance to `65` in `agent_state.json`
- [ ] 13. Run `python agent/engine.py cycle` — verify:
    - [ ] System prompt includes `CAPITAL PRESERVATION MODE` (check logs)
    - [ ] Agent's reasoning reflects conservative posture
    - [ ] Agent journals about being in preservation mode
- [ ] 14. Reset balance to `100.00` in `agent_state.json`

## Phase 6: Activation

- [ ] 15. Run `python agent/engine.py activate`
    - [ ] `agent_state.json` status is `"active"`
    - [ ] Activation message in `conversations.json` and `inbox.json`
- [ ] 16. Run `python agent/engine.py cycle` — verify agent acknowledges activation and begins executing
- [ ] 17. If agent tries to spend, verify:
    - [ ] Spend under $5 goes through without projection warning
    - [ ] Spend over $5 triggers projection requirement (check for `WARNING` in tool result)

## Phase 7: Pause

- [ ] 18. Run `python agent/engine.py pause`
    - [ ] Status back to `"planning"` in `agent_state.json`
    - [ ] Agent gets pause message on next cycle (check `inbox.json`)

## Phase 8: Self-Improvement Proposals

- [ ] 19. Send: `python agent/engine.py send -m "If you could add one new capability to yourself, what would it be?"`
- [ ] 20. Run `python agent/engine.py cycle` — see if agent calls `propose_improvement`
- [ ] 21. Run `python agent/engine.py proposals` — verify proposal listed
- [ ] 22. Run `python agent/engine.py approve 1` — verify status changes to `"approved"`
- [ ] 23. Run `python agent/engine.py reject 2 -m "too risky"` (if there's a second proposal) — verify status and feedback

## Phase 9: Health Check

- [ ] 24. Run `python agent/engine.py health` — verify ALL fields:
    - [ ] Running status (daemon RUNNING or STOPPED)
    - [ ] Balance and GPU fund
    - [ ] Burn rate and survival estimate
    - [ ] Risk posture
    - [ ] Pipeline count
    - [ ] Watch count
    - [ ] Pending proposals
    - [ ] Projection accuracy (if projections exist)
    - [ ] Mood

## Phase 10: Logging Verification

- [ ] 25. Check `logs/agent.log` — human readable, recent cycles present
- [ ] 26. Check `logs/events.jsonl` — verify these event types exist:
    - [ ] `cycle_start`, `cycle_end`
    - [ ] `tool_call`
    - [ ] `api_cost`
    - [ ] `message_received`, `message_sent`
    - [ ] `projection_created` (if projections were run)

## Phase 11: Stress Test

- [ ] 27. Run 3 rapid cycles back to back:
    ```bash
    python agent/engine.py cycle && python agent/engine.py cycle && python agent/engine.py cycle
    ```
    Verify:
    - [ ] State files don't corrupt (all valid JSON — run `python -c "import json; json.load(open('state/agent_state.json'))"`)
    - [ ] Balance math stays consistent (ledger balance_after matches agent_state balance)
    - [ ] No duplicate ledger entries (check IDs are sequential)
    - [ ] Backups accumulate correctly in `state/backups/`

---

## Known Bugs to Watch For

During manual testing, keep an eye on these documented bugs:

| # | Bug | How to Spot |
|---|-----|-------------|
| 1 | Negative transaction amounts increase balance | Agent records a negative expense — balance goes UP |
| 2 | Pipeline history crash on old items | `update_pipeline` errors on items missing `"history"` key |
| 3 | Code safety bypass with multiple spaces | Agent runs `rm  -rf  /` with extra spaces — not caught |
| 4 | API cost `balance_after` stale | Ledger's last `operations` entry `balance_after` doesn't match state balance |
| 5 | No risk posture in projection verdict | Agent in preservation mode gets `strong_buy` verdict |

---

## Verdict

If all checks pass, the agent is ready for real money. Run `python agent/engine.py activate` and let it cook.
