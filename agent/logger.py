"""
Hustle Agent — Structured Logging

Two outputs:
- logs/agent.log — human-readable, for tailing
- logs/events.jsonl — structured JSON lines, for machine parsing
"""

import json
import logging
import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

# Human-readable log (file + stdout)
_logger = logging.getLogger("hustle_agent")
_logger.setLevel(logging.DEBUG)

_file_handler = logging.FileHandler(LOG_DIR / "agent.log")
_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
_logger.addHandler(_file_handler)

_stdout_handler = logging.StreamHandler()
_stdout_handler.setFormatter(logging.Formatter("%(message)s"))
_logger.addHandler(_stdout_handler)

# Machine-readable events
EVENTS_FILE = LOG_DIR / "events.jsonl"


def log_event(event_type: str, cycle: int = 0, **data):
    """Write a structured event to events.jsonl."""
    event = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "event_type": event_type,
        "cycle": cycle,
        "data": data
    }
    with open(EVENTS_FILE, "a") as f:
        f.write(json.dumps(event) + "\n")


def info(msg: str):
    _logger.info(msg)


def error(msg: str):
    _logger.error(msg)


def cycle_start(cycle: int, name: str, balance: float, gpu_fund: float):
    info(f"\n{'='*60}")
    info(f"  CYCLE {cycle} -- {name}")
    info(f"  Balance: ${balance:.2f} | GPU Fund: ${gpu_fund:.2f}")
    info(f"{'='*60}\n")
    log_event("cycle_start", cycle=cycle, name=name, balance=balance, gpu_fund=gpu_fund)


def cycle_end(cycle: int, balance: float, earned: float, spent: float,
              gpu_fund: float, mood: str, summary: str):
    info(f"\n{'='*60}")
    info(f"  CYCLE {cycle} COMPLETE")
    info(f"  Balance: ${balance:.2f} | Earned: ${earned:.2f} | Spent: ${spent:.2f}")
    info(f"  GPU Fund: ${gpu_fund:.2f} | Mood: {mood}")
    info(f"  Summary: {summary}")
    info(f"{'='*60}\n")
    log_event("cycle_end", cycle=cycle, balance=balance, earned=earned,
              spent=spent, gpu_fund=gpu_fund, mood=mood, summary=summary)


def thinking(iteration: int):
    _logger.info(f"  [Think #{iteration}]")


def thought(text: str):
    _logger.info(f"  {text[:200]}")


def tool_use(name: str, input_preview: str):
    _logger.info(f"  {name}: {input_preview[:150]}")


def tool_ok(result: str):
    _logger.info(f"  OK: {result[:150]}")


def tool_fail(result: str):
    _logger.error(f"  FAIL: {result[:150]}")


def message_received(source: str, content: str, cycle: int = 0):
    log_event("message_received", cycle=cycle, source=source, content=content[:200])


def message_sent(content: str, cycle: int = 0):
    log_event("message_sent", cycle=cycle, content=content[:200])


def api_cost(model: str, input_tokens: int, output_tokens: int,
             cost: float, purpose: str, cycle: int = 0):
    log_event("api_cost", cycle=cycle, model=model, input_tokens=input_tokens,
              output_tokens=output_tokens, cost=cost, purpose=purpose)


def projection_created(projection_id: str, action: str, verdict: str, cycle: int = 0):
    log_event("projection_created", cycle=cycle, projection_id=projection_id,
              action=action[:100], verdict=verdict)


def projection_resolved(projection_id: str, hit: bool, delta: float, cycle: int = 0):
    log_event("projection_resolved", cycle=cycle, projection_id=projection_id,
              hit=hit, profit_delta=delta)


def self_audit(cycle: int, calibration: float, recommendations: list):
    log_event("self_audit", cycle=cycle, calibration=calibration,
              recommendations=recommendations[:5])


def proposal_submitted(proposal_id: int, name: str, cycle: int = 0):
    log_event("proposal_submitted", cycle=cycle, proposal_id=proposal_id, name=name)


def risk_check(allowed: bool, reason: str, strategy: str,
               amount: float, cycle: int = 0):
    log_event("risk_check", cycle=cycle, allowed=allowed, reason=reason,
              strategy=strategy, amount=amount)


def pipeline_update(name: str, stage: str, cycle: int = 0):
    log_event("pipeline_update", cycle=cycle, name=name, stage=stage)


def watch_triggered(watch_id: int, condition: str, cycle: int = 0):
    log_event("watch_triggered", cycle=cycle, watch_id=watch_id,
              condition=condition[:100])
