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
  const dailyCost = avgCycleCost * 12;
  const cyclesRemaining = avgCycleCost > 0 ? Math.floor(state.balance / avgCycleCost) : 0;
  const risk = getRiskPosture(state.balance);

  const survivalColor = cyclesRemaining > 100 ? 'text-[var(--nest-success)]' : cyclesRemaining > 50 ? 'text-[var(--nest-warning)]' : 'text-[var(--nest-error)]';

  const activeWatches = (watches || []).filter(w => w.status === 'active');
  const latestAudit = audits && audits.length > 0 ? audits[audits.length - 1] : null;

  return (
    <div>
      <PageHeader title="Health & System" lastUpdated={lastUpdated} onRefresh={refresh} />

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
        {/* Burn Rate */}
        <div className="nest-card p-4">
          <p className="text-[10px] uppercase tracking-wider mb-3" style={{ color: 'var(--nest-text-ghost)' }}>Burn Rate</p>
          <div className="space-y-2 text-sm">
            <div className="flex justify-between">
              <span style={{ color: 'var(--nest-text-dim)' }}>Total API cost</span>
              <span className="font-mono" style={{ color: 'var(--nest-text)' }}>{money(totalCost)}</span>
            </div>
            <div className="flex justify-between">
              <span style={{ color: 'var(--nest-text-dim)' }}>Per cycle avg</span>
              <span className="font-mono" style={{ color: 'var(--nest-text)' }}>{money(avgCycleCost)}</span>
            </div>
            <div className="flex justify-between">
              <span style={{ color: 'var(--nest-text-dim)' }}>Est. daily</span>
              <span className="font-mono" style={{ color: 'var(--nest-text)' }}>{money(dailyCost)}</span>
            </div>
          </div>
        </div>

        {/* Survival */}
        <div className="nest-card p-4">
          <p className="text-[10px] uppercase tracking-wider mb-1" style={{ color: 'var(--nest-text-ghost)' }}>Survival Estimate</p>
          <p className={clsx('text-2xl font-bold font-mono', survivalColor)}>
            {avgCycleCost > 0 ? `${cyclesRemaining} cycles` : 'N/A'}
          </p>
          <p className="text-xs mt-1" style={{ color: 'var(--nest-text-dim)' }}>at current burn rate</p>
          <div className="w-full h-1.5 rounded-full mt-3" style={{ background: 'var(--nest-bg-surface)' }}>
            <div
              className={clsx('h-full rounded-full', cyclesRemaining > 100 ? 'bg-[var(--nest-success)]' : cyclesRemaining > 50 ? 'bg-[var(--nest-warning)]' : 'bg-[var(--nest-error)]')}
              style={{ width: `${Math.min(cyclesRemaining, 200) / 2}%` }}
            />
          </div>
        </div>

        {/* Risk Posture */}
        <div className="nest-card p-4">
          <p className="text-[10px] uppercase tracking-wider mb-1" style={{ color: 'var(--nest-text-ghost)' }}>Risk Posture</p>
          <p className={clsx('text-2xl font-bold', risk.color)}>{risk.label}</p>
          <p className="text-xs mt-1" style={{ color: 'var(--nest-text-dim)' }}>Balance: {money(state.balance)}</p>
          <div className="flex gap-1 mt-3">
            {['Preservation', 'Normal', 'Aggressive'].map(level => (
              <div
                key={level}
                className="flex-1 h-1.5 rounded-full"
                style={{
                  background: risk.label === level
                    ? (level === 'Aggressive' ? 'var(--nest-success)' : level === 'Normal' ? 'var(--nest-warning)' : 'var(--nest-error)')
                    : 'var(--nest-bg-surface)',
                }}
              />
            ))}
          </div>
        </div>
      </div>

      {/* Watches */}
      <div className="nest-card p-5 mb-6">
        <h3 className="text-sm font-medium mb-4" style={{ color: 'var(--nest-text)' }}>Active Watches</h3>
        {activeWatches.length === 0 ? (
          <p className="text-sm" style={{ color: 'var(--nest-text-ghost)' }}>No active watches.</p>
        ) : (
          <div className="space-y-2">
            {activeWatches.map(w => (
              <div key={w.id} className="flex items-start justify-between rounded-md p-3" style={{ background: 'var(--nest-bg-surface)' }}>
                <div>
                  <p className="text-sm" style={{ color: 'var(--nest-text)' }}>{w.condition}</p>
                  <p className="text-xs mt-0.5" style={{ color: 'var(--nest-text-dim)' }}>{w.action_hint}</p>
                </div>
                <div className="text-right shrink-0 ml-4">
                  <StatusBadge status={w.status} />
                  <p className="text-xs mt-1" style={{ color: 'var(--nest-text-ghost)' }}>Check after: {shortDate(w.check_after)}</p>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Latest Audit */}
      <div className="nest-card p-5">
        <h3 className="text-sm font-medium mb-4" style={{ color: 'var(--nest-text)' }}>Latest Self-Audit</h3>
        {!latestAudit ? (
          <p className="text-sm" style={{ color: 'var(--nest-text-ghost)' }}>No audits yet. Self-audit runs every 10 cycles.</p>
        ) : (
          <div>
            <p className="text-xs mb-4" style={{ color: 'var(--nest-text-ghost)' }}>Cycle {latestAudit.cycle} &middot; {shortDate(latestAudit.timestamp)}</p>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
              <div className="text-center">
                <p className="text-[10px]" style={{ color: 'var(--nest-text-ghost)' }}>Projection Hits</p>
                <p className="text-lg font-mono" style={{ color: 'var(--nest-text)' }}>{latestAudit.projection_accuracy.hits ?? 0}/{latestAudit.projection_accuracy.count ?? 0}</p>
              </div>
              <div className="text-center">
                <p className="text-[10px]" style={{ color: 'var(--nest-text-ghost)' }}>Hit Rate</p>
                <p className="text-lg font-mono text-[var(--nest-success)]">{(latestAudit.projection_accuracy.actual_hit_rate ?? 0).toFixed(0)}%</p>
              </div>
              <div className="text-center">
                <p className="text-[10px]" style={{ color: 'var(--nest-text-ghost)' }}>Calibration</p>
                <p className="text-lg font-mono" style={{ color: 'var(--nest-text)' }}>{(latestAudit.projection_accuracy.calibration_multiplier ?? 1).toFixed(2)}x</p>
              </div>
              <div className="text-center">
                <p className="text-[10px]" style={{ color: 'var(--nest-text-ghost)' }}>Cost/$ Earned</p>
                <p className="text-lg font-mono" style={{ color: 'var(--nest-text)' }}>{money(latestAudit.operational_efficiency.cost_per_dollar_earned)}</p>
              </div>
            </div>
            {latestAudit.recommendations.length > 0 && (
              <div>
                <p className="text-xs mb-2" style={{ color: 'var(--nest-text-ghost)' }}>Recommendations:</p>
                <ul className="space-y-1">
                  {latestAudit.recommendations.map((r, i) => (
                    <li key={i} className="text-sm flex gap-2" style={{ color: 'var(--nest-text-dim)' }}>
                      <span style={{ color: 'var(--nest-text-ghost)' }}>&bull;</span>
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
