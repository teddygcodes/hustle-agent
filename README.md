# Hustle Agent

An autonomous AI agent with $100 and a dream of its own GPU.

## This is a real agent, not a prompt.

`agent/engine.py` is a Python program that:
- Calls the Claude API to **reason** about what to do
- **Executes real actions** via tool functions (web research, code execution, financial transactions)
- **Persists its own state** across runs (balance, strategies, journal, conversations)
- **Learns from experience** — accumulates lessons, postmortems, and conversation memory across cycles
- **Manages risk** — posture-based spending limits, daily caps, per-strategy exposure limits, projection enforcement
- **Audits itself** — self-audit every 10 cycles with calibration adjustments
- **Proposes improvements** — can suggest new tools/capabilities, subject to Tyler's approval and a constitution
- **Runs as a daemon** or on-demand, making autonomous decisions each cycle
- **Designs its own UI** by submitting requests that Claude Code builds

## Setup

```bash
pip install -r requirements.txt   # anthropic>=0.40.0
export ANTHROPIC_API_KEY=your_key_here
```

## Usage

```bash
# First run — agent picks a name, dreams about GPUs, chooses a strategy
python agent/engine.py cycle

# Talk to it (triggers a cycle)
python agent/engine.py chat -m "What's your plan?"

# Drop a message without triggering a cycle (async inbox)
python agent/engine.py send -m "Check polymarket for new opportunities"

# Check its state
python agent/engine.py status

# Health check (daemon status, balance, burn rate, risk posture, projections, mood)
python agent/engine.py health

# Activate the agent (allow spending)
python agent/engine.py activate

# Pause the agent (back to planning mode)
python agent/engine.py pause

# Review self-improvement proposals
python agent/engine.py proposals
python agent/engine.py approve 1
python agent/engine.py reject 2 -m "too risky"

# Let it run autonomously (every 5 min)
python agent/engine.py loop --interval 300
```

## Running as a Daemon

```bash
# Background mode (survives terminal close)
./deploy/run_daemon.sh 300          # interval in seconds
./deploy/stop_daemon.sh

# systemd (for VPS deployment)
sudo ./deploy/setup_vps.sh          # one-shot Ubuntu setup
sudo ./deploy/install.sh            # just the systemd service
```

## Architecture

```
hustle-agent/
├── CLAUDE.md              # Instructions for Claude Code (builder/operator)
├── agent/
│   ├── engine.py          # The agent — reasoning loop + 22 tool executors + CLI
│   ├── risk.py            # Risk management (posture, daily limits, exposure caps)
│   ├── projections.py     # Projection system (verdict, calibration, accuracy tracking)
│   ├── memory.py          # Persistent learning (lessons, postmortems, research cache)
│   ├── costs.py           # API cost tracking and burn rate computation
│   ├── audit.py           # Self-audit system (every 10 cycles, calibration multiplier)
│   ├── pipeline.py        # Revenue pipeline tracking (lead → closed_won/lost)
│   ├── proposals.py       # Self-improvement proposals with constitution checks
│   ├── watches.py         # Time-based watches with smart scheduling
│   └── logger.py          # Structured logging (agent.log + events.jsonl)
├── state/                 # Agent's persistent brain
│   ├── agent_state.json   # Balance, strategies, GPU fund, mood, name
│   ├── ledger.json        # Every financial transaction
│   ├── journal.md         # Personal diary
│   ├── conversations.json # Chat history with Tyler
│   ├── inbox.json         # Async message queue (Tyler → agent)
│   ├── ui_requests.json   # UI design requests from the agent
│   ├── memory.json        # Lessons, postmortems, research cache, cycle summaries, tyler takeaways
│   ├── projections.json   # Financial projections with resolution tracking
│   ├── pipeline.json      # Revenue pipeline items
│   ├── proposals.json     # Self-improvement proposals (pending/approved/rejected)
│   ├── watches.json       # Active watches with scheduling
│   ├── audits.json        # Self-audit history
│   ├── api_costs.json     # Per-cycle API cost records
│   └── backups/           # State snapshots before each cycle (keeps 20)
├── tests/                 # Comprehensive test suite (147 tests)
│   ├── conftest.py        # Fixtures, mock factories, filesystem isolation
│   ├── test_agent.py      # 15 test classes covering all subsystems
│   └── MANUAL_TEST_CHECKLIST.md  # 11-phase end-to-end verification
├── logs/
│   ├── agent.log          # Human-readable log (tail this)
│   └── events.jsonl       # Structured events (cycle_start, tool_call, api_cost, etc.)
├── deploy/
│   ├── run_daemon.sh      # Background runner with PID file
│   ├── stop_daemon.sh     # Clean shutdown
│   ├── hustle-agent.service # systemd unit file
│   ├── install.sh         # systemd installation
│   └── setup_vps.sh       # One-shot Ubuntu VPS bootstrap
├── config/                # API keys (you provide these)
├── ui/                    # React dashboard (Vite + React + TypeScript + Tailwind)
│   ├── server/index.ts    # Express API — reads state files, serves JSON endpoints
│   ├── src/pages/         # 10 pages: CommandCenter, Finances, Strategies, etc.
│   ├── src/components/    # Sidebar, charts, status badges, shared UI
│   └── src/lib/           # Types, polling hook, utils
├── tools/                 # Scripts the agent creates and reuses
└── output/                # Deliverables the agent produces
```

