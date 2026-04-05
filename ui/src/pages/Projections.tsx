import { useState, useMemo } from 'react';
import { usePolling } from '../lib/usePolling';
import type { Projection } from '../lib/types';
import { PageHeader } from '../components/PageHeader';
import { VerdictBadge } from '../components/VerdictBadge';
import { EmptyState } from '../components/EmptyState';
import { money, shortDate } from '../lib/utils';

export default function Projections() {
  const { data: projections, lastUpdated, refresh } = usePolling<Projection[]>('/api/projections');
  const [tab, setTab] = useState<'pending' | 'resolved'>('pending');

  const items = projections || [];
  const pending = useMemo(() => items.filter(p => p.status === 'pending'), [items]);
  const resolved = useMemo(() => items.filter(p => p.status === 'resolved'), [items]);
  const shown = tab === 'pending' ? pending : resolved;

  const hits = resolved.filter(p => p.resolution?.hit).length;
  const hitRate = resolved.length > 0 ? (hits / resolved.length) * 100 : 0;

  return (
    <div>
      <PageHeader title="Projections" lastUpdated={lastUpdated} onRefresh={refresh} />

      {items.length === 0 ? (
        <EmptyState message="No projections yet. The agent runs projections before spending over $5." />
      ) : (
        <>
          {resolved.length > 0 && (
            <div className="grid grid-cols-3 gap-3 mb-6">
              <div className="nest-card p-4 text-center">
                <p className="text-[10px] uppercase tracking-wider" style={{ color: 'var(--nest-text-ghost)' }}>Total</p>
                <p className="text-xl font-semibold font-mono" style={{ color: 'var(--nest-text-bright)' }}>{items.length}</p>
              </div>
              <div className="nest-card p-4 text-center">
                <p className="text-[10px] uppercase tracking-wider" style={{ color: 'var(--nest-text-ghost)' }}>Hit Rate</p>
                <p className="text-xl font-semibold font-mono text-[var(--nest-success)]">{hitRate.toFixed(0)}%</p>
              </div>
              <div className="nest-card p-4 text-center">
                <p className="text-[10px] uppercase tracking-wider" style={{ color: 'var(--nest-text-ghost)' }}>Avg Confidence</p>
                <p className="text-xl font-semibold font-mono" style={{ color: 'var(--nest-text-bright)' }}>
                  {(items.reduce((s, p) => s + (p.confidence_calibrated || p.confidence_raw || 0), 0) / items.length).toFixed(0)}%
                </p>
              </div>
            </div>
          )}

          <div className="flex gap-1 mb-4">
            {(['pending', 'resolved'] as const).map(t => (
              <button key={t} onClick={() => setTab(t)}
                className="text-sm px-3 py-1.5 rounded-md transition-colors"
                style={{
                  background: tab === t ? 'var(--nest-bg-surface)' : 'transparent',
                  color: tab === t ? 'var(--nest-text-bright)' : 'var(--nest-text-dim)',
                }}>
                {t.charAt(0).toUpperCase() + t.slice(1)} ({t === 'pending' ? pending.length : resolved.length})
              </button>
            ))}
          </div>

          <div className="space-y-3">
            {shown.map((p, idx) => (
              <div key={p.id} className="nest-card p-4 animate-fade-up" style={{ animationDelay: `${idx * 50}ms` }}>
                <div className="flex items-start justify-between mb-2">
                  <div>
                    <p className="text-sm" style={{ color: 'var(--nest-text)' }}>{p.action}</p>
                    <p className="text-xs mt-0.5" style={{ color: 'var(--nest-text-ghost)' }}>{shortDate(p.timestamp)} &middot; {p.strategy_type}</p>
                  </div>
                  <VerdictBadge verdict={p.verdict} />
                </div>
                <div className="grid grid-cols-4 gap-3 mt-3 text-center text-xs">
                  <div>
                    <p style={{ color: 'var(--nest-text-ghost)' }}>Cost</p>
                    <p className="font-mono" style={{ color: 'var(--nest-text)' }}>{money(p.cost)}</p>
                  </div>
                  <div>
                    <p style={{ color: 'var(--nest-text-ghost)' }}>Expected Return</p>
                    <p className="font-mono" style={{ color: 'var(--nest-text)' }}>{money(p.expected_return)}</p>
                  </div>
                  <div>
                    <p style={{ color: 'var(--nest-text-ghost)' }}>Confidence</p>
                    <p className="font-mono" style={{ color: 'var(--nest-text)' }}>{p.confidence_calibrated || p.confidence_raw}%</p>
                  </div>
                  <div>
                    <p style={{ color: 'var(--nest-text-ghost)' }}>Time</p>
                    <p className="font-mono" style={{ color: 'var(--nest-text)' }}>{p.time_to_return_days}d</p>
                  </div>
                </div>
                {p.resolution && (
                  <div className="mt-3 pt-3 grid grid-cols-3 gap-3 text-center text-xs" style={{ borderTop: '1px solid var(--nest-border)' }}>
                    <div>
                      <p style={{ color: 'var(--nest-text-ghost)' }}>Actual Return</p>
                      <p className="font-mono" style={{ color: 'var(--nest-text)' }}>{money(p.resolution.actual_return)}</p>
                    </div>
                    <div>
                      <p style={{ color: 'var(--nest-text-ghost)' }}>Profit Delta</p>
                      <p className={`font-mono ${p.resolution.profit_delta >= 0 ? 'text-[var(--nest-success)]' : 'text-[var(--nest-error)]'}`}>
                        {p.resolution.profit_delta >= 0 ? '+' : ''}{money(p.resolution.profit_delta)}
                      </p>
                    </div>
                    <div>
                      <p style={{ color: 'var(--nest-text-ghost)' }}>Result</p>
                      <p className={p.resolution.hit ? 'text-[var(--nest-success)]' : 'text-[var(--nest-error)]'}>{p.resolution.hit ? 'HIT' : 'MISS'}</p>
                    </div>
                  </div>
                )}
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
