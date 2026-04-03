export interface DreamGpu {
  name: string;
  description: string;
  estimated_cost: number;
  why: string;
}

export interface Strategy {
  name: string;
  status: string;
  description: string;
  invested: number;
  returned: number;
  confidence: number;
  notes: string;
  created_at: string;
  updated_at: string;
}

export interface Avatar {
  name: string;
  creature: string;
  description: string;
}

export interface AgentState {
  name: string;
  balance: number;
  target: number;
  cycle: number;
  status: string;
  mood: string;
  avatar: Avatar;
  active_strategies: string[];
  total_earned: number;
  total_spent: number;
  net_profit: number;
  roi_percent: number;
  tylers_cut: number;
  gpu_fund: number;
  dream_gpu: DreamGpu;
  gpu_fund_progress_percent: number;
  strategies: Strategy[];
  created_at: string;
  last_updated: string;
}

export interface Transaction {
  id: number;
  timestamp: string;
  type: 'income' | 'expense' | 'investment' | 'return';
  amount: number;
  description: string;
  strategy: string;
  balance_after: number;
  reasoning: string;
  tags: string[];
}

export interface Conversation {
  timestamp: string;
  from: 'agent' | 'tyler';
  message: string;
}

export interface PipelineItem {
  id: number;
  name: string;
  stage: string;
  strategy: string;
  description: string;
  expected_value: number;
  expected_close_date: string;
  notes: string;
  created_at: string;
  updated_at: string;
  history: { from: string; to: string; at: string }[];
}

export interface DataBacking {
  source: string;
  data_point: string;
  source_probability: number;
  market_price: number;
  edge: number;
  edge_direction: string;
  source_url: string;
  retrieved_at: string;
}

export interface TransactionReport {
  report_id: string;
  timestamp: string;
  type: 'expense' | 'income' | 'investment' | 'return';
  summary: {
    action: string;
    amount: number;
    outcome: 'pending' | 'won' | 'lost';
    profit_loss: number | null;
    balance_after: number;
  };
  reasoning: {
    strategy: string;
    thesis: string;
    confidence_raw: number | null;
    confidence_adjusted: number | null;
    calibration_applied: string | null;
    instinct_warnings: string[];
    risk_posture_at_time: string | null;
    exploration_mode: string | null;
  };
  data_backing: DataBacking | null;
  projection: {
    projection_id: string;
    expected_return: number;
    expected_profit: number;
    roi_percent: number;
    time_to_return_days: number;
    verdict_raw: string;
    verdict_adjusted: string;
    bull_case: string;
    bear_case: string;
  } | null;
  resolution: {
    resolved_at: string;
    actual_outcome: string;
    actual_return: number;
    actual_profit_loss: number;
    prediction_delta: number;
    notes: string | null;
  } | null;
  linked_ids: {
    ledger_id: number;
    action_id: string | null;
    projection_id: string | null;
    kalshi_order_id: string | null;
  };
}

export interface Projection {
  id: string;
  timestamp: string;
  action: string;
  cost: number;
  strategy_type: string;
  expected_return: number;
  expected_profit: number;
  roi_percent: number;
  time_to_return_days: number;
  confidence_raw: number;
  confidence_calibrated: number;
  calibration_multiplier: number;
  assumptions: string[];
  risks: string[];
  comparables: string;
  bull_case: string;
  bear_case: string;
  research_summary: string;
  data_backing?: DataBacking;
  operational_overhead: number;
  capital_velocity_cost: number;
  verdict: string;
  status: string;
  resolution?: {
    timestamp: string;
    actual_outcome: string;
    actual_return: number;
    actual_profit: number;
    actual_time_days: number;
    profit_delta: number;
    time_delta: number;
    hit: boolean;
  };
}

export interface Proposal {
  id: number;
  name: string;
  description: string;
  why_needed: string;
  proposed_tool_schema: string;
  proposed_execution_logic: string;
  status: string;
  feedback: string;
  submitted_at: string;
  resolved_at: string;
}

export interface UiRequest {
  id: number;
  timestamp: string;
  request: string;
  status: string;
  design_notes: string;
}

export interface Watch {
  id: number;
  condition: string;
  action_hint: string;
  check_after: string;
  expires_at: string;
  projection_id: string;
  status: string;
  created_at: string;
  triggered_at: string;
}

export interface AuditResult {
  cycle: number;
  timestamp: string;
  projection_accuracy: {
    count: number;
    hits: number;
    avg_confidence: number;
    actual_hit_rate: number;
    calibration_multiplier: number;
    avg_time_error_days: number;
  };
  strategy_trends: Record<string, {
    invested: number;
    returned: number;
    roi: number;
    transactions: number;
  }>;
  operational_efficiency: {
    total_api_cost: number;
    total_earned: number;
    cost_per_dollar_earned: number;
    avg_cycle_cost: number;
    cycles_tracked: number;
  };
  pipeline_health: {
    active_count: number;
    stale_count: number;
    total_expected_value: number;
  };
  recommendations: string[];
}

export interface CostEntry {
  cycle: number;
  model: string;
  input_tokens: number;
  output_tokens: number;
  cost: number;
  operation: string;
}

export interface AgentEvent {
  timestamp: string;
  event_type: string;
  cycle: number;
  data: Record<string, unknown>;
}

export interface Action {
  action_id: string;
  projection_id: string | null;
  timestamp: string;
  category: string;
  subcategory: string;
  cost: number;
  conditions: {
    time_horizon_days: number;
    market_odds: number | null;
    confidence_at_decision: number;
    capital_percentage: number;
    time_of_day: string;
    day_of_week: string;
    risk_posture_at_time: string;
    balance_at_time: number;
  };
  expected_return: number;
  status: 'pending' | 'won' | 'lost' | 'partial' | 'expired';
  actual_return: number | null;
  actual_time_days: number | null;
  resolved_at: string | null;
}

export interface CategoryScore {
  win_rate: number;
  avg_roi: number;
  avg_return_time_days: number;
  capital_efficiency: number;
  trend: string;
  confidence_calibration_gap: number;
  sample_size: number;
}

export interface CrossPattern {
  combo: string;
  key: string;
  win_rate: number;
  avg_roi: number;
  sample_size: number;
  signal_strength: number;
  description: string;
}

export interface InstinctsData {
  last_computed: string;
  action_count_at_compute: number;
  exploration_mode: 'explore' | 'exploit';
  category_scores: Record<string, CategoryScore>;
  dimension_scores: Record<string, Record<string, { win_rate: number; avg_roi: number; sample_size: number }>>;
  cross_patterns: CrossPattern[];
  calibration: { overall: number; per_category: Record<string, number> };
  instinct_sentences: string[];
  history: { timestamp: string; sentences: string[]; overall_calibration: number; action_count: number }[];
}

export interface PriorEntry {
  win_rate: number;
  avg_roi: number;
  source: 'default' | 'research' | 'earned';
  validated: boolean;
  research_date: string | null;
  note: string;
}

export type PriorsData = Record<string, PriorEntry>;

export interface MemoryData {
  lessons: { text: string; timestamp: string; cycle: number }[];
  strategy_postmortems: { strategy: string; thesis: string; outcome: string; profit_delta: number; lesson: string; would_retry: boolean; cycle: number }[];
  tyler_takeaways: { takeaway: string; type: string; cycle: number }[];
  research_cache: { query: string; results: string; timestamp: string }[];
  cycle_summaries: { cycle: number; summary: string }[];
}