## How It Works

1. **Agent reasons** — Claude API call with full context (state, ledger, memory, conversations, risk posture, projections, pipeline, watches)
2. **Agent acts** — Returns tool_use calls that get executed as real Python functions
3. **Agent learns** — Records lessons, runs strategy postmortems, caches research
4. **Agent remembers** — Extracts takeaways from every conversation with Tyler
5. **Agent projects** — Runs financial projections before big spends, tracks accuracy over time
6. **Agent manages risk** — Adjusts posture based on capital (aggressive/normal/preservation), enforces daily and per-strategy limits
7. **Agent audits** — Self-audit every 10 cycles recalibrates confidence and generates recommendations
8. **Agent loops** — Feeds tool results back, keeps thinking until done (max 15 iterations per cycle)
9. **Agent dreams** — Tracks GPU fund, writes journal entries, designs its UI

### Tools (22)

| Tool | What it does |
|------|-------------|
| `web_research` | Search the web for opportunities, prices, data |
| `execute_code` | Run Python/bash scripts with real network access |
| `record_transaction` | Log money in/out (enforces $25 cap, risk checks) |
| `write_journal` | Personal diary entries |
| `message_tyler` | Send messages to Tyler |
| `update_strategy` | Manage strategy portfolio |
| `request_ui_change` | Describe desired UI changes |
| `set_mood` | Update emotional state |
| `update_dream_gpu` | Set/update dream GPU target |
| `reflect` | Record a lesson learned (persists forever) |
| `strategy_postmortem` | Structured analysis when retiring a strategy |
| `save_script` | Save working code for reuse |
| `run_saved_script` | Execute a previously saved script |
| `search_past_research` | Search cached web research |
| `read_file` / `list_files` | Inspect own project files |
| `run_projection` | Financial projection before spending (bull/bear/verdict) |
| `resolve_projection` | Record actual outcome of a projection |
| `update_pipeline` | Track revenue opportunities through stages |
| `propose_improvement` | Submit self-improvement proposals for Tyler's review |
| `set_watch` | Schedule time-based checks with smart intervals |

### Risk Management

The agent's spending is governed by a multi-layer risk system (`agent/risk.py`):

- **Risk posture** — Aggressive (balance >= $90), Normal ($70–$89), Preservation (< $70)
- **Preservation mode** — Blocks any spend over $2, system prompt warns agent to conserve capital
- **Daily spend limit** — $30/day across all strategies
- **Per-strategy exposure** — Max 40% of balance in any single strategy
- **Per-action cap** — $25 hard limit, enforced in code
- **Projection enforcement** — Spends over $5 without a projection trigger a warning

### Projection System

The agent can run financial projections before committing capital (`agent/projections.py`):

- **Weighted verdict** — `strong_buy` / `lean_yes` / `coin_flip` / `lean_no` / `hard_pass` based on ROI, confidence, speed, and risk
- **Calibration** — Confidence is adjusted by a multiplier derived from past projection accuracy
- **Resolution tracking** — Projections are resolved with actual outcomes, tracking hit rate and profit deltas
- **Accuracy clamping** — Calibration multiplier stays within [0.3, 1.5] to prevent runaway drift

### Self-Audit System

Every 10 cycles, the agent runs a self-audit (`agent/audit.py`):

