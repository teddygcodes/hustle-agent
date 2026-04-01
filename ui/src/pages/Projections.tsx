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

  // Accuracy stats
  const hits = resolved.filter(p => p.resolution?.hit).length;
  const hitRate = resolved.length > 0 ? (hits / resolved.length) * 100 : 0;

  return (
    <div>
      <PageHeader title="Projections" lastUpdated={lastUpdated} onRefresh={refresh} />

      {items.length === 0 ? (
        <EmptyState message="No projections yet. The agent runs projections before spending over $5." />
      ) : (
        <>
          {/* Accuracy stats */}
          {resolved.length > 0 && (
            <div className="grid grid-cols-3 gap-3 mb-6">
              <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4 text-center">
                <p className="text-xs text-zinc-500">Total</p>
                <p className="text-xl font-semibold text-zinc-100 font-mono">{items.length}</p>
              </div>
              <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4 text-center">
                <p className="text-xs text-zinc-500">Hit Rate</p>
                <p className="text-xl font-semibold text-emerald-400 font-mono">{hitRate.toFixed(0)}%</p>
              </div>
              <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4 text-center">
                <p className="text-xs text-zinc-500">Avg Confidence</p>
                <p className="text-xl font-semibold text-zinc-100 font-mono">
                  {(items.reduce((s, p) => s + (p.confidence_calibrated || p.confidence_raw || 0), 0) / items.length).toFixed(0)}%
                </p>
              </div>
            </div>
          )}

          {/* Tabs */}
          <div className="flex gap-1 mb-4">
            <button onClick={() => setTab('pending')} className={`text-sm px-3 py-1.5 rounded-md ${tab === 'pending' ? 'bg-zinc-800 text-zinc-200' : 'text-zinc-500 hover:text-zinc-300'}`}>
              Pending ({pending.length})
            </button>
            <button onClick={() => setTab('resolved')} className={`text-sm px-3 py-1.5 rounded-md ${tab === 'resolved' ? 'bg-zinc-800 text-zinc-200' : 'text-zinc-500 hover:text-zinc-300'}`}>
              Resolved ({resolved.length})
            </button>
          </div>

          <div className="space-y-3">
            {shown.map(p => (
              <div key={p.id} className="bg-zinc-900 border border-zinc-800 rounded-lg p-4">
                <div className="flex items-start justify-between mb-2">
                  <div>
                    <p className="text-sm text-zinc-200 mb-1">{p.action}</p>
                    <p className="text-xs text-zinc-500">{shortDate(p.timestamp)} &middot; {p.strategy_type}</p>
                  </div>
                  <VerdictBadge verdict={p.verdict} />
                </div>
                <div className="grid grid-cols-4 gap-3 mt-3 text-center text-xs">
                  <div>
                    <p className="text-zinc-500">Cost</p>
                    <p className="text-zinc-300 font-mono">{money(p.cost)}</p>
                  </div>
                  <div>
                    <p className="text-zinc-500">Expected Return</p>
                    <p className="text-zinc-300 font-mono">{money(p.expected_return)}</p>
                  </div>
                  <div>
                    <p className="text-zinc-500">Confidence</p>
                    <p className="text-zinc-300 font-mono">{p.confidence_calibrated || p.confidence_raw}%</p>
                  </div>
                  <div>
                    <p className="text-zinc-500">Time</p>
                    <p className="text-zinc-300 font-mono">{p.time_to_return_days}d</p>
                  </div>
                </div>
                {p.resolution && (
                  <div className="mt-3 pt-3 border-t border-zinc-800 grid grid-cols-3 gap-3 text-center text-xs">
                    <div>
                      <p className="text-zinc-500">Actual Return</p>
                      <p className="text-zinc-300 font-mono">{money(p.resolution.actual_return)}</p>
                    </div>
                    <div>
                      <p className="text-zinc-500">Profit Delta</p>
                      <p className={`font-mono ${p.resolution.profit_delta >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                        {p.resolution.profit_delta >= 0 ? '+' : ''}{money(p.resolution.profit_delta)}
                      </p>
                    </div>
                    <div>
                      <p className="text-zinc-500">Result</p>
                      <p className={p.resolution.hit ? 'text-emerald-400' : 'text-red-400'}>{p.resolution.hit ? 'HIT' : 'MISS'}</p>
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
