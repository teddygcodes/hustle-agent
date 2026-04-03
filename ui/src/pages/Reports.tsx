import { useState, useMemo } from 'react';
import { usePolling } from '../lib/usePolling';
import type { TransactionReport } from '../lib/types';
import { PageHeader } from '../components/PageHeader';
import { VerdictBadge } from '../components/VerdictBadge';
import { EmptyState } from '../components/EmptyState';
import { money, shortDate } from '../lib/utils';
import clsx from 'clsx';

const typeColors: Record<string, string> = {
  income: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30',
  return: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30',
  expense: 'bg-red-500/15 text-red-400 border-red-500/30',
  investment: 'bg-violet-500/15 text-violet-400 border-violet-500/30',
};

const outcomeColors: Record<string, string> = {
  won: 'text-emerald-400',
  lost: 'text-red-400',
  pending: 'text-amber-400',
};

export default function Reports() {
  const { data: reports, lastUpdated, refresh } = usePolling<TransactionReport[]>('/api/reports');
  const [typeFilter, setTypeFilter] = useState<string>('all');
  const [statusFilter, setStatusFilter] = useState<string>('all');
  const [expanded, setExpanded] = useState<string | null>(null);

  const items = reports || [];

  const filtered = useMemo(() => {
    let list = items;
    if (typeFilter !== 'all') list = list.filter(r => r.type === typeFilter);
    if (statusFilter === 'pending') list = list.filter(r => !r.resolution);
    if (statusFilter === 'resolved') list = list.filter(r => !!r.resolution);
    return list;
  }, [items, typeFilter, statusFilter]);

  // Stats
  const resolved = items.filter(r => r.resolution);
  const hits = resolved.filter(r => r.summary.outcome === 'won').length;
  const hitRate = resolved.length > 0 ? (hits / resolved.length) * 100 : 0;
  const withEdge = items.filter(r => r.data_backing);
  const avgEdge = withEdge.length > 0
    ? withEdge.reduce((s, r) => s + (r.data_backing?.edge || 0), 0) / withEdge.length
    : 0;

  return (
    <div>
      <PageHeader title="Transaction Reports" lastUpdated={lastUpdated} onRefresh={refresh} />

      {items.length === 0 ? (
        <EmptyState message="No transaction reports yet. Reports are generated automatically when the agent records transactions." />
      ) : (
        <>
          {/* Stats row */}
          <div className="grid grid-cols-4 gap-3 mb-6">
            <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4 text-center">
              <p className="text-xs text-zinc-500">Total Reports</p>
              <p className="text-xl font-semibold text-zinc-100 font-mono">{items.length}</p>
            </div>
            <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4 text-center">
              <p className="text-xs text-zinc-500">Resolved</p>
              <p className="text-xl font-semibold text-zinc-100 font-mono">{resolved.length}</p>
            </div>
            <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4 text-center">
              <p className="text-xs text-zinc-500">Hit Rate</p>
              <p className={clsx('text-xl font-semibold font-mono', resolved.length > 0 ? 'text-emerald-400' : 'text-zinc-500')}>
                {resolved.length > 0 ? `${hitRate.toFixed(0)}%` : '--'}
              </p>
            </div>
            <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4 text-center">
              <p className="text-xs text-zinc-500">Avg Edge</p>
              <p className={clsx('text-xl font-semibold font-mono', withEdge.length > 0 ? 'text-violet-400' : 'text-zinc-500')}>
                {withEdge.length > 0 ? `${(avgEdge * 100).toFixed(1)}pp` : '--'}
              </p>
            </div>
          </div>

          {/* Filters */}
          <div className="flex flex-wrap gap-4 mb-4">
            <div className="flex gap-1">
              {['all', 'investment', 'expense', 'income', 'return'].map(t => (
                <button
                  key={t}
                  onClick={() => setTypeFilter(t)}
                  className={clsx(
                    'text-xs px-2.5 py-1 rounded-md transition-colors',
                    typeFilter === t ? 'bg-zinc-700 text-zinc-200' : 'text-zinc-500 hover:text-zinc-300'
                  )}
                >
                  {t}
                </button>
              ))}
            </div>
            <div className="flex gap-1">
              {['all', 'pending', 'resolved'].map(s => (
                <button
                  key={s}
                  onClick={() => setStatusFilter(s)}
                  className={clsx(
                    'text-xs px-2.5 py-1 rounded-md transition-colors',
                    statusFilter === s ? 'bg-zinc-700 text-zinc-200' : 'text-zinc-500 hover:text-zinc-300'
                  )}
                >
                  {s}
                </button>
              ))}
            </div>
          </div>

          {/* Report cards */}
          <div className="space-y-3">
            {filtered.map(r => {
              const isExpanded = expanded === r.report_id;
              return (
                <div
                  key={r.report_id}
                  className={clsx(
                    'bg-zinc-900 border rounded-lg transition-colors cursor-pointer',
                    r.summary.outcome === 'won' ? 'border-emerald-800/50' :
                    r.summary.outcome === 'lost' ? 'border-red-800/50' :
                    'border-zinc-800'
                  )}
                  onClick={() => setExpanded(isExpanded ? null : r.report_id)}
                >
                  {/* Summary row */}
                  <div className="p-4">
                    <div className="flex items-start justify-between mb-2">
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 mb-1">
                          <span className={clsx('text-[10px] font-semibold px-2 py-0.5 rounded border', typeColors[r.type] || typeColors.expense)}>
                            {r.type.toUpperCase()}
                          </span>
                          <span className="text-xs text-zinc-500 font-mono">{r.report_id}</span>
                          <span className={clsx('text-xs font-semibold', outcomeColors[r.summary.outcome])}>
                            {r.summary.outcome.toUpperCase()}
                          </span>
                        </div>
                        <p className="text-sm text-zinc-200 truncate">{r.summary.action}</p>
                        <p className="text-xs text-zinc-500 mt-0.5">{shortDate(r.timestamp)} &middot; {r.reasoning.strategy}</p>
                      </div>
                      <div className="text-right shrink-0 ml-4">
                        <p className="text-sm font-mono text-zinc-200">{money(r.summary.amount)}</p>
                        {r.data_backing && (
                          <p className="text-xs text-violet-400 font-mono mt-0.5">
                            edge: {(r.data_backing.edge * 100).toFixed(1)}pp
                          </p>
                        )}
                        {r.projection && (
                          <div className="mt-1">
                            <VerdictBadge verdict={r.projection.verdict_raw} />
                          </div>
                        )}
                      </div>
                    </div>
                  </div>

                  {/* Expanded detail */}
                  {isExpanded && (
                    <div className="border-t border-zinc-800 p-4 space-y-4">
                      {/* Reasoning */}
                      <div>
                        <h4 className="text-xs font-semibold text-zinc-400 uppercase tracking-wider mb-2">Reasoning</h4>
                        <p className="text-sm text-zinc-300">{r.reasoning.thesis}</p>
                        {r.reasoning.confidence_raw != null && (
                          <div className="flex gap-4 mt-2 text-xs text-zinc-500">
                            <span>Confidence: {r.reasoning.confidence_raw}% raw{r.reasoning.confidence_adjusted != null && ` -> ${r.reasoning.confidence_adjusted}% adjusted`}</span>
                            {r.reasoning.calibration_applied && <span>{r.reasoning.calibration_applied}</span>}
                          </div>
                        )}
                      </div>

                      {/* Data Backing */}
                      {r.data_backing && (
                        <div>
                          <h4 className="text-xs font-semibold text-zinc-400 uppercase tracking-wider mb-2">Data Backing</h4>
                          <div className="bg-zinc-950 border border-zinc-800 rounded-lg p-3 space-y-2">
                            <div className="flex justify-between text-sm">
                              <span className="text-zinc-500">Source</span>
                              <span className="text-zinc-200">{r.data_backing.source}</span>
                            </div>
                            <p className="text-xs text-zinc-400">{r.data_backing.data_point}</p>
                            <div className="grid grid-cols-3 gap-3 pt-2 border-t border-zinc-800">
                              <div className="text-center">
                                <p className="text-[10px] text-zinc-500 uppercase">Source Prob</p>
                                <p className="text-sm font-mono text-emerald-400">{(r.data_backing.source_probability * 100).toFixed(0)}%</p>
                              </div>
                              <div className="text-center">
                                <p className="text-[10px] text-zinc-500 uppercase">Market Price</p>
                                <p className="text-sm font-mono text-zinc-300">{(r.data_backing.market_price * 100).toFixed(0)}%</p>
                              </div>
                              <div className="text-center">
                                <p className="text-[10px] text-zinc-500 uppercase">Edge</p>
                                <p className="text-sm font-mono text-violet-400">{(r.data_backing.edge * 100).toFixed(1)}pp</p>
                              </div>
                            </div>
                            <p className="text-xs text-zinc-500">{r.data_backing.edge_direction}</p>
                          </div>
                        </div>
                      )}

                      {/* Projection */}
                      {r.projection && (
                        <div>
                          <h4 className="text-xs font-semibold text-zinc-400 uppercase tracking-wider mb-2">Projection</h4>
                          <div className="grid grid-cols-4 gap-3 text-center text-xs">
                            <div>
                              <p className="text-zinc-500">Expected Return</p>
                              <p className="text-zinc-300 font-mono">{money(r.projection.expected_return)}</p>
                            </div>
                            <div>
                              <p className="text-zinc-500">Expected Profit</p>
                              <p className="text-zinc-300 font-mono">{money(r.projection.expected_profit)}</p>
                            </div>
                            <div>
                              <p className="text-zinc-500">ROI</p>
                              <p className="text-zinc-300 font-mono">{r.projection.roi_percent.toFixed(1)}%</p>
                            </div>
                            <div>
                              <p className="text-zinc-500">Time</p>
                              <p className="text-zinc-300 font-mono">{r.projection.time_to_return_days}d</p>
                            </div>
                          </div>
                          {(r.projection.bull_case || r.projection.bear_case) && (
                            <div className="grid grid-cols-2 gap-3 mt-3 text-xs">
                              <div>
                                <p className="text-emerald-500 font-semibold mb-1">Bull Case</p>
                                <p className="text-zinc-400">{r.projection.bull_case}</p>
                              </div>
                              <div>
                                <p className="text-red-500 font-semibold mb-1">Bear Case</p>
                                <p className="text-zinc-400">{r.projection.bear_case}</p>
                              </div>
                            </div>
                          )}
                        </div>
                      )}

                      {/* Resolution */}
                      {r.resolution && (
                        <div>
                          <h4 className="text-xs font-semibold text-zinc-400 uppercase tracking-wider mb-2">Resolution</h4>
                          <div className="grid grid-cols-3 gap-3 text-center text-xs">
                            <div>
                              <p className="text-zinc-500">Actual Return</p>
                              <p className="text-zinc-300 font-mono">{money(r.resolution.actual_return)}</p>
                            </div>
                            <div>
                              <p className="text-zinc-500">P&L</p>
                              <p className={clsx('font-mono', (r.resolution.actual_profit_loss || 0) >= 0 ? 'text-emerald-400' : 'text-red-400')}>
                                {(r.resolution.actual_profit_loss || 0) >= 0 ? '+' : ''}{money(r.resolution.actual_profit_loss || 0)}
                              </p>
                            </div>
                            <div>
                              <p className="text-zinc-500">vs Prediction</p>
                              <p className={clsx('font-mono', (r.resolution.prediction_delta || 0) >= 0 ? 'text-emerald-400' : 'text-red-400')}>
                                {(r.resolution.prediction_delta || 0) >= 0 ? '+' : ''}{money(r.resolution.prediction_delta || 0)}
                              </p>
                            </div>
                          </div>
                          {r.resolution.actual_outcome && (
                            <p className="text-xs text-zinc-400 mt-2">{r.resolution.actual_outcome}</p>
                          )}
                        </div>
                      )}

                      {/* Linked IDs */}
                      <div className="flex gap-4 text-[10px] text-zinc-600 pt-2 border-t border-zinc-800">
                        {r.linked_ids.projection_id && <span>proj: {r.linked_ids.projection_id}</span>}
                        {r.linked_ids.kalshi_order_id && <span>order: {r.linked_ids.kalshi_order_id}</span>}
                        <span>txn: #{r.linked_ids.ledger_id}</span>
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </>
      )}
    </div>
  );
}
