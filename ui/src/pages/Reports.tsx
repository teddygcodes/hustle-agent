import { useState, useMemo } from 'react';
import { usePolling } from '../lib/usePolling';
import type { TransactionReport } from '../lib/types';
import { PageHeader } from '../components/PageHeader';
import { VerdictBadge } from '../components/VerdictBadge';
import { EmptyState } from '../components/EmptyState';
import { money, shortDate } from '../lib/utils';
import clsx from 'clsx';

const typeColors: Record<string, { bg: string; text: string; border: string }> = {
  income: { bg: 'rgba(16, 185, 129, 0.1)', text: 'var(--nest-success)', border: 'rgba(16, 185, 129, 0.2)' },
  return: { bg: 'rgba(16, 185, 129, 0.1)', text: 'var(--nest-success)', border: 'rgba(16, 185, 129, 0.2)' },
  expense: { bg: 'rgba(239, 68, 68, 0.1)', text: 'var(--nest-error)', border: 'rgba(239, 68, 68, 0.2)' },
  investment: { bg: 'rgba(124, 58, 237, 0.1)', text: 'var(--nest-purple)', border: 'rgba(124, 58, 237, 0.2)' },
};

const outcomeColors: Record<string, string> = {
  won: 'var(--nest-success)',
  lost: 'var(--nest-error)',
  pending: 'var(--nest-warning)',
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
          <div className="grid grid-cols-4 gap-3 mb-6">
            {[
              { label: 'Total Reports', value: items.length.toString(), color: 'var(--nest-text-bright)' },
              { label: 'Resolved', value: resolved.length.toString(), color: 'var(--nest-text-bright)' },
              { label: 'Hit Rate', value: resolved.length > 0 ? `${hitRate.toFixed(0)}%` : '--', color: resolved.length > 0 ? 'var(--nest-success)' : 'var(--nest-text-ghost)' },
              { label: 'Avg Edge', value: withEdge.length > 0 ? `${(avgEdge * 100).toFixed(1)}pp` : '--', color: withEdge.length > 0 ? 'var(--nest-purple)' : 'var(--nest-text-ghost)' },
            ].map(s => (
              <div key={s.label} className="nest-card p-4 text-center">
                <p className="text-[10px] uppercase tracking-wider" style={{ color: 'var(--nest-text-ghost)' }}>{s.label}</p>
                <p className="text-xl font-semibold font-mono" style={{ color: s.color }}>{s.value}</p>
              </div>
            ))}
          </div>

          <div className="flex flex-wrap gap-4 mb-4">
            <div className="flex gap-1">
              {['all', 'investment', 'expense', 'income', 'return'].map(t => (
                <button key={t} onClick={() => setTypeFilter(t)}
                  className="text-xs px-2.5 py-1 rounded-md transition-colors"
                  style={{
                    background: typeFilter === t ? 'var(--nest-bg-surface)' : 'transparent',
                    color: typeFilter === t ? 'var(--nest-text-bright)' : 'var(--nest-text-dim)',
                  }}>
                  {t}
                </button>
              ))}
            </div>
            <div className="flex gap-1">
              {['all', 'pending', 'resolved'].map(s => (
                <button key={s} onClick={() => setStatusFilter(s)}
                  className="text-xs px-2.5 py-1 rounded-md transition-colors"
                  style={{
                    background: statusFilter === s ? 'var(--nest-bg-surface)' : 'transparent',
                    color: statusFilter === s ? 'var(--nest-text-bright)' : 'var(--nest-text-dim)',
                  }}>
                  {s}
                </button>
              ))}
            </div>
          </div>

          <div className="space-y-3">
            {filtered.map((r, idx) => {
              const isExpanded = expanded === r.report_id;
              const tc = typeColors[r.type] || typeColors.expense;
              return (
                <div
                  key={r.report_id}
                  className="nest-card cursor-pointer animate-fade-up"
                  style={{
                    animationDelay: `${idx * 40}ms`,
                    borderColor: r.summary.outcome === 'won' ? 'rgba(16, 185, 129, 0.2)' :
                      r.summary.outcome === 'lost' ? 'rgba(239, 68, 68, 0.2)' : undefined,
                  }}
                  onClick={() => setExpanded(isExpanded ? null : r.report_id)}
                >
                  <div className="p-4">
                    <div className="flex items-start justify-between mb-2">
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 mb-1">
                          <span className="text-[10px] font-semibold px-2 py-0.5 rounded"
                            style={{ background: tc.bg, color: tc.text, border: `1px solid ${tc.border}` }}>
                            {r.type.toUpperCase()}
                          </span>
                          <span className="text-xs font-mono" style={{ color: 'var(--nest-text-ghost)' }}>{r.report_id}</span>
                          <span className="text-xs font-semibold" style={{ color: outcomeColors[r.summary.outcome] }}>
                            {r.summary.outcome.toUpperCase()}
                          </span>
                        </div>
                        <p className="text-sm truncate" style={{ color: 'var(--nest-text)' }}>{r.summary.action}</p>
                        <p className="text-xs mt-0.5" style={{ color: 'var(--nest-text-ghost)' }}>{shortDate(r.timestamp)} &middot; {r.reasoning.strategy}</p>
                      </div>
                      <div className="text-right shrink-0 ml-4">
                        <p className="text-sm font-mono" style={{ color: 'var(--nest-text)' }}>{money(r.summary.amount)}</p>
                        {r.data_backing && (
                          <p className="text-xs font-mono mt-0.5" style={{ color: 'var(--nest-purple)' }}>
                            edge: {(r.data_backing.edge * 100).toFixed(1)}pp
                          </p>
                        )}
                        {r.projection && (
                          <div className="mt-1"><VerdictBadge verdict={r.projection.verdict_raw} /></div>
                        )}
                      </div>
                    </div>
                  </div>

                  {isExpanded && (
                    <div className="p-4 space-y-4" style={{ borderTop: '1px solid var(--nest-border)' }}>
                      <div>
                        <h4 className="text-[10px] font-semibold uppercase tracking-wider mb-2" style={{ color: 'var(--nest-text-dim)' }}>Reasoning</h4>
                        <p className="text-sm" style={{ color: 'var(--nest-text)' }}>{r.reasoning.thesis}</p>
                        {r.reasoning.confidence_raw != null && (
                          <div className="flex gap-4 mt-2 text-xs" style={{ color: 'var(--nest-text-ghost)' }}>
                            <span>Confidence: {r.reasoning.confidence_raw}% raw{r.reasoning.confidence_adjusted != null && ` \u2192 ${r.reasoning.confidence_adjusted}% adjusted`}</span>
                            {r.reasoning.calibration_applied && <span>{r.reasoning.calibration_applied}</span>}
                          </div>
                        )}
                      </div>

                      {r.data_backing && (
                        <div>
                          <h4 className="text-[10px] font-semibold uppercase tracking-wider mb-2" style={{ color: 'var(--nest-text-dim)' }}>Data Backing</h4>
                          <div className="rounded-lg p-3 space-y-2" style={{ background: 'var(--nest-bg)', border: '1px solid var(--nest-border)' }}>
                            <div className="flex justify-between text-sm">
                              <span style={{ color: 'var(--nest-text-ghost)' }}>Source</span>
                              <span style={{ color: 'var(--nest-text)' }}>{r.data_backing.source}</span>
                            </div>
                            <p className="text-xs" style={{ color: 'var(--nest-text-dim)' }}>{r.data_backing.data_point}</p>
                            <div className="grid grid-cols-3 gap-3 pt-2" style={{ borderTop: '1px solid var(--nest-border)' }}>
                              <div className="text-center">
                                <p className="text-[10px] uppercase" style={{ color: 'var(--nest-text-ghost)' }}>Source Prob</p>
                                <p className="text-sm font-mono text-[var(--nest-success)]">{(r.data_backing.source_probability * 100).toFixed(0)}%</p>
                              </div>
                              <div className="text-center">
                                <p className="text-[10px] uppercase" style={{ color: 'var(--nest-text-ghost)' }}>Market Price</p>
                                <p className="text-sm font-mono" style={{ color: 'var(--nest-text)' }}>{(r.data_backing.market_price * 100).toFixed(0)}%</p>
                              </div>
                              <div className="text-center">
                                <p className="text-[10px] uppercase" style={{ color: 'var(--nest-text-ghost)' }}>Edge</p>
                                <p className="text-sm font-mono" style={{ color: 'var(--nest-purple)' }}>{(r.data_backing.edge * 100).toFixed(1)}pp</p>
                              </div>
                            </div>
                          </div>
                        </div>
                      )}

                      {r.projection && (
                        <div>
                          <h4 className="text-[10px] font-semibold uppercase tracking-wider mb-2" style={{ color: 'var(--nest-text-dim)' }}>Projection</h4>
                          <div className="grid grid-cols-4 gap-3 text-center text-xs">
                            {[
                              { l: 'Expected Return', v: money(r.projection.expected_return) },
                              { l: 'Expected Profit', v: money(r.projection.expected_profit) },
                              { l: 'ROI', v: `${r.projection.roi_percent.toFixed(1)}%` },
                              { l: 'Time', v: `${r.projection.time_to_return_days}d` },
                            ].map(x => (
                              <div key={x.l}>
                                <p style={{ color: 'var(--nest-text-ghost)' }}>{x.l}</p>
                                <p className="font-mono" style={{ color: 'var(--nest-text)' }}>{x.v}</p>
                              </div>
                            ))}
                          </div>
                          {(r.projection.bull_case || r.projection.bear_case) && (
                            <div className="grid grid-cols-2 gap-3 mt-3 text-xs">
                              <div>
                                <p className="font-semibold mb-1 text-[var(--nest-success)]">Bull Case</p>
                                <p style={{ color: 'var(--nest-text-dim)' }}>{r.projection.bull_case}</p>
                              </div>
                              <div>
                                <p className="font-semibold mb-1 text-[var(--nest-error)]">Bear Case</p>
                                <p style={{ color: 'var(--nest-text-dim)' }}>{r.projection.bear_case}</p>
                              </div>
                            </div>
                          )}
                        </div>
                      )}

                      {r.resolution && (
                        <div>
                          <h4 className="text-[10px] font-semibold uppercase tracking-wider mb-2" style={{ color: 'var(--nest-text-dim)' }}>Resolution</h4>
                          <div className="grid grid-cols-3 gap-3 text-center text-xs">
                            <div>
                              <p style={{ color: 'var(--nest-text-ghost)' }}>Actual Return</p>
                              <p className="font-mono" style={{ color: 'var(--nest-text)' }}>{money(r.resolution.actual_return)}</p>
                            </div>
                            <div>
                              <p style={{ color: 'var(--nest-text-ghost)' }}>P&L</p>
                              <p className={clsx('font-mono', (r.resolution.actual_profit_loss || 0) >= 0 ? 'text-[var(--nest-success)]' : 'text-[var(--nest-error)]')}>
                                {(r.resolution.actual_profit_loss || 0) >= 0 ? '+' : ''}{money(r.resolution.actual_profit_loss || 0)}
                              </p>
                            </div>
                            <div>
                              <p style={{ color: 'var(--nest-text-ghost)' }}>vs Prediction</p>
                              <p className={clsx('font-mono', (r.resolution.prediction_delta || 0) >= 0 ? 'text-[var(--nest-success)]' : 'text-[var(--nest-error)]')}>
                                {(r.resolution.prediction_delta || 0) >= 0 ? '+' : ''}{money(r.resolution.prediction_delta || 0)}
                              </p>
                            </div>
                          </div>
                          {r.resolution.actual_outcome && (
                            <p className="text-xs mt-2" style={{ color: 'var(--nest-text-dim)' }}>{r.resolution.actual_outcome}</p>
                          )}
                        </div>
                      )}

                      <div className="flex gap-4 text-[10px] pt-2" style={{ borderTop: '1px solid var(--nest-border)', color: 'var(--nest-text-ghost)' }}>
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
