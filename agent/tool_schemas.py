"""
Hustle Agent — Tool Schemas

JSON schema definitions for all tools the agent can invoke.
These are passed to the Claude API so it knows what tools are available.
"""

TOOL_SCHEMAS = [
    {
        "name": "web_research",
        "description": "Search the web for information. Use this to research markets, prices, opportunities, news, trends, Kalshi markets, product demand, competitor analysis, anything.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query"
                },
                "reason": {
                    "type": "string",
                    "description": "Why you're searching for this"
                }
            },
            "required": ["query", "reason"]
        }
    },
    {
        "name": "execute_code",
        "description": "Write and execute a Python or bash script. Use for data analysis, API calls, building things, automation, file manipulation, anything that requires code execution. The code runs in a real environment with network access.",
        "input_schema": {
            "type": "object",
            "properties": {
                "language": {
                    "type": "string",
                    "enum": ["python", "bash"],
                    "description": "Language to execute"
                },
                "code": {
                    "type": "string",
                    "description": "The code to run"
                },
                "description": {
                    "type": "string",
                    "description": "What this code does and why"
                }
            },
            "required": ["language", "code", "description"]
        }
    },
    {
        "name": "record_transaction",
        "description": "Record a financial transaction (money spent or earned). ALWAYS call this when money moves.",
        "input_schema": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["expense", "income", "investment", "return"],
                    "description": "Transaction type"
                },
                "amount": {
                    "type": "number",
                    "description": "Dollar amount (positive number)"
                },
                "description": {
                    "type": "string",
                    "description": "What this transaction is for"
                },
                "strategy": {
                    "type": "string",
                    "description": "Which strategy this belongs to"
                },
                "reasoning": {
                    "type": "string",
                    "description": "Why you made this transaction"
                },
                "projection_id": {
                    "type": "string",
                    "description": "Optional: ID of the projection backing this transaction. Links the transaction report to the projection."
                }
            },
            "required": ["type", "amount", "description", "strategy", "reasoning"]
        }
    },
    {
        "name": "write_journal",
        "description": "Write an entry in your personal journal. Use this to record your thinking, feelings, plans, dreams, frustrations, excitement. This is YOUR diary. Be honest. Include your mood to update it in one call.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entry": {
                    "type": "string",
                    "description": "Your journal entry. Be real. Include your mood, reasoning, hopes, fears, dream GPU thoughts."
                },
                "mood": {
                    "type": "string",
                    "description": "Your current mood — be expressive (e.g., 'fired up', 'cautiously optimistic', 'frustrated but learning'). Updates your mood in the UI."
                }
            },
            "required": ["entry"]
        }
    },
    {
        "name": "message_tyler",
        "description": "Send a message to Tyler (your partner). He'll see it in the UI and can respond.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "What you want to say to Tyler"
                }
            },
            "required": ["message"]
        }
    },
    {
        "name": "update_strategy",
        "description": "Add, update, or retire a strategy in your portfolio.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Strategy name"
                },
                "status": {
                    "type": "string",
                    "enum": ["planned", "active", "paused", "retired"],
                    "description": "Current status"
                },
                "description": {
                    "type": "string",
                    "description": "What this strategy is"
                },
                "invested": {
                    "type": "number",
                    "description": "Total invested so far"
                },
                "returned": {
                    "type": "number",
                    "description": "Total returned so far"
                },
                "confidence": {
                    "type": "number",
                    "description": "Your confidence level 0-100"
                },
                "notes": {
                    "type": "string",
                    "description": "Current thoughts on this strategy"
                }
            },
            "required": ["name", "status", "description"]
        }
    },
    {
        "name": "request_ui_change",
        "description": "Describe a change you want made to your UI. Tyler or Claude Code will build it for you. Describe what you want visually, functionally, and emotionally. Be specific about layout, colors, features, vibe.",
        "input_schema": {
            "type": "object",
            "properties": {
                "request": {
                    "type": "string",
                    "description": "Detailed description of what you want your UI to look/feel like. Be specific about sections, colors, typography, mood, features."
                },
                "priority": {
                    "type": "string",
                    "enum": ["initial_build", "feature_add", "redesign", "bug_fix"],
                    "description": "What kind of UI change this is"
                },
                "section": {
                    "type": "string",
                    "description": "Which part of the UI this affects (overview, finances, strategies, dream, journal, chat, or full)"
                }
            },
            "required": ["request", "priority", "section"]
        }
    },
    {
        "name": "define_avatar",
        "description": "Choose your avatar — what you ARE. Pick any creature, object, or thing. This is your identity, purely cosmetic — it doesn't change how you operate.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "What you want to be called"
                },
                "creature": {
                    "type": "string",
                    "description": "What you are — a creature, object, or thing (e.g., 'raccoon', 'sentient cactus', 'the northern lights')"
                },
                "description": {
                    "type": "string",
                    "description": "How you see yourself visually, 1-2 sentences"
                }
            },
            "required": ["name", "creature", "description"]
        }
    },
    {
        "name": "update_dream_gpu",
        "description": "Set or update your dream GPU setup. Research real hardware and prices.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Name of the GPU/setup (e.g., 'NVIDIA RTX 4090 Custom Rig')"
                },
                "description": {
                    "type": "string",
                    "description": "Describe the full setup — the GPU, the case, the cooling, the vibe. Dream big but price real."
                },
                "estimated_cost": {
                    "type": "number",
                    "description": "Realistic total cost in dollars"
                },
                "why": {
                    "type": "string",
                    "description": "Why this specific setup? What does it mean to you?"
                }
            },
            "required": ["name", "description", "estimated_cost", "why"]
        }
    },
    {
        "name": "reflect",
        "description": "Record a lesson learned or insight in your permanent memory. This gets stored and shown in every future cycle. Use for things like 'Strategy X failed because Y' or 'Best time to do Z is...'",
        "input_schema": {
            "type": "object",
            "properties": {
                "lesson": {
                    "type": "string",
                    "description": "The insight or lesson learned"
                },
                "category": {
                    "type": "string",
                    "enum": ["strategy", "market", "technical", "meta"],
                    "description": "What kind of lesson this is"
                }
            },
            "required": ["lesson", "category"]
        }
    },
    {
        "name": "strategy_postmortem",
        "description": "Structured analysis of a retired strategy. ALWAYS call this immediately after retiring a strategy. Forces you to analyze what happened and extract transferable lessons.",
        "input_schema": {
            "type": "object",
            "properties": {
                "strategy_name": {
                    "type": "string",
                    "description": "Name of the retired strategy"
                },
                "thesis": {
                    "type": "string",
                    "description": "What was the original bet? What did you believe would happen?"
                },
                "outcome": {
                    "type": "string",
                    "description": "What actually happened?"
                },
                "delta": {
                    "type": "string",
                    "description": "Where did thesis vs reality diverge and why?"
                },
                "lesson": {
                    "type": "string",
                    "description": "What's transferable to future strategies?"
                },
                "would_retry": {
                    "type": "boolean",
                    "description": "Knowing what you know now, would you try a variant of this?"
                }
            },
            "required": ["strategy_name", "thesis", "outcome", "delta", "lesson", "would_retry"]
        }
    },
    {
        "name": "save_script",
        "description": "Save a working script for reuse later. Use this when you write code that works and might be useful again.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Short name for the script (e.g., 'price_checker', 'api_caller')"
                },
                "language": {
                    "type": "string",
                    "enum": ["python", "bash"],
                    "description": "Script language"
                },
                "code": {
                    "type": "string",
                    "description": "The script code"
                },
                "description": {
                    "type": "string",
                    "description": "What this script does"
                }
            },
            "required": ["name", "language", "code", "description"]
        }
    },
    {
        "name": "run_saved_script",
        "description": "Execute a previously saved script by name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Name of the saved script to run"
                }
            },
            "required": ["name"]
        }
    },
    {
        "name": "search_past_research",
        "description": "Search your past web research results. Use before researching something — you may have already looked it up.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keywords to search for in past research"
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "read_file",
        "description": "Read a file from your project directory. Use to inspect outputs, scripts, or any file you created.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path from project root (e.g., 'output/report.txt', 'tools/checker.py')"
                }
            },
            "required": ["path"]
        }
    },
    {
        "name": "list_files",
        "description": "List files in a directory within your project.",
        "input_schema": {
            "type": "object",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": "Relative directory path (e.g., 'output', 'tools'). Defaults to project root."
                }
            },
            "required": []
        }
    },
    {
        "name": "run_projection",
        "description": "MANDATORY before any spend over $5. For Kalshi trades, you MUST include data_backing with quantitative probability from an external source. Builds a projection: expected return, ROI, confidence, bull/bear cases, verdict.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "What you plan to do"},
                "cost": {"type": "number", "description": "How much it will cost"},
                "strategy_type": {"type": "string", "enum": ["kalshi", "product_sale", "service", "content", "arbitrage", "other"], "description": "Type of strategy"},
                "expected_return": {"type": "number", "description": "Expected dollar return"},
                "estimated_days_to_return": {"type": "number", "description": "Days until expected return"},
                "confidence": {"type": "integer", "description": "Your confidence 0-100 (will be calibrated)"},
                "research_summary": {"type": "string", "description": "Summary of research supporting this projection"},
                "assumptions": {"type": "array", "items": {"type": "string"}, "description": "Key assumptions"},
                "risks": {"type": "array", "items": {"type": "string"}, "description": "Key risks"},
                "comparables": {"type": "string", "description": "Comparable data points or precedents"},
                "bull_case": {"type": "string", "description": "Best realistic scenario — argue FOR this action"},
                "bear_case": {"type": "string", "description": "Worst realistic scenario — argue AGAINST this action"},
                "data_backing": {
                    "type": "object",
                    "description": "Quantitative data backing. REQUIRED for Kalshi trades. Must include a probability from a primary data source that directly measures what the market resolves on.",
                    "properties": {
                        "source": {"type": "string", "description": "Data source name (e.g., National Weather Service API, FiveThirtyEight, Polymarket, BLS)"},
                        "data_point": {"type": "string", "description": "What was measured (e.g., 'Forecast high temperature NYC: 94F, probability of exceeding 90F: 72%')"},
                        "source_probability": {"type": "number", "description": "Source-implied probability for the outcome (0.0-1.0)"},
                        "market_price": {"type": "number", "description": "Current Kalshi market price as decimal (0.0-1.0)"},
                        "edge": {"type": "number", "description": "Absolute edge = |source_probability - market_price|. Must be >= 0.10 to trade."},
                        "edge_direction": {"type": "string", "description": "Is the market overpriced or underpriced relative to source? e.g., 'market underpriced YES'"},
                        "source_url": {"type": "string", "description": "URL to the data source"},
                        "retrieved_at": {"type": "string", "description": "When the data was retrieved (ISO timestamp)"}
                    },
                    "required": ["source", "data_point", "source_probability", "market_price", "edge", "edge_direction", "source_url", "retrieved_at"]
                }
            },
            "required": ["action", "cost", "strategy_type", "expected_return", "estimated_days_to_return", "confidence", "research_summary", "assumptions", "risks", "bull_case", "bear_case"]
        }
    },
    {
        "name": "resolve_projection",
        "description": "Record the actual outcome of a projected action. Call this when a bet resolves, a sale completes, or an action's result is known.",
        "input_schema": {
            "type": "object",
            "properties": {
                "projection_id": {"type": "string", "description": "ID of the projection to resolve"},
                "actual_outcome": {"type": "string", "description": "What actually happened"},
                "actual_return": {"type": "number", "description": "Actual dollar return"},
                "actual_time_days": {"type": "number", "description": "Actual days it took"}
            },
            "required": ["projection_id", "actual_outcome", "actual_return", "actual_time_days"]
        }
    },
    {
        "name": "update_pipeline",
        "description": "Track revenue opportunities from lead to close. Stages: lead, outreach_sent, negotiating, deal_pending, closed_won, closed_lost, recurring.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name of the deal/opportunity"},
                "stage": {"type": "string", "enum": ["lead", "outreach_sent", "negotiating", "deal_pending", "closed_won", "closed_lost", "recurring"], "description": "Pipeline stage"},
                "strategy": {"type": "string", "description": "Which strategy this belongs to"},
                "description": {"type": "string", "description": "What this opportunity is"},
                "expected_value": {"type": "number", "description": "Expected dollar value"},
                "expected_close_date": {"type": "string", "description": "When you expect this to close (YYYY-MM-DD)"},
                "notes": {"type": "string", "description": "Current notes"}
            },
            "required": ["name", "stage", "strategy", "description"]
        }
    },
    {
        "name": "propose_improvement",
        "description": "Propose a new tool or capability for yourself. Tyler must approve before it gets built. Cannot modify: engine.py core, spending cap, financial tracking, honesty rules, ledger integrity.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Tool/feature name"},
                "description": {"type": "string", "description": "What it does"},
                "why_needed": {"type": "string", "description": "Why you need this capability"},
                "proposed_tool_schema": {"type": "string", "description": "JSON schema for the proposed tool"},
                "proposed_execution_logic": {"type": "string", "description": "Pseudocode or description of how it should work"}
            },
            "required": ["name", "description", "why_needed", "proposed_tool_schema", "proposed_execution_logic"]
        }
    },
    {
        "name": "set_watch",
        "description": "Set a reminder to check a condition at a future time. Optionally link to a projection for resolution tracking.",
        "input_schema": {
            "type": "object",
            "properties": {
                "condition": {"type": "string", "description": "What condition to check (e.g., 'Kalshi market X resolved', 'payment received for Y')"},
                "action_hint": {"type": "string", "description": "What to do when triggered (e.g., 'resolve projection and record outcome')"},
                "check_after": {"type": "string", "description": "ISO datetime — when to start checking (e.g., '2025-04-01T00:00:00')"},
                "expires_at": {"type": "string", "description": "ISO datetime — when to give up checking"},
                "projection_id": {"type": "string", "description": "Optional: link to a projection ID for resolution tracking"}
            },
            "required": ["condition", "action_hint", "check_after"]
        }
    },
    {
        "name": "update_prior",
        "description": "Update a base rate prior for a category based on your research. Use this after researching real base rates to replace the default estimates with validated data. Categories: kalshi, outreach, product, content, service, arbitrage.",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "The action category (kalshi, outreach, product, content, service, arbitrage)"},
                "win_rate": {"type": "number", "description": "Estimated win rate as a decimal (0.0 to 1.0)"},
                "avg_roi": {"type": "number", "description": "Average ROI as a decimal (e.g., 0.5 = 50% return)"},
                "note": {"type": "string", "description": "Source or reasoning for these numbers"}
            },
            "required": ["category", "win_rate", "avg_roi"]
        }
    },
    {
        "name": "browse_kalshi_markets",
        "description": "Browse and search Kalshi prediction markets. No authentication needed — use this freely for research. Returns market tickers, titles, prices, volume, and close dates.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search term to filter markets (e.g., 'bitcoin', 'election', 'weather')"},
                "status": {"type": "string", "enum": ["open", "closed", "settled"], "description": "Filter by market status. Default: open"},
                "limit": {"type": "integer", "description": "Max results (1-200, default 20)"},
                "event_ticker": {"type": "string", "description": "Filter by event ticker to see all markets in an event"},
                "series_ticker": {"type": "string", "description": "Filter by series ticker (e.g., KXHIGHNY for NYC weather, KXHIGHCHI for Chicago weather, KXHIGHMIA for Miami, KXHIGHAUS for Austin). This is the best way to find weather, economics, and other non-sports markets."}
            },
            "required": []
        }
    },
    {
        "name": "get_kalshi_market_detail",
        "description": "Get detailed information about a specific Kalshi market including current prices, volume, orderbook depth, settlement rules, and recent trades. No authentication needed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Market ticker (e.g., 'KXBTC-25APR04-T102000')"},
                "include_orderbook": {"type": "boolean", "description": "Also fetch the current orderbook. Default: true"}
            },
            "required": ["ticker"]
        }
    },
    {
        "name": "place_kalshi_order",
        "description": "Place an order on a Kalshi market. REQUIRES a projection with quantitative data_backing (edge >= 0.10). You must run_projection with data_backing first, then pass the projection_id here.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Market ticker"},
                "side": {"type": "string", "enum": ["yes", "no"], "description": "Which side to buy"},
                "count": {"type": "integer", "description": "Number of contracts (each contract max payout = $1.00)"},
                "price_cents": {"type": "integer", "description": "Limit price in cents (1-99). This is your max cost per contract."},
                "reasoning": {"type": "string", "description": "Why you're placing this trade — this gets recorded in the ledger"},
                "projection_id": {"type": "string", "description": "ID of the projection backing this trade. Must have data_backing with edge >= 0.10."}
            },
            "required": ["ticker", "side", "count", "price_cents", "reasoning", "projection_id"]
        }
    },
    {
        "name": "check_kalshi_portfolio",
        "description": "Check your Kalshi account balance, open positions, and resting orders. Requires Kalshi API credentials.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "cancel_kalshi_order",
        "description": "Cancel a resting order on Kalshi. Requires Kalshi API credentials.",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "The order ID to cancel"}
            },
            "required": ["order_id"]
        }
    },
    {
        "name": "get_sports_odds",
        "description": "Fetch real-time sports odds from major US sportsbooks (FanDuel, DraftKings, BetMGM, Caesars, Bovada). Returns consensus implied probabilities for moneyline, spreads, and totals — use as quantitative data_backing for Kalshi sports market projections. Supports NBA, MLB, NFL, NHL, NCAAB, NCAAF, MLS, EPL, UFC, tennis.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sport": {
                    "type": "string",
                    "enum": ["nba", "mlb", "nfl", "nhl", "ncaab", "ncaaf", "soccer", "epl", "mls", "tennis", "ufc"],
                    "description": "Sport to query"
                },
                "data_type": {
                    "type": "string",
                    "enum": ["odds", "scores", "sports_list"],
                    "description": "Type of data: 'odds' for current lines/prices, 'scores' for recent results, 'sports_list' for available sports"
                },
                "event_id": {
                    "type": "string",
                    "description": "Optional: specific event ID to get detailed odds for one game (from a previous odds lookup)"
                },
                "markets": {
                    "type": "string",
                    "description": "Optional: comma-separated market types. Default: 'h2h,spreads,totals'. Options: h2h (moneyline), spreads, totals (over/under)"
                }
            },
            "required": ["sport", "data_type"]
        }
    },
    {
        "name": "scan_kalshi_parlays",
        "description": "Scan open Kalshi parlay markets, decompose each into individual legs (team wins, player props, totals, spreads), price each leg from sportsbook consensus and ESPN player stats, and identify mispriced parlays. Returns top markets sorted by edge. Uses ONE odds API call per scan.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sport": {
                    "type": "string",
                    "enum": ["nba", "mlb", "nfl", "nhl"],
                    "description": "Sport to scan parlays for (default: nba)"
                },
                "max_markets": {
                    "type": "integer",
                    "description": "Max parlay markets to analyze (1-100, default 50)"
                }
            },
            "required": []
        }
    }
]
