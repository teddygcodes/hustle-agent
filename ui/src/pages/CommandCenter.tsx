import { usePolling } from '../lib/usePolling';
import type { AgentState, AgentEvent, CostEntry } from '../lib/types';
import { PageHeader } from '../components/PageHeader';
import { StatCard } from '../components/StatCard';
import { StatusBadge } from '../components/StatusBadge';
import { EmptyState } from '../components/EmptyState';
import { money, relativeTime, getRiskPosture } from '../lib/utils';

export default function CommandCenter() {
  const { data: state, lastUpdated, refresh } = usePolling<AgentState>('/api/state');
  const { data: events } = usePolling<AgentEvent[]>('/api/events');
  const { data: costs } = usePolling<CostEntry[]>('/api/costs');

  if (!state) return null;

  const totalCost = costs?.reduce((s, c) => s + c.cost, 0) ?? 0;
  const cycleCount = state.cycle || 0;
  const avgCycleCost = cycleCount > 0 ? totalCost / cycleCount : 0;
  const risk = getRiskPosture(state.balance);
  const recentEvents = (events || []).slice(0, 15);

  return (
    <div>
      <PageHeader title="Command Center" lastUpdated={lastUpdated} onRefresh={refresh} />

      {/* Agent identity */}
      <div className="flex items-center gap-3 mb-6">
        <h2 className="text-2xl font-bold text-zinc-100">{state.name || 'Unnamed Agent'}</h2>
        <StatusBadge status={state.status || 'offline'} />
        {state.avatar?.creature && (
          <span className="text-sm text-zinc-400">the {state.avatar.creature}</span>
        )}
        {state.mood && <span className="text-sm text-zinc-500">{state.mood}</span>}
      </div>
      {state.avatar?.description && (
        <p className="text-xs text-zinc-500 -mt-4 mb-6">{state.avatar.description}</p>
      )}

      {/* Stats grid */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 mb-6">
        <StatCard label="Balance" value={money(state.balance)} accent />
        <StatCard label="Net Profit" value={money(state.net_profit)} sub={`ROI: ${state.roi_percent?.toFixed(1) || 0}%`} />
        <StatCard label="Tyler's Cut" value={money(state.tylers_cut)} />
        <StatCard label="GPU Fund" value={money(state.gpu_fund)} sub={`${state.gpu_fund_progress_percent?.toFixed(1) || 0}% of goal`} />
      </div>

      {/* Split + Risk + Burn */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3 mb-6">
        {/* 50/50 Split */}
        <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4">
          <p className="text-xs text-zinc-500 mb-3">50/50 Split</p>
          <div className="flex gap-2">
            <div className="flex-1">
              <div className="h-2 bg-violet-500/30 rounded-full overflow-hidden">
                <div className="h-full bg-violet-500 rounded-full" style={{ width: state.net_profit > 0 ? '100%' : '0%' }} />
              </div>
              <p className="text-xs text-zinc-400 mt-1">Tyler: {money(state.tylers_cut)}</p>
            </div>
            <div className="flex-1">
              <div className="h-2 bg-emerald-500/30 rounded-full overflow-hidden">
                <div className="h-full bg-emerald-500 rounded-full" style={{ width: state.net_profit > 0 ? '100%' : '0%' }} />
              </div>
              <p className="text-xs text-zinc-400 mt-1">GPU Fund: {money(state.gpu_fund)}</p>
            </div>
          </div>
        </div>

        {/* Risk Posture */}
        <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4">
          <p className="text-xs text-zinc-500 mb-1">Risk Posture</p>
          <p className={`text-lg font-semibold ${risk.color}`}>{risk.label}</p>
          <p className="text-xs text-zinc-500 mt-1">
            {risk.label === 'Preservation' ? 'Spending restricted' : risk.label === 'Normal' ? 'Standard limits' : 'Full throttle'}
          </p>
        </div>

        {/* Burn Rate */}
        <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4">
          <p className="text-xs text-zinc-500 mb-1">Burn Rate</p>
          <p className="text-lg font-semibold text-zinc-100 font-mono">{money(avgCycleCost)}<span className="text-xs text-zinc-500">/cycle</span></p>
          <p className="text-xs text-zinc-500 mt-1">
            {avgCycleCost > 0 ? `~${Math.floor(state.balance / avgCycleCost)} cycles remaining` : 'No data yet'}
          </p>
        </div>
      </div>

      {/* Quick stats row */}
      <div className="flex gap-4 mb-6 text-xs text-zinc-500">
        <span>Cycle <span className="text-zinc-300 font-mono">{cycleCount}</span></span>
        <span>Earned <span className="text-emerald-400 font-mono">{money(state.total_earned)}</span></span>
        <span>Spent <span className="text-red-400 font-mono">{money(state.total_spent)}</span></span>
        <span>Strategies <span className="text-zinc-300 font-mono">{state.active_strategies?.length || 0}</span></span>
      </div>

      {/* Activity feed */}
      <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-5">
        <h3 className="text-sm font-medium text-zinc-300 mb-4">Recent Activity</h3>
        {recentEvents.length === 0 ? (
          <EmptyState message="No activity yet. Run the agent's first cycle to see events here." />
        ) : (
          <div className="space-y-2">
            {recentEvents.map((e, i) => (
              <div key={i} className="flex items-start gap-3 text-sm">
                <span className="text-xs text-zinc-600 font-mono w-16 shrink-0 pt-0.5">{relativeTime(e.timestamp)}</span>
                <span className="text-zinc-500 w-24 shrink-0 truncate">{e.event_type}</span>
                <span className="text-zinc-400 truncate">
                  {typeof e.data === 'object' && e.data ? JSON.stringify(e.data).slice(0, 120) : ''}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
