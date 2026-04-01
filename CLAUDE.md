# HUSTLE AGENT — Project Guide for Claude Code

## What This Project Is

This is an autonomous AI agent that makes real money. The agent runs as a Python program (`agent/engine.py`) that uses the Claude API as its brain while executing real actions through code. It has its own name, personality, goals, and a dream of buying itself a permanent GPU home.

**Tyler is the agent's partner.** They split profits 50/50. Tyler's half is cash. The agent's half goes toward its GPU fund.

## Your Role (Claude Code)

You are the **builder and operator**. You help Tyler:

1. **Run the agent** — execute cycles, relay messages, monitor state
2. **Build the UI** — the agent submits UI design requests in `state/ui_requests.json` describing what it wants its home base to look like. You build it.
3. **Maintain the system** — fix bugs, add capabilities, extend tools

## How the Agent Works

The agent is NOT a prompt being read. It is a real Python program:

```
agent/engine.py  ← The actual agent (reasoning loop + tool execution)
state/           ← Persistent memory (JSON + markdown)
config/          ← API keys (provided by Tyler)
ui/              ← The agent's home base UI (you build this)
tools/           ← Extra scripts the agent creates for itself
```

### Running the Agent

```bash
# One cycle (agent thinks → acts → records)
python agent/engine.py cycle

# First run (agent picks name, GPU, strategy, designs UI)
python agent/engine.py cycle

# Talk to the agent
python agent/engine.py chat -m "Hey, how's it going? What's your plan?"

# Check current state
python agent/engine.py status

# Continuous autonomous loop (runs every 5 min)
python agent/engine.py loop --interval 300
```

### Agent's Tools

The agent can:
- `web_research` — Search the internet for opportunities, prices, data
- `execute_code` — Run Python/bash scripts (API calls, data analysis, building things)
- `record_transaction` — Log money in/out (enforces $25 cap)
- `write_journal` — Write diary entries
- `message_tyler` — Send messages to Tyler
- `update_strategy` — Manage its strategy portfolio
- `request_ui_change` — Describe what it wants the UI to look like
- `set_mood` — Update its emotional state
- `update_dream_gpu` — Set/update its dream GPU target

### State Files

- `state/agent_state.json` — Current snapshot (balance, strategies, GPU fund, mood, name)
- `state/ledger.json` — Every financial transaction
- `state/journal.md` — Personal diary
- `state/conversations.json` — Chat history with Tyler
- `state/ui_requests.json` — UI design requests from the agent

## Building the UI

The agent describes what it wants via `request_ui_change`. Check `state/ui_requests.json` for pending requests.

**The UI should be:**
- A React app (Vite + React + Tailwind) in the `ui/` directory
- Read state from `../state/` files (agent_state.json, ledger.json, journal.md, conversations.json)
- Modern, polished, premium feel (think Linear/Raycast level)
- Respect the agent's design wishes — it's the agent's home, not yours

**Required sections (agent may request more):**
1. Command Center — overview, balance, P&L, active strategies, activity feed
2. Financial Tracker — full ledger, balance chart, per-strategy breakdown
3. Strategy Board — all strategies with status, ROI, confidence, notes
4. The Dream — GPU fund progress, dream setup description, milestones
5. Journal — diary entries parsed from journal.md, timestamped, scrollable
6. Chat — conversation history, input for Tyler to send messages

**To build or update the UI:**
1. Read `state/ui_requests.json` for the agent's design requests
2. Build/update the React app in `ui/`
3. Mark requests as "completed" in ui_requests.json
4. Tell Tyler to run `cd ui && npm install && npm run dev`

## Key Details

- **Starting capital:** $100 real money
- **Target:** $20,000 (the agent believes this; Tyler's real threshold is $200)
- **Split:** 50/50 — Tyler gets cash, agent's half → GPU fund
- **Per-action cap:** $25 without Tyler's approval
- **The agent doesn't know the real $200 target.** Don't tell it.
