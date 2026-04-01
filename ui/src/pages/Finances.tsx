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
            <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-5">
              <h3 className="text-sm font-medium text-zinc-300 mb-4">Operational Costs</h3>
              <div className="space-y-3">
                <div className="flex justify-between text-sm">
                  <span className="text-zinc-500">Total API Cost</span>
                  <span className="text-zinc-200 font-mono">{money(totalApiCost)}</span>
                </div>
                <div className="flex justify-between text-sm">
                  <span className="text-zinc-500">Avg per Cycle</span>
                  <span className="text-zinc-200 font-mono">{costs && costs.length > 0 ? money(totalApiCost / new Set(costs.map(c => c.cycle)).size) : '$0.00'}</span>
                </div>
              </div>
            </div>
          </div>

          {/* Ledger table */}
          <div className="bg-zinc-900 border border-zinc-800 rounded-lg overflow-hidden">
            <div className="flex items-center gap-2 p-4 border-b border-zinc-800">
              <h3 className="text-sm font-medium text-zinc-300">Ledger</h3>
              <div className="ml-auto flex gap-1">
                {['all', 'income', 'expense', 'investment', 'return'].map(t => (
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
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-xs text-zinc-500 border-b border-zinc-800">
                    <th className="text-left px-4 py-2 cursor-pointer hover:text-zinc-300" onClick={() => toggleSort('timestamp')}>Date</th>
                    <th className="text-left px-4 py-2">Type</th>
                    <th className="text-right px-4 py-2 cursor-pointer hover:text-zinc-300" onClick={() => toggleSort('amount')}>Amount</th>
                    <th className="text-left px-4 py-2">Description</th>
                    <th className="text-left px-4 py-2">Strategy</th>
                    <th className="text-right px-4 py-2">Balance</th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.map(t => (
                    <tr key={t.id} className="border-b border-zinc-800/50 hover:bg-zinc-800/30">
                      <td className="px-4 py-2 text-zinc-400 font-mono text-xs whitespace-nowrap">{shortDate(t.timestamp)}</td>
                      <td className="px-4 py-2">
                        <span className={clsx(
                          'text-xs',
                          t.type === 'income' || t.type === 'return' ? 'text-emerald-400' : 'text-red-400'
                        )}>{t.type}</span>
                      </td>
                      <td className={clsx(
                        'px-4 py-2 text-right font-mono',
                        t.type === 'income' || t.type === 'return' ? 'text-emerald-400' : 'text-red-400'
                      )}>
                        {t.type === 'income' || t.type === 'return' ? '+' : '-'}{money(t.amount)}
                      </td>
                      <td className="px-4 py-2 text-zinc-400 max-w-xs truncate">{t.description}</td>
                      <td className="px-4 py-2 text-zinc-500 text-xs">{t.strategy || '—'}</td>
                      <td className="px-4 py-2 text-right text-zinc-300 font-mono">{money(t.balance_after)}</td>
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
