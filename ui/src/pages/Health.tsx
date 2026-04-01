import { usePolling } from '../lib/usePolling';
import type { AgentState, CostEntry, Watch, AuditResult } from '../lib/types';
import { PageHeader } from '../components/PageHeader';
import { StatusBadge } from '../components/StatusBadge';
import { money, getRiskPosture, shortDate } from '../lib/utils';
import clsx from 'clsx';

export default function Health() {
  const { data: state, lastUpdated, refresh } = usePolling<AgentState>('/api/state');
  const { data: costs } = usePolling<CostEntry[]>('/api/costs');
  const { data: watches } = usePolling<Watch[]>('/api/watches');
  const { data: audits } = usePolling<AuditResult[]>('/api/audits');

  if (!state) return null;

  const costEntries = costs || [];
  const totalCost = costEntries.reduce((s, c) => s + c.cost, 0);
  const uniqueCycles = new Set(costEntries.map(c => c.cycle)).size;
  const avgCycleCost = uniqueCycles > 0 ? totalCost / uniqueCycles : 0;
  const dailyCost = avgCycleCost * 12; // estimate ~12 cycles/day at 5min intervals
  const cyclesRemaining = avgCycleCost > 0 ? Math.floor(state.balance / avgCycleCost) : 0;
  const risk = getRiskPosture(state.balance);

  const survivalColor = cyclesRemaining > 100 ? 'text-emerald-400' : cyclesRemaining > 50 ? 'text-amber-400' : 'text-red-400';

  const activeWatches = (watches || []).filter(w => w.status === 'active');
  const latestAudit = audits && audits.length > 0 ? audits[audits.length - 1] : null;

  return (
    <div>
      <PageHeader title="Health & System" lastUpdated={lastUpdated} onRefresh={refresh} />

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
        {/* Burn Rate */}
        <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4">
          <p className="text-xs text-zinc-500 mb-3">Burn Rate</p>
          <div className="space-y-2 text-sm">
            <div className="flex justify-between">
              <span className="text-zinc-500">Total API cost</span>
              <span className="text-zinc-200 font-mono">{money(totalCost)}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-zinc-500">Per cycle avg</span>
              <span className="text-zinc-200 font-mono">{money(avgCycleCost)}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-zinc-500">Est. daily</span>
              <span className="text-zinc-200 font-mono">{money(dailyCost)}</span>
            </div>
          </div>
        </div>

        {/* Survival */}
        <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4">
          <p className="text-xs text-zinc-500 mb-1">Survival Estimate</p>
          <p className={clsx('text-2xl font-bold font-mono', survivalColor)}>
            {avgCycleCost > 0 ? `${cyclesRemaining} cycles` : 'N/A'}
          </p>
          <p className="text-xs text-zinc-500 mt-1">at current burn rate</p>
          <div className="w-full h-1.5 bg-zinc-800 rounded-full mt-3">
            <div
              className={clsx('h-full rounded-full', cyclesRemaining > 100 ? 'bg-emerald-500' : cyclesRemaining > 50 ? 'bg-amber-500' : 'bg-red-500')}
              style={{ width: `${Math.min(cyclesRemaining, 200) / 2}%` }}
            />
          </div>
        </div>

        {/* Risk Posture */}
        <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4">
          <p className="text-xs text-zinc-500 mb-1">Risk Posture</p>
          <p className={clsx('text-2xl font-bold', risk.color)}>{risk.label}</p>
          <p className="text-xs text-zinc-500 mt-1">Balance: {money(state.balance)}</p>
          <div className="flex gap-1 mt-3">
            {['Preservation', 'Normal', 'Aggressive'].map(level => (
              <div
                key={level}
                className={clsx(
                  'flex-1 h-1.5 rounded-full',
                  risk.label === level ? (level === 'Aggressive' ? 'bg-emerald-500' : level === 'Normal' ? 'bg-amber-500' : 'bg-red-500') : 'bg-zinc-800'
                )}
              />
            ))}
          </div>
        </div>
      </div>

      {/* Watches */}
      <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-5 mb-6">
        <h3 className="text-sm font-medium text-zinc-300 mb-4">Active Watches</h3>
        {activeWatches.length === 0 ? (
          <p className="text-sm text-zinc-600">No active watches.</p>
        ) : (
          <div className="space-y-2">
            {activeWatches.map(w => (
              <div key={w.id} className="flex items-start justify-between bg-zinc-800/40 rounded-md p-3">
                <div>
                  <p className="text-sm text-zinc-300">{w.condition}</p>
                  <p className="text-xs text-zinc-500 mt-0.5">{w.action_hint}</p>
                </div>
                <div className="text-right shrink-0 ml-4">
                  <StatusBadge status={w.status} />
                  <p className="text-xs text-zinc-600 mt-1">Check after: {shortDate(w.check_after)}</p>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Latest Audit */}
      <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-5">
        <h3 className="text-sm font-medium text-zinc-300 mb-4">Latest Self-Audit</h3>
        {!latestAudit ? (
          <p className="text-sm text-zinc-600">No audits yet. Self-audit runs every 10 cycles.</p>
        ) : (
          <div>
            <p className="text-xs text-zinc-500 mb-4">Cycle {latestAudit.cycle} &middot; {shortDate(latestAudit.timestamp)}</p>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
              <div className="text-center">
                <p className="text-xs text-zinc-500">Projection Hits</p>
                <p className="text-lg font-mono text-zinc-200">{latestAudit.projection_accuracy.hits}/{latestAudit.projection_accuracy.count}</p>
              </div>
              <div className="text-center">
                <p className="text-xs text-zinc-500">Hit Rate</p>
                <p className="text-lg font-mono text-emerald-400">{latestAudit.projection_accuracy.actual_hit_rate.toFixed(0)}%</p>
              </div>
              <div className="text-center">
                <p className="text-xs text-zinc-500">Calibration</p>
                <p className="text-lg font-mono text-zinc-200">{latestAudit.projection_accuracy.calibration_multiplier.toFixed(2)}x</p>
              </div>
              <div className="text-center">
                <p className="text-xs text-zinc-500">Cost/$ Earned</p>
                <p className="text-lg font-mono text-zinc-200">{money(latestAudit.operational_efficiency.cost_per_dollar_earned)}</p>
              </div>
            </div>
            {latestAudit.recommendations.length > 0 && (
              <div>
                <p className="text-xs text-zinc-500 mb-2">Recommendations:</p>
                <ul className="space-y-1">
                  {latestAudit.recommendations.map((r, i) => (
                    <li key={i} className="text-sm text-zinc-400 flex gap-2">
                      <span className="text-zinc-600">&bull;</span>
                      {r}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
