import { useState, useMemo } from 'react';
import { usePolling } from '../lib/usePolling';
import type { Transaction, CostEntry } from '../lib/types';
import { PageHeader } from '../components/PageHeader';
import { EmptyState } from '../components/EmptyState';
import { BalanceChart } from '../components/charts/BalanceChart';
import { DailyPnLChart } from '../components/charts/DailyPnLChart';
import { StrategyBreakdown } from '../components/charts/StrategyBreakdown';
import { money, shortDate } from '../lib/utils';
import clsx from 'clsx';

export default function Finances() {
  const { data: ledger, lastUpdated, refresh } = usePolling<Transaction[]>('/api/ledger');
  const { data: costs } = usePolling<CostEntry[]>('/api/costs');
  const [typeFilter, setTypeFilter] = useState<string>('all');
  const [sortField, setSortField] = useState<'timestamp' | 'amount'>('timestamp');
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc');

  const items = ledger || [];
  const totalApiCost = costs?.reduce((s, c) => s + c.cost, 0) ?? 0;

  const filtered = useMemo(() => {
    let list = typeFilter === 'all' ? items : items.filter(t => t.type === typeFilter);
    list = [...list].sort((a, b) => {
      const av = sortField === 'amount' ? a.amount : new Date(a.timestamp).getTime();
      const bv = sortField === 'amount' ? b.amount : new Date(b.timestamp).getTime();
      return sortDir === 'desc' ? bv - av : av - bv;
    });
    return list;
  }, [items, typeFilter, sortField, sortDir]);

  const toggleSort = (field: 'timestamp' | 'amount') => {
    if (sortField === field) setSortDir(d => d === 'desc' ? 'asc' : 'desc');
    else { setSortField(field); setSortDir('desc'); }
  };

  return (
    <div>
      <PageHeader title="Finances" lastUpdated={lastUpdated} onRefresh={refresh} />

      {items.length === 0 ? (
        <EmptyState message="No transactions yet. Financial data will appear after the agent's first spend." />
      ) : (
        <>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-6">
            <BalanceChart ledger={items} />
            <DailyPnLChart ledger={items} />
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-6">
            <StrategyBreakdown ledger={items} />
            <div className="nest-card p-5">
              <h3 className="text-sm font-medium mb-4" style={{ color: 'var(--nest-text)' }}>Operational Costs</h3>
              <div className="space-y-3">
                <div className="flex justify-between text-sm">
                  <span style={{ color: 'var(--nest-text-dim)' }}>Total API Cost</span>
                  <span className="font-mono" style={{ color: 'var(--nest-text)' }}>{money(totalApiCost)}</span>
                </div>
                <div className="flex justify-between text-sm">
                  <span style={{ color: 'var(--nest-text-dim)' }}>Avg per Cycle</span>
                  <span className="font-mono" style={{ color: 'var(--nest-text)' }}>{costs && costs.length > 0 ? money(totalApiCost / new Set(costs.map(c => c.cycle)).size) : '$0.00'}</span>
                </div>
              </div>
            </div>
          </div>

          {/* Ledger table */}
          <div className="nest-card overflow-hidden">
            <div className="flex items-center gap-2 p-4" style={{ borderBottom: '1px solid var(--nest-border)' }}>
              <h3 className="text-sm font-medium" style={{ color: 'var(--nest-text)' }}>Ledger</h3>
              <div className="ml-auto flex gap-1">
                {['all', 'income', 'expense', 'investment', 'return'].map(t => (
                  <button
                    key={t}
                    onClick={() => setTypeFilter(t)}
                    className={clsx(
                      'text-xs px-2.5 py-1 rounded-md transition-colors',
                      typeFilter === t
                        ? 'text-[var(--nest-text-bright)]'
                        : 'hover:text-[var(--nest-text)]'
                    )}
                    style={{
                      background: typeFilter === t ? 'var(--nest-bg-surface)' : 'transparent',
                      color: typeFilter === t ? undefined : 'var(--nest-text-dim)',
                    }}
                  >
                    {t}
                  </button>
                ))}
              </div>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-xs" style={{ borderBottom: '1px solid var(--nest-border)', color: 'var(--nest-text-dim)' }}>
                    <th className="text-left px-4 py-2 cursor-pointer hover:text-[var(--nest-text)]" onClick={() => toggleSort('timestamp')}>Date</th>
                    <th className="text-left px-4 py-2">Type</th>
                    <th className="text-right px-4 py-2 cursor-pointer hover:text-[var(--nest-text)]" onClick={() => toggleSort('amount')}>Amount</th>
                    <th className="text-left px-4 py-2">Description</th>
                    <th className="text-left px-4 py-2">Strategy</th>
                    <th className="text-right px-4 py-2">Balance</th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.map(t => (
                    <tr key={t.id} className="hover:bg-white/[0.02]" style={{ borderBottom: '1px solid var(--nest-border-subtle)' }}>
                      <td className="px-4 py-2 font-mono text-xs whitespace-nowrap" style={{ color: 'var(--nest-text-dim)' }}>{shortDate(t.timestamp)}</td>
                      <td className="px-4 py-2">
                        <span className={clsx('text-xs', t.type === 'income' || t.type === 'return' ? 'text-[var(--nest-success)]' : 'text-[var(--nest-error)]')}>{t.type}</span>
                      </td>
                      <td className={clsx('px-4 py-2 text-right font-mono', t.type === 'income' || t.type === 'return' ? 'text-[var(--nest-success)]' : 'text-[var(--nest-error)]')}>
                        {t.type === 'income' || t.type === 'return' ? '+' : '-'}{money(t.amount)}
                      </td>
                      <td className="px-4 py-2 max-w-xs truncate" style={{ color: 'var(--nest-text-dim)' }}>{t.description}</td>
                      <td className="px-4 py-2 text-xs" style={{ color: 'var(--nest-text-ghost)' }}>{t.strategy || '\u2014'}</td>
                      <td className="px-4 py-2 text-right font-mono" style={{ color: 'var(--nest-text)' }}>{money(t.balance_after)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