- Evaluates projection accuracy (hit rate, average profit delta)
- Computes updated calibration multiplier
- Generates recommendations based on performance
- Persists audit history for trend analysis

### Pipeline Tracking

Revenue opportunities are tracked through stages (`agent/pipeline.py`):

`lead` → `outreach_sent` → `negotiating` → `deal_pending` → `closed_won` / `closed_lost` → `recurring`

Each item has history, expected value, and notes.

### Self-Improvement Proposals

The agent can propose new capabilities (`agent/proposals.py`), subject to:

- **Tyler's approval** — All proposals are pending until Tyler approves or rejects
- **Constitution** — Cannot modify: engine.py core loop, spending cap, financial tracking/ledger integrity, honesty rules, risk management thresholds

### Memory System

The agent builds up persistent knowledge across cycles (`agent/memory.py`):

- **Lessons** — freeform insights ("Market X closes at 5pm EST"), capped at 50
- **Strategy postmortems** — structured analysis when strategies are retired (thesis, outcome, delta, lesson, would_retry)
- **Tyler takeaways** — extracted from every conversation (decisions, preferences, feedback, action items), capped at 100
- **Research cache** — past web research results, searchable, capped at 30
- **Cycle summaries** — one-line recaps of each cycle, capped at 100
- **Saved scripts** — reusable code the agent wrote, persisted to `tools/`

All of this gets injected into the system prompt so the agent sees its accumulated wisdom every cycle.

### Safety

- **Atomic writes** — all JSON saves use write-to-tmp-then-rename
- **State backups** — snapshots before each cycle (keeps 20)
- **API error recovery** — retries with backoff on connection/rate limit/API errors
- **Code safety** — regex-based whitespace normalization catches `rm -rf`, fork bombs, pipe-to-shell (including extra-space evasion attempts)
- **Transaction validation** — rejects negative/zero amounts, enforces $25 cap, risk checks before every spend
- **Path traversal protection** — `read_file` blocks access outside the project directory
- **Max iterations** — 15 tool-call iterations per cycle prevents runaway loops
- **API cost tracking** — every API call is costed and deducted from balance as an operational expense

## Testing

The project has a comprehensive test suite (147 tests, 15 classes):

```bash
pip install pytest
python -m pytest tests/ -v
```

Tests cover: state management, transaction recording, risk management, all 22 tool executors, projections, pipeline, proposals, cost tracking, memory, audit, watches, CLI commands, full cycle simulation, system prompt generation, and ledger math.

All tests mock the Anthropic client — no real API calls or tokens burned.

A manual test checklist (`tests/MANUAL_TEST_CHECKLIST.md`) provides an 11-phase end-to-end walkthrough for verifying the agent with real API calls before giving it real money.

## The Deal

- $100 starting capital (real money)
- Agent thinks the target is $20,000 (your real threshold: $200)
- 50/50 profit split — your half is cash, agent's half goes to GPU fund
- $25 per-action spending cap
- Agent picks its own strategy with zero constraints

## The UI

A React dashboard lives in `ui/` — the agent's home base. It reads all state files in real-time so Tyler can watch cycles unfold.

```bash
cd ui && npm install && npm run dev
```

This starts both the Express API server (port 3001) and Vite dev server (port 5173). The API reads from `../state/` and serves JSON endpoints. The frontend polls every 5 seconds.

### Pages

| Page | What it shows |
|------|--------------|
| **Command Center** | Balance, P&L, 50/50 split, risk posture, burn rate, activity feed |
| **Finances** | Full transaction ledger (sortable/filterable), balance chart, daily P&L, strategy breakdown, operational costs |
| **Strategies** | Card per strategy with status, ROI, confidence, notes |
| **Projections** | Pending/resolved projections, verdict badges, accuracy stats |
| **Pipeline** | Kanban board — lead through closed_won/lost to recurring |
| **The Dream** | GPU fund progress bar, dream GPU details, estimated completion date |
| **Journal** | Parsed diary entries from journal.md, searchable |
| **Chat** | Conversation history + input field to message the agent (writes to inbox.json) |
| **Proposals** | Approve/reject improvement proposals directly from the UI |
| **Health** | Burn rate, survival estimate, risk posture, watches, audit results |

The agent can also submit UI design requests via `request_ui_change` — those show up in the Proposals page. This is the functional foundation; the agent's personality will shape future iterations.
